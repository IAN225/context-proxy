"""压缩主流程。

一次请求的处理顺序（全程持会话锁）：

    定位会话 → 对齐 checkpoint → 判阈值 → 按轮切批 → 逐批调摘要模型并**立刻落盘**
    → （必要时）二次重压 → 封存 checkpoint → 组装 → 出口闸门

几条硬规则：

* **绝不把未压缩的上下文放行到上游。** 低于 ``trigger_tokens`` 的原样转发是正常设计；
  一旦超过阈值而压缩没有成功完成，就报错，不降级转发。最后还有一道出口闸门：
  转发前实测 token，超过 ``trigger_tokens × exit_gate_ratio`` 直接拒绝，
  用来兜住所有还没想到的 bug 路径。
* **每批成功就落盘**，摘要和压缩位置在同一个事务里更新。一次请求最多压
  ``max_batches_per_request`` 批，压不完带着已落盘的进度返回，下次请求接着压——
  这是终结"几十批跑几十分钟、失败即全丢、下次从头再来"死循环的关键。
* **切点只落在轮边界上**，不会在一轮中间（比如 assistant 与它的 tool 返回之间）切开。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from . import config, locate, store, summarizer
from . import messages as M

log = logging.getLogger("proxy.compress")

OnEvent = Callable[..., Awaitable[None]]


class CompressionRefused(Exception):
    """压缩未能把上下文降到阈值以下，拒绝转发。``detail`` 会原样返回给用户。"""

    def __init__(self, message: str, detail: dict[str, Any]):
        super().__init__(message)
        self.message = message
        self.detail = detail

    def user_text(self) -> str:
        d = self.detail
        lines = [f"⚠️ 上下文压缩未完成，本次请求已被代理拦截（不会按全量 token 转发给上游）。",
                 f"原因：{self.message}"]
        if d.get("round_upto") is not None:
            lines.append(f"进度：已压缩到第 {d['round_upto']}/{d.get('total_rounds', '?')} 轮"
                         f"（消息下标 {d.get('compressed_upto')}），"
                         f"剩余 {d.get('remaining_rounds', '?')} 轮 / 约 {d.get('remaining_tokens', '?')} tokens")
        if d.get("final_tokens") is not None:
            lines.append(f"实测待转发 {d['final_tokens']} tokens，闸门上限 {d.get('gate_tokens')} tokens")
            if d.get("keep_recent_floor") and d.get("retained_tokens"):
                lines.append(
                    f"其中近期原文 {d['retained_tokens']} tokens 是硬性保留的"
                    f"（keep_recent_tokens 有效值 {d['keep_recent_floor']}，"
                    "已按 trigger_tokens 的 50% 自动封顶），压缩不会动它。")
        if d.get("cause") == "batch_cap":
            lines.append(f"本次请求压了 {d.get('batches_done')}/{d.get('batches_planned')} 批就到达"
                         "单请求上限（避免一个请求跑几十分钟）。超大历史的首次压缩需要分几次请求完成。")
        elif d.get("cause") == "oversize_tail":
            # 近期原文已经按 trigger 的 50% 封顶了，还顶穿闸门只可能是这一段本身太大
            lines.append(f"压缩已经压无可压：最近一轮原文本身就有 {d.get('last_round_tokens', '?')} tokens，"
                         "它属于硬性保留的近期原文，压缩碰不到它。")
            if d.get("tool_tokens"):
                lines.append(f"其中 {d['tool_tokens']} tokens 来自工具调用与工具返回——"
                             "工具调用的请求与结果会整段留在近期原文里，是最常见的撑爆原因。")
            lines.append("请这样处理：编辑或缩短最后一条消息后重发，并避免在这一轮里让模型调用工具；"
                         "如果反复出现，说明 trigger_tokens 相对这个对话设得太小了。")
        lines.append("已完成的部分**已经保存**，直接重发这条消息即可从断点继续，不会重复计费。")
        return "\n".join(lines)


@dataclass
class Prepared:
    messages: list[dict]
    meta: dict[str, Any] = field(default_factory=dict)


# ===== per-conversation 异步锁（LRU 防泄漏）=====
_LOCKS: "OrderedDict[str, asyncio.Lock]" = OrderedDict()


def _lock_for(key: str) -> asyncio.Lock:
    lk = _LOCKS.get(key)
    if lk is None:
        lk = asyncio.Lock()
        _LOCKS[key] = lk
    _LOCKS.move_to_end(key)
    limit = max(64, int(config.summary().get("cache_max_entries", 1024)))
    while len(_LOCKS) > limit:
        k, v = next(iter(_LOCKS.items()))
        if v.locked():          # 正在用的锁不能丢
            _LOCKS.move_to_end(k)
            break
        _LOCKS.popitem(last=False)
    return lk


def conversation_lock(conv_key: str | None, conv_id: str) -> asyncio.Lock:
    """给管理接口用：拿到和 prepare() 同一把会话锁。

    prepare() 是在知道 conv_id 之前就要上锁的（上锁才能安全地查会话），
    所以锁键用的是 conv_key。管理接口只有 conv_id，从会话表里回查 conv_key 即可对上。
    """
    return _lock_for(conv_key or f"cid:{conv_id}")


# ===== 此刻真的有请求在处理的会话 =====
# 不能拿 checkpoint 的 partial 状态回答"是不是正在压缩"：
# partial 是"上次没压完，下次请求接着压"的**静止**状态，可能停在那里好几天——
# 单请求批次上限没压完、摘要模型报错、中转站返回错误页、进程重启，都会留下 partial。
# 把它当成"压缩中"，页面就会永久显示压缩中、摘要永远不让改（这正是用户遇到的现象）。
# 真正的"正在跑"只有进程自己知道：请求进来时登记，走完就销号。
_INFLIGHT: dict[str, float] = {}


@contextlib.contextmanager
def _mark_inflight(key: str):
    _INFLIGHT[key] = time.time()
    try:
        yield
    finally:
        _INFLIGHT.pop(key, None)


def is_busy(conv_key: str | None, conv_id: str) -> bool:
    """这个会话此刻是否有请求正在处理（键与 conversation_lock 一致）。"""
    return (conv_key or f"cid:{conv_id}") in _INFLIGHT


def busy_count() -> int:
    return len(_INFLIGHT)


# ===== 组装 =====
def _summary_message(summary_text: str, *, branch_warning: bool) -> dict:
    p = config.prompts()
    # 两种写法都认：新配置用 {{summary}}，和摘要提示词的 {{context}} 保持一致；
    # 旧配置里的单花括号 {summary} 继续有效，升级不用改文件
    body = (p["injection"].replace("{{summary}}", summary_text.strip())
                          .replace("{summary}", summary_text.strip()))
    if branch_warning:
        body = body.rstrip() + "\n" + p["fallback_notice"].strip()
    # SUMMARY_TAG 前缀让下一轮请求能认出并剥掉自己注入的内容
    return {"role": str(config.summary().get("summary_role", "system") or "system"),
            "content": f"{M.SUMMARY_TAG}\n{body}"}


def _assemble(head: list[dict], summary_text: str, retained: list[dict],
              *, branch_warning: bool = False) -> list[dict]:
    """固定为 [开头 system] + [摘要] + [近期原文]。

    摘要放 system（可用 summary_role 改成 user）：模型对 system 的"这是背景设定"
    预期最强，放 assistant 会被当成自己说过的话而反复引用，放 user 则容易被当成新指令。
    位置固定在原文之前而不是贴着最后一条消息，是为了让请求前缀保持稳定——
    摘要只在压缩事件发生时才变，中间若干轮都能命中上游的 prefix cache。
    """
    out = list(head)
    if summary_text.strip():
        out.append(_summary_message(summary_text, branch_warning=branch_warning))
    elif branch_warning:
        # 兜底但连摘要都没有（例如未启用持久化）：警告本身仍然必须注入
        out.append({"role": str(config.summary().get("summary_role", "system") or "system"),
                    "content": f"{M.BRANCH_TAG}\n{config.prompts()['fallback_notice'].strip()}"})
    out.extend(retained)
    return out


def _round_span(rounds: list[tuple[int, int]], start: int, end: int) -> str:
    if not rounds:
        return "-"
    return f"第 {M.round_of_index(rounds, start)}~{max(1, M.rounds_before(rounds, end))} 轮"


def _pick_keep_from(rounds: list[tuple[int, int]], infos: list[M.MsgInfo],
                    already: int, keep_recent: int) -> int:
    """从尾部往前累加，凑够 keep_recent 就在**轮边界**上切。"""
    acc = 0
    keep_from = rounds[-1][0] if rounds else len(infos)
    for start, end in reversed(rounds):
        if end <= already:
            break
        seg_start = max(start, already)
        acc += M.tokens_of(infos, seg_start, end)
        keep_from = seg_start
        if acc >= keep_recent:
            break
    return max(already, keep_from)


def _floor_retain(rounds: list[tuple[int, int]], infos: list[M.MsgInfo],
                  upto: int, keep_recent: int, conv_id: str = "?") -> int:
    """近期原文是**硬性下限**：只要这个会话压缩过，转发给上游的逐字原文就不得少于
    ``keep_recent_tokens``（除非整个对话本身就没这么长）。

    正常情况下"已压缩位置"天然满足——压缩切点本来就是按这个下限挑的。但有三种情况
    会让存量的 ``compressed_upto`` 不再满足，此时必须把原文窗口**往回退**：

    * 用户删掉了近期若干轮，已压缩位置一下子逼近消息列表末尾；
    * 运行中调大了 ``keep_recent_tokens``，存量 checkpoint 的切点是按旧值定的；
    * 旧库迁移来的 ``compressed_upto`` 出自另一套阈值。

    回退意味着这几轮同时出现在摘要和原文里。重叠是无害的（提示词里明确以原文为准），
    而原文不足是有害的。**只回退、不前推**，所以绝不会把尚未摘要的内容吞掉；
    等下一次真正触发压缩时切点重算，重叠自动消失。
    """
    floor = _pick_keep_from(rounds, infos, 0, keep_recent)
    if floor >= upto:
        return upto
    log.warning("[%s] 近期原文只剩 %d tokens(<%d 硬下限)，原文窗口从下标 %d 回退到 %d"
                "（第 %d 轮起），这几轮会同时出现在摘要和原文里；"
                "常见原因：用户删了近期消息 / 调大了 keep_recent_tokens / 旧库迁移的切点",
                conv_id[:12], M.tokens_of(infos, upto, len(infos)), keep_recent,
                upto, floor, M.rounds_before(rounds, floor) + 1)
    return floor


def _build_batches(rounds: list[tuple[int, int]], infos: list[M.MsgInfo],
                   already: int, keep_from: int, batch_limit: int) -> list[tuple[int, int]]:
    """把 [already, keep_from) 按轮切成若干批，单批不超过 batch_limit（单轮超限则独立成批）。"""
    segs: list[tuple[int, int]] = []
    for start, end in rounds:
        s, e = max(start, already), min(end, keep_from)
        if s < e:
            segs.append((s, e))
    batches: list[tuple[int, int]] = []
    cur_s: int | None = None
    cur_e = 0
    cur_tok = 0
    for s, e in segs:
        t = M.tokens_of(infos, s, e)
        if cur_s is not None and cur_tok + t > batch_limit:
            batches.append((cur_s, cur_e))
            cur_s, cur_tok = None, 0
        if cur_s is None:
            cur_s = s
        cur_e = e
        cur_tok += t
    if cur_s is not None:
        batches.append((cur_s, cur_e))
    return batches


# ===== 入口 =====
async def prepare(messages: list[dict], provider: dict[str, Any],
                  on_event: OnEvent | None = None) -> Prepared:
    s = config.summary()
    if not s.get("enabled", True):
        final, stats = M.sanitize_for_upstream(messages, provider.get("multimodal", True))
        return Prepared(final, {"mode": "disabled", "sanitize": stats})

    head, body = M.split_head_system(messages)
    if not body:
        final, stats = M.sanitize_for_upstream(messages, provider.get("multimodal", True))
        return Prepared(final, {"mode": "no_body", "sanitize": stats})

    key = M.conv_key(body)
    # 登记整段持锁期间：管理接口据此判断"现在改摘要会不会和请求打架"。
    # 透传请求也算在内，但它只占几毫秒，不会像 partial 那样把页面卡死。
    with _mark_inflight(key):
        async with _lock_for(key):
            return await _prepare_locked(head, body, key, provider, on_event)


async def _prepare_locked(head: list[dict], body: list[dict], key: str,
                          provider: dict[str, Any], on_event: OnEvent | None) -> Prepared:
    s = config.summary()
    st = store.get()
    t0 = time.time()

    # ---- 一次性算出全部派生信息，全流程只编码一次 ----
    # 几千条消息首次编码要几百毫秒（之后命中 token 缓存只要几十毫秒）。
    # tiktoken 编码时会释放 GIL，丢进线程能让事件循环在这期间继续服务其它请求。
    infos = (await asyncio.to_thread(M.analyze, body)) if len(body) > 200 else M.analyze(body)
    cur_sig = M.signature(infos)
    rounds = M.split_rounds(body)
    total_rounds = len(rounds)
    head_tokens = M.count_tokens(head)
    raw_tokens = head_tokens + M.tokens_of(infos, 0, len(infos))

    # ---- 定位会话 ----
    anchors = M.pick_anchors(infos)
    legacy_id = M.legacy_conv_id(body)
    cands = await locate.candidates(st, key, anchors, legacy_id)

    located = locate.Located(mode="new", conv_id=None, checkpoint=None, already=0, summary="")
    match_mode = "new"
    empty_conv_id: str | None = None      # 匹配上但还没有任何 checkpoint 的会话，复用它的 id
    for conv_id, mode, score in cands:
        cks = await st.load_checkpoints(conv_id, int(s.get("checkpoint_keep", 10)) + 4)
        if not cks:
            if empty_conv_id is None:
                empty_conv_id, match_mode = conv_id, mode
            continue
        cand = locate.choose_checkpoint(cks, cur_sig, body, rounds)
        if cand.mode == "ok":
            cand.conv_id = conv_id
            cand.match_score = score
            located, match_mode = cand, mode
            break
        # 记住第一个候选的兜底信息（分叉轮次）
        if located.mode == "new":
            cand.conv_id = conv_id
            located, match_mode = cand, mode

    conv_id = located.conv_id or empty_conv_id
    already = max(0, min(located.already, len(body)))
    prev_summary = located.summary or ""
    is_fallback = located.mode == "fallback"
    keep_recent = config.keep_recent_tokens()      # 已按 trigger 的 50% 封顶
    # 已压缩位置只决定"还要摘要什么"；实际发出去的原文从 retain_from 起，受硬下限保护
    retain_from = _floor_retain(rounds, infos, already, keep_recent, conv_id or "new")

    for note in located.notes:
        log.info("[%s] %s", (conv_id or "new")[:12], note)
    if located.fork_round is not None and located.mode == "ok":
        log.info("[%s] 检测到历史在第 %d 轮（下标 %s）之后分叉，回退到 checkpoint seq=%s"
                 "（已压缩到第 %d 轮 / 下标 %d）", (conv_id or "?")[:12], located.fork_round,
                 located.fork_index, (located.checkpoint or {}).get("seq"),
                 M.rounds_before(rounds, already), already)

    # 旧库迁移来的 checkpoint 没有指纹数组，分支检测对它是瞎的（只能靠一条边界指纹弱校验）。
    # 认亲成功后立刻把当前指纹数组补上——否则这个会话要等到下一次真正触发压缩才有指纹，
    # 而"原文窗口回退"期间可能很久都不触发压缩，这段时间里用户改早期消息是检测不到的。
    ck0 = located.checkpoint
    if (located.mode == "ok" and conv_id and ck0 is not None and st.enabled
            and store.checkpoint_signature(ck0) is None):
        await st.resume_event(int(ck0["id"]), already, M.rounds_before(rounds, already),
                              total_rounds, len(body), cur_sig)
        ck0["signature"] = cur_sig
        log.info("[%s] 旧库迁移的 checkpoint seq=%s 补写指纹数组（%d 条），"
                 "从下次请求起可正常检测分叉", conv_id[:12], ck0.get("seq"), len(cur_sig))

    # ---- 阈值判断（用压缩后的等效总量，而不是全量原文）----
    trigger = int(s["trigger_tokens"])
    cap = int(s.get("summary_total_cap_tokens", 12800))
    summary_tokens = M.text_tokens(prev_summary)
    tail_tokens = M.tokens_of(infos, retain_from, len(infos))
    effective = head_tokens + summary_tokens + tail_tokens

    base_meta = {
        "conv_id": conv_id, "match": match_mode, "score": located.match_score,
        "rounds": total_rounds, "messages": len(body),
        "raw_tokens": raw_tokens, "effective_tokens": effective,
        "compressed_upto": already, "round_upto": M.rounds_before(rounds, already),
        "fork_round": located.fork_round,
        "body_tokens": M.tokens_of(infos, 0, len(infos)),
        "retain_from": retain_from, "retained_tokens": tail_tokens,
        "keep_recent_floor": keep_recent,
    }

    if effective < trigger:
        if conv_id:
            await st.touch(conv_id, key, "reuse")
        if prev_summary and not is_fallback:
            log.info("[%s] 无需压缩：等效 %d tokens < %d｜复用摘要(%d tokens)｜"
                     "保留第 %d~%d 轮共 %d 条原文", (conv_id or "?")[:12], effective, trigger,
                     summary_tokens, M.rounds_before(rounds, retain_from) + 1, total_rounds,
                     len(body) - retain_from)
            final = _assemble(head, prev_summary, body[retain_from:])
            return _finish(final, provider, {**base_meta, "mode": "reuse"}, trigger, t0,
                           rounds=rounds, infos=infos, body=body, total_rounds=total_rounds)
        log.info("[%s] 无需压缩：等效 %d tokens < %d｜共 %d 轮 %d 条原文，原样转发",
                 (conv_id or "new")[:12], effective, trigger, total_rounds, len(body))
        final = _assemble(head, "", body)
        return _finish(final, provider, {**base_meta, "mode": "passthrough"}, trigger, t0,
                       rounds=rounds, infos=infos, body=body, total_rounds=total_rounds)

    # ---- 超阈值：必须压缩 ----
    if is_fallback:
        return await _fallback_path(head, body, infos, rounds, cur_sig, key, conv_id,
                                    located, provider, base_meta, trigger, t0)

    if conv_id is None:
        conv_id = uuid.uuid4().hex
        await st.create_conversation(conv_id, key, legacy_id)
        log.info("[%s] 新会话建档：%d 轮 / %d 条 / 等效 %d tokens",
                 conv_id[:12], total_rounds, len(body), effective)
        base_meta["conv_id"] = conv_id
    else:
        await st.touch(conv_id, key, "compress")

    keep_from = _pick_keep_from(rounds, infos, already, keep_recent)
    batches = _build_batches(rounds, infos, already, keep_from,
                             int(s.get("summary_batch_tokens", 10000)))

    # 没有可压的新内容：要么复用摘要，要么只需要二次重压
    if not batches:
        if summary_tokens > cap:
            new_summary, model, parts = await _run_recompress(
                st, conv_id, prev_summary, already, rounds, total_rounds, len(body),
                cur_sig, on_event)
            prev_summary = new_summary
            base_meta["recompress"] = {"model": model, "parts": parts}
        log.info("[%s] 等效 %d tokens 超阈值但近期原文仅 %d tokens(<=%d)，复用摘要，保留 %d 条",
                 conv_id[:12], effective, tail_tokens, keep_recent, len(body) - retain_from)
        final = _assemble(head, prev_summary, body[retain_from:])
        return _finish(final, provider, {**base_meta, "mode": "reuse_over_trigger"}, trigger, t0,
                       rounds=rounds, infos=infos, body=body, total_rounds=total_rounds)

    # ---- 开启 / 续接压缩事件 ----
    ck = located.checkpoint
    resuming = bool(ck and ck.get("status") == "partial")
    if resuming:
        ckpt_id, seq = int(ck["id"]), int(ck["seq"])
        await st.resume_event(ckpt_id, already, M.rounds_before(rounds, already),
                              total_rounds, len(body), cur_sig)
        log.info("[%s] 续接上次未完成的压缩事件 seq=%d（已压到第 %d 轮 / 下标 %d）",
                 conv_id[:12], seq, M.rounds_before(rounds, already), already)
    else:
        ev = await st.open_event(conv_id, "incremental", prev_summary, already,
                                 M.rounds_before(rounds, already), total_rounds,
                                 len(body), cur_sig)
        ckpt_id, seq = (int(ev["id"]), int(ev["seq"])) if ev else (0, 0)

    max_batches = max(1, int(s.get("max_batches_per_request", 4)))
    planned = len(batches)
    todo = batches[:max_batches]
    to_compress_tokens = M.tokens_of(infos, already, keep_from)
    log.info("[%s] 增量压缩 seq=%d：等效 %d tokens｜待压 %s（下标 %d~%d，%d 条，约 %d tokens）"
             "｜保留 %d 条原文｜共 %d 批，本次压 %d 批",
             conv_id[:12], seq, effective, _round_span(rounds, already, keep_from),
             already, keep_from, keep_from - already, to_compress_tokens,
             len(body) - keep_from, planned, len(todo))

    summary_text = prev_summary
    done_upto = already
    used_models: list[str] = []
    failure: summarizer.SummaryFailure | None = None

    for bi, (bs, be) in enumerate(todo):
        if on_event:
            await on_event("compress", bi + 1, len(todo))
        batch_in = M.tokens_of(infos, bs, be)
        try:
            text, model = await summarizer.summarize_batch(body[bs:be])
        except summarizer.SummaryFailure as e:
            failure = e
            log.error("[%s] 第 %d/%d 批摘要失败（%s）：%s｜已落盘进度保留到第 %d 轮 / 下标 %d",
                      conv_id[:12], bi + 1, len(todo), e.kind, e.message,
                      M.rounds_before(rounds, done_upto), done_upto)
            break
        summary_text = (summary_text.rstrip() + "\n\n" + text.strip()).strip() if summary_text else text.strip()
        done_upto = be
        round_upto = M.rounds_before(rounds, be)
        await st.flush(ckpt_id, summary_text, be, round_upto)      # 摘要 + 位置同事务落盘
        used_models.append(model)
        log.info("[%s] 第 %d/%d 批完成：%s（下标 %d~%d，%d 条）｜输入 %d tokens → 输出 %d tokens"
                 "｜累积摘要 %d tokens｜模型 %s｜已落盘至第 %d 轮",
                 conv_id[:12], bi + 1, len(todo), _round_span(rounds, bs, be), bs, be, be - bs,
                 batch_in, M.text_tokens(text), M.text_tokens(summary_text), model, round_upto)

    completed = failure is None and len(todo) == planned

    # ---- 二次重压 ----
    if completed and M.text_tokens(summary_text) > cap:
        await st.seal(ckpt_id, cur_sig, len(body), total_rounds, int(s.get("checkpoint_keep", 10)))
        new_summary, model, parts = await _run_recompress(
            st, conv_id, summary_text, done_upto, rounds, total_rounds, len(body), cur_sig, on_event)
        summary_text = new_summary
        base_meta["recompress"] = {"model": model, "parts": parts}
    elif completed:
        await st.seal(ckpt_id, cur_sig, len(body), total_rounds, int(s.get("checkpoint_keep", 10)))

    await _index_anchors(st, conv_id, infos)

    meta = {**base_meta, "mode": "compress", "seq": seq, "batches_planned": planned,
            "batches_done": len(used_models), "models": sorted(set(used_models)),
            "compressed_upto": done_upto, "round_upto": M.rounds_before(rounds, done_upto),
            "summary_tokens": M.text_tokens(summary_text), "resumed": resuming}

    if failure is not None:
        remaining = M.tokens_of(infos, done_upto, keep_from)
        raise CompressionRefused(
            f"摘要模型调用失败（{failure.kind}）：{failure.message[:400]}",
            {**meta, "total_rounds": total_rounds,
             "remaining_rounds": max(0, M.rounds_before(rounds, keep_from) - meta["round_upto"]),
             "remaining_tokens": remaining, "progress_saved": True})

    if not completed:
        log.warning("[%s] 达到单请求批次上限（%d/%d 批），带着已落盘进度返回，下次请求继续",
                    conv_id[:12], len(todo), planned)

    retain_final = _floor_retain(rounds, infos, done_upto, keep_recent, conv_id)
    meta["retain_from"] = retain_final
    meta["retained_tokens"] = M.tokens_of(infos, retain_final, len(infos))
    final = _assemble(head, summary_text, body[retain_final:])
    return _finish(final, provider, meta, trigger, t0, rounds=rounds, infos=infos,
                   keep_from=keep_from, total_rounds=total_rounds, body=body)


async def _index_anchors(st, conv_id: str, infos: list[M.MsgInfo]) -> None:
    """把合格消息（≥20 字）的指纹增量写进倒排表，供锚点匹配使用。"""
    if not st.enabled or not conv_id:
        return
    conv = await st.get_conversation(conv_id)
    watermark = int((conv or {}).get("indexed_upto") or 0)
    if watermark > len(infos):
        watermark = 0
    fps = [i.fp for i in infos[watermark:] if i.chars >= M.MIN_ANCHOR_CHARS]
    if fps:
        await st.index_fps(conv_id, fps, len(infos))


async def _run_recompress(st, conv_id: str, summary_text: str, upto: int,
                          rounds: list[tuple[int, int]], total_rounds: int, msg_count: int,
                          cur_sig: list[str], on_event: OnEvent | None) -> tuple[str, str, int]:
    """二次重压：结果写成**新的** checkpoint，不覆盖旧的。"""
    before = M.text_tokens(summary_text)
    round_upto = M.rounds_before(rounds, upto)
    log.info("[%s] 触发二次重压：累积摘要 %d tokens，覆盖第 1~%d 轮",
             conv_id[:12], before, round_upto)

    async def progress(cur: int, total: int) -> None:
        if on_event:
            await on_event("recompress", cur, total)

    if on_event:
        await on_event("recompress", 0, 0)
    new_summary, model, parts = await summarizer.recompress(summary_text, progress)

    ev = await st.open_event(conv_id, "recompress", new_summary, upto, round_upto,
                             total_rounds, msg_count, cur_sig)
    if ev:
        await st.flush(int(ev["id"]), new_summary, upto, round_upto)
        await st.seal(int(ev["id"]), cur_sig, msg_count, total_rounds,
                      int(config.summary().get("checkpoint_keep", 10)))
    log.info("[%s] 二次重压完成：%d → %d tokens（%d 片，模型 %s），覆盖第 1~%d 轮，写入新 checkpoint",
             conv_id[:12], before, M.text_tokens(new_summary), parts, model, round_upto)
    return new_summary, model, parts


async def _fallback_path(head, body, infos, rounds, cur_sig, key, conv_id, located,
                         provider, base_meta, trigger, t0) -> Prepared:
    """定位彻底失败的兜底。

    用户从早已滑出窗口的早期消息处改了输入，所有 checkpoint 的已压缩区都被改动波及。
    这时取「永久置顶的最早那条 checkpoint」的摘要，原文只留 keep_recent 对应的最近若干轮，
    其余全部标记为已压缩，并在注入的摘要里显式声明衔接处可能重叠或跳跃。

    这条路径每次触发都记 WARNING + 计数，在 /health 里暴露。
    它频繁触发说明上面的定位逻辑有 bug，不该指望兜底扛。
    """
    st = store.get()
    s = config.summary()
    keep_recent = config.keep_recent_tokens()
    total_rounds = len(rounds)

    pinned = await st.load_pinned(conv_id) if conv_id else None
    summary_text = (pinned or {}).get("summary") or ""
    keep_from = _pick_keep_from(rounds, infos, 0, keep_recent)
    count = await st.bump_fallback(conv_id) if conv_id else 0

    log.warning("[%s] ⚠️ 定位兜底触发（第 %d 次）：分叉轮次 %s，所有 checkpoint 的已压缩区都被改动，"
                "改用置顶的最早 checkpoint 摘要(%d tokens)，把第 1~%d 轮全部标记为已压缩，"
                "只保留第 %d~%d 轮原文",
                (conv_id or "?")[:12], count, located.fork_round,
                M.text_tokens(summary_text), M.rounds_before(rounds, keep_from),
                M.rounds_before(rounds, keep_from) + 1, total_rounds)

    if conv_id and st.enabled:
        ev = await st.open_event(conv_id, "fallback", summary_text, keep_from,
                                 M.rounds_before(rounds, keep_from), total_rounds,
                                 len(body), cur_sig)
        if ev:
            await st.flush(int(ev["id"]), summary_text, keep_from, M.rounds_before(rounds, keep_from))
            await st.seal(int(ev["id"]), cur_sig, len(body), total_rounds,
                          int(s.get("checkpoint_keep", 10)))
        await st.touch(conv_id, key, "fallback")
        await _index_anchors(st, conv_id, infos)

    final = _assemble(head, summary_text, body[keep_from:], branch_warning=True)
    meta = {**base_meta, "mode": "fallback", "fallback_count": count,
            "compressed_upto": keep_from, "round_upto": M.rounds_before(rounds, keep_from),
            "retain_from": keep_from,
            "retained_tokens": M.tokens_of(infos, keep_from, len(infos))}
    return _finish(final, provider, meta, trigger, t0, rounds=rounds, infos=infos,
                   keep_from=keep_from, total_rounds=total_rounds, body=body)


def build_timeline(body: list[dict], infos: list[M.MsgInfo], rounds: list[tuple[int, int]],
                   meta: dict[str, Any], provider_name: str) -> dict[str, Any]:
    """一次请求的结构快照：逐轮的角色、token、预览，以及这一轮是被折叠还是逐字发出。

    **只存结构 + 每轮开头 preview_chars 字（默认 60）的预览，不存完整原文。**
    存全量 payload 意味着每次请求写几十 MB，小机器上磁盘和 IO 都扛不住，
    而且那是用户对话的完整副本，落盘本身就是风险。注意预览是未脱敏的真实片段。
    这里每轮约 100 字节，几千轮也就几百 KB，且每个会话只保留最新一份（覆盖写）。
    """
    n = int(config.observability().get("preview_chars", 60))
    # 显式判 None：retain_from 合法取 0（还没压过 / 已回退到全量原文），
    # 用 or 会把 0 当缺失，误退回 compressed_upto，时间轴上就会多标一段"已压缩"
    rf = meta.get("retain_from")
    if rf is None:
        rf = meta.get("compressed_upto")
    retain_from = int(rf or 0)
    out_rounds = []
    for r, (s, e) in enumerate(rounds):
        # 一轮里挑第一条 user 做预览，没有就用这一轮的第一条
        head = next((body[i] for i in range(s, e) if body[i].get("role") == "user"), body[s])
        out_rounds.append({
            "r": r + 1, "s": s, "e": e,
            "t": M.tokens_of(infos, s, e),
            "roles": "".join((infos[i].role or "?")[0] for i in range(s, e))[:24],
            "p": M.message_text(head)[:n],
            "c": 1 if e <= retain_from else 0,          # 1 = 这一轮已被折叠进摘要
        })
    return {
        "at": time.time(), "provider": provider_name, "mode": meta.get("mode"),
        "in": {"messages": meta.get("messages"), "tokens": meta.get("raw_tokens")},
        "out": {"messages": len(meta.get("_final_roles") or []) or None,
                "tokens": meta.get("final_tokens")},
        "out_roles": meta.get("_final_roles"),
        "summary_tokens": meta.get("summary_tokens"),
        "retain_from": retain_from, "round_upto": meta.get("round_upto"),
        "total_rounds": len(rounds), "gate_tokens": meta.get("gate_tokens"),
        "rounds": out_rounds,
    }


def _finish(final: list[dict], provider: dict[str, Any], meta: dict[str, Any],
            trigger: int, t0: float, *, rounds=None, infos=None, keep_from=None,
            total_rounds=None, body=None) -> Prepared:
    """净化 + 出口闸门。"""
    multimodal = bool(provider.get("multimodal", True))
    final, stats = M.sanitize_for_upstream(final, multimodal)
    if stats:
        log.info("转发前净化：%s", stats)
    final_tokens = M.count_tokens(final)
    ratio = float(config.summary().get("exit_gate_ratio", 1.2))
    gate = int(trigger * ratio)
    meta.update({"final_tokens": final_tokens, "gate_tokens": gate,
                 "sanitize": stats, "elapsed_ms": int((time.time() - t0) * 1000)})

    # 不变量兜底：只要还有原文可留，发出去的近期原文就不该低于硬下限。
    # 结构上已经由 _floor_retain 保证，这里只做告警型自检，抓漏网的 bug 路径。
    floor = int(meta.get("keep_recent_floor") or 0)
    kept, avail = meta.get("retained_tokens"), meta.get("body_tokens")
    if kept is not None and avail is not None and kept < floor and kept < avail:
        log.error("近期原文下限被破坏：只留了 %d tokens（下限 %d，全部原文 %d，mode=%s）"
                  "——这是 bug，请带日志反馈", kept, floor, avail, meta.get("mode"))

    if final_tokens > gate and meta.get("mode") != "disabled":
        remaining_rounds = None
        remaining_tokens = None
        if rounds is not None and infos is not None and keep_from is not None:
            remaining_rounds = max(0, M.rounds_before(rounds, keep_from) - int(meta.get("round_upto") or 0))
            remaining_tokens = M.tokens_of(infos, int(meta.get("compressed_upto") or 0), keep_from)
        planned, done = meta.get("batches_planned"), meta.get("batches_done")
        cause = "batch_cap" if (planned and done is not None and done < planned) else "oversize_tail"
        extra: dict[str, Any] = {"cause": cause}
        if cause == "oversize_tail":
            # 近期原文已经按 trigger 的 50% 封顶，还顶穿闸门就只剩两种可能：
            # 最后一轮本身太大，或者这一轮里塞了工具调用。两个数都报出来，让用户能对症下药。
            extra["tool_tokens"] = sum(M.tool_tokens(m) for m in final)
            if rounds and infos:
                extra["last_round_tokens"] = M.tokens_of(infos, rounds[-1][0], len(infos))
        log.error("出口闸门拦截：待转发 %d tokens > 闸门 %d（trigger %d × %.2f），mode=%s，成因=%s%s",
                  final_tokens, gate, trigger, ratio, meta.get("mode"), cause,
                  f"，其中工具调用占 {extra['tool_tokens']} tokens" if extra.get("tool_tokens") else "")
        raise CompressionRefused(
            "压缩后仍超出转发上限，为避免按全量 token 计费已拦截。",
            {**meta, **extra, "total_rounds": total_rounds or meta.get("rounds"),
             "remaining_rounds": remaining_rounds, "remaining_tokens": remaining_tokens,
             "progress_saved": True})

    log.info("转发上游：%d 条消息 / %d tokens（mode=%s，闸门 %d，耗时 %d ms）",
             len(final), final_tokens, meta.get("mode"), gate, meta["elapsed_ms"])
    if (config.observability().get("capture_timeline") and body is not None
            and rounds and infos and meta.get("conv_id")):
        meta["_final_roles"] = [str(m.get("role") or "?") for m in final]
        meta["_timeline"] = build_timeline(body, infos, rounds, meta, provider.get("name", "?"))
    return Prepared(final, meta)
