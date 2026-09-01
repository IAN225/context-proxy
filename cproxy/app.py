"""FastAPI 应用：鉴权、转发、流式平滑、管理接口。"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import signal
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse

from . import compress, config, logging_setup, probe, store, ui
from . import messages as M

log = logging.getLogger("proxy")

STARTED_AT = time.time()


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    logging_setup.install_loop_handler(asyncio.get_running_loop())
    log.info("服务启动完成，监听中")
    yield
    log.info("服务停止")


app = FastAPI(title="context-proxy", lifespan=_lifespan)


# ===== 启停 =====
def bootstrap() -> None:
    """加载配置 + 日志 + DB。导入模块时执行一次。"""
    # 先给一个最小的 stdout logger，保证连配置都读不出来时也有清晰输出
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    try:
        config.reload(warn=lambda *a: logging.getLogger("proxy").warning(*a))
    except Exception:
        log.critical("配置加载失败（检查 %s），服务无法启动", config.CONFIG_PATH, exc_info=True)
        raise
    logging_setup.setup()
    logging_setup.install_crash_handlers()
    store.init(config.db_path())
    _load_prompt_overrides()
    log.info("配置已加载：providers=%s summary=%s fallback=%s",
             list(config.providers()), config.summary().get("model"),
             (config.summary().get("fallback") or {}).get("model")
             if (config.summary().get("fallback") or {}).get("enabled") else "off")


def do_reload() -> dict[str, Any]:
    summary = config.reload(warn=log.warning)
    logging_setup.setup()
    store.init(config.db_path())
    _load_prompt_overrides()
    log.info("配置已热重载：%s", summary)
    return summary


PROMPT_OVERRIDE_KEY = "prompt_overrides"


def _load_prompt_overrides() -> None:
    """页面上改过的提示词存在 DB 里，热重载后要重新盖回去，否则会被文件里的值顶掉。"""
    st = store.get()
    if not st.enabled:
        config.set_prompt_overrides({})
        return
    try:
        raw = st._get_meta_sync(PROMPT_OVERRIDE_KEY)      # 启动路径，同步读一次即可
        data = json.loads(raw) if raw else {}
    except Exception:
        log.exception("提示词覆盖项读取失败，改用 config.yaml 里的值")
        data = {}
    config.set_prompt_overrides(data)
    if data:
        log.info("已装载页面保存的提示词覆盖项：%s", ", ".join(sorted(data)))


# ===== 鉴权 =====
# 失败计数：16 位密钥被在线暴力破解并不现实，但这个端口通常开在公网上，
# 挡一下能顺带把扫描器的日志噪音压下去。
_FAIL: "OrderedDict[str, list[float]]" = OrderedDict()
_FAIL_WINDOW = 300.0
_FAIL_MAX = 10


def _client_ip(request: Request) -> str:
    return (request.client.host if request.client else "?") or "?"


def _throttled(ip: str) -> bool:
    now = time.time()
    hits = [t for t in _FAIL.get(ip, []) if now - t < _FAIL_WINDOW]
    if hits:
        _FAIL[ip] = hits
    elif ip in _FAIL:
        del _FAIL[ip]
    return len(hits) >= _FAIL_MAX


def _record_fail(ip: str) -> None:
    _FAIL.setdefault(ip, []).append(time.time())
    _FAIL.move_to_end(ip)
    while len(_FAIL) > 1024:
        _FAIL.popitem(last=False)


def _bearer(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    return auth[7:].strip() if auth.lower().startswith("bearer ") else auth.strip()


def _accepted_tokens(admin: bool) -> list[str]:
    """列出该类接口认可的密钥。

    对话接口（admin=False）永远只认 auth_token。
    管理接口：**配了 ui_token 就只认 ui_token**，auth_token 一并失效——
    auth_token 要填进 chatbox、跟着每个对话请求走，还能开后台等于把后台钥匙散出去。
    没配 ui_token 时（页面本身也是关的）才退回 auth_token，否则 ./ctl.sh 没法调管理接口。
    """
    if admin:
        ui = config.ui_token()
        return [ui] if ui else [t for t in [config.auth_token()] if t]
    return [t for t in [config.auth_token()] if t]


def _check_auth(request: Request, *, admin: bool = False) -> JSONResponse | None:
    accepted = _accepted_tokens(admin)
    if not accepted:
        return None
    ip = _client_ip(request)
    if _throttled(ip):
        return JSONResponse(status_code=429, content={"error": {
            "message": "密钥错误次数过多，请稍后再试", "type": "auth_error"}})
    got = _bearer(request)
    # compare_digest 防时序侧信道。any() 会短路，所以先把每个都比完再看结果，
    # 让耗时不随"第几个才匹配上"变化。
    results = [hmac.compare_digest(got, t) for t in accepted]
    if any(results):
        return None
    _record_fail(ip)
    log.warning("鉴权失败：%s %s（来自 %s）", request.method, request.url.path, ip)
    return JSONResponse(status_code=401,
                        content={"error": {"message": "unauthorized", "type": "auth_error"}})


def _sse(obj: dict) -> bytes:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode("utf-8")


def _chunk(model: str, *, content: str | None = None, reasoning: str | None = None,
           finish: str | None = None) -> dict:
    delta: dict[str, Any] = {}
    if content is not None:
        delta["content"] = content
    if reasoning is not None:
        delta["reasoning_content"] = reasoning
    return {"id": "chatcmpl-proxy", "object": "chat.completion.chunk",
            "created": int(time.time()), "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


async def _save_timeline(prepared) -> None:
    tl = (prepared.meta or {}).pop("_timeline", None)
    (prepared.meta or {}).pop("_final_roles", None)
    if not tl:
        return
    try:
        await store.get().save_timeline(prepared.meta["conv_id"],
                                        json.dumps(tl, ensure_ascii=False))
    except Exception:
        log.exception("请求快照落盘失败（不影响本次转发）")


def _refusal_payload(exc: compress.CompressionRefused) -> dict:
    return {"error": {"message": exc.user_text(), "type": "context_compression_incomplete",
                      "code": "compression_incomplete", "detail": exc.detail}}


# ===== 健康检查 / 管理 =====
@app.get("/health")
async def health():
    st = store.get()
    stats = await st.stats() if st.enabled else {}
    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - STARTED_AT),
        "providers": {n: {"multimodal": p["multimodal"]} for n, p in config.providers().items()},
        "summary_model": config.summary().get("model"),
        "trigger_tokens": config.summary().get("trigger_tokens"),
        # 有效值：配置值与 trigger 的 50% 取小，两者不一致说明 config 写大了
        "keep_recent_tokens_effective": config.keep_recent_tokens(),
        "keep_recent_tokens_configured": config.summary().get("keep_recent_tokens"),
        "summary_fallback": ((config.summary().get("fallback") or {}).get("model")
                             if (config.summary().get("fallback") or {}).get("enabled") else None),
        "persist_db": st.path or "disabled",
        "legacy_migrated": st.migrated_count,
        "conversations": stats.get("conversations", 0),
        "checkpoints": stats.get("checkpoints", 0),
        # 未压完 ≠ 正在压：前者是"下次请求接着压"的静止状态，后者才是此刻真有请求在跑
        "unfinished_compressions": stats.get("unfinished_events", 0),
        "compressing_now": compress.busy_count(),
        # 兜底定位每触发一次都说明上面的定位逻辑漏了一种情况，这个数字应当长期为 0
        "fallback_activations": stats.get("fallback_activations", 0),
        "conversation_locks": len(compress._LOCKS),
    }


@app.post("/admin/reload")
async def admin_reload(request: Request):
    if (denied := _check_auth(request, admin=True)) is not None:
        return denied
    try:
        return {"status": "reloaded", **do_reload()}
    except Exception as e:
        log.exception("热重载失败，保留旧配置")
        return JSONResponse(status_code=400, content={
            "error": {"message": f"reload failed: {e}", "type": "reload_error"}})


@app.get("/admin/sessions")
async def admin_sessions(request: Request, limit: int = 100):
    if (denied := _check_auth(request, admin=True)) is not None:
        return denied
    rows = await store.get().list_conversations(limit)
    for r in rows:
        r["conv_id_short"] = r["conv_id"][:16]
        r["age_seconds"] = int(time.time() - (r.get("updated_at") or 0))
    return {"count": len(rows), "sessions": rows}


@app.get("/admin/session/{conv_id}")
async def admin_session(conv_id: str, request: Request):
    if (denied := _check_auth(request, admin=True)) is not None:
        return denied
    st = store.get()
    matched = await st.match_prefix(conv_id)
    if not matched:
        return JSONResponse(status_code=404, content={"error": {"message": "session not found"}})
    cid = matched[0]
    conv = await st.get_conversation(cid)
    cks = await st.load_checkpoints(cid, 24)
    return {
        "conversation": conv,
        "checkpoints": [{
            "seq": c["seq"], "status": c["status"], "kind": c["kind"], "pinned": c["pinned"],
            "compressed_upto": c["compressed_upto"], "round_upto": c["round_upto"],
            "total_rounds": c["total_rounds"], "msg_count": c["msg_count"],
            "signature_len": len(store.checkpoint_signature(c) or []) or None,
            "summary_tokens": M.text_tokens(c["summary"] or ""),
            # 全文走 /admin/session/{id}/summary，这里只给预览，否则十几条 checkpoint 刷屏
            "summary_preview": (c["summary"] or "")[:400],
            "updated_at": c["updated_at"],
        } for c in cks],
    }


async def _resolve_conv(conv_id: str):
    st = store.get()
    matched = await st.match_prefix(conv_id)
    if not matched:
        return None, None, JSONResponse(status_code=404, content={
            "error": {"message": f"没有匹配 {conv_id!r} 的会话（支持 conv_id 前缀）"}})
    if len(matched) > 1:
        return None, None, JSONResponse(status_code=409, content={
            "error": {"message": f"前缀 {conv_id!r} 匹配到 {len(matched)} 个会话，请写长一点",
                      "candidates": [c[:16] for c in matched]}})
    return matched[0], await st.get_conversation(matched[0]), None


def _summary_too_long(text: str) -> JSONResponse | None:
    """超过 cap 就拦下来：留着它下次压缩会触发二次重压，把人写的东西洗成模型的话。"""
    tokens = M.text_tokens(text)
    cap = int(config.summary().get("summary_total_cap_tokens", 12800))
    if tokens <= cap:
        return None
    return JSONResponse(status_code=400, content={"error": {"message":
        f"摘要 {tokens} tokens 超过 summary_total_cap_tokens={cap}，"
        "超了会在下次压缩时被自动二次重压、把你的改动洗掉。请精简后再存。"}})


@app.get("/admin/session/{conv_id}/summary")
async def get_summary(conv_id: str, request: Request, format: str = "json"):
    """取当前生效的累积摘要全文。format=text 时直接返回纯文本，便于重定向到文件编辑。"""
    if (denied := _check_auth(request, admin=True)) is not None:
        return denied
    cid, _conv, err = await _resolve_conv(conv_id)
    if err is not None:
        return err
    st = store.get()
    ck = await st.latest_checkpoint(cid)
    if ck is None:
        return JSONResponse(status_code=404, content={
            "error": {"message": "这个会话还没有任何 checkpoint（尚未触发过压缩）"}})
    if format == "text":
        return PlainTextResponse(ck["summary"] or "")
    open_ev = await st.open_event_checkpoint(cid)
    conv = await st.get_conversation(cid)
    busy = compress.is_busy((conv or {}).get("conv_key"), cid)
    return {
        "conv_id": cid,
        "base_seq": ck["seq"],          # 回写时带上它做乐观并发校验
        "kind": ck["kind"], "status": ck["status"],
        "pinned": ck.get("pinned", 0),
        "compressed_upto": ck["compressed_upto"], "round_upto": ck["round_upto"],
        "total_rounds": ck["total_rounds"],
        "summary_tokens": M.text_tokens(ck["summary"] or ""),
        "summary_cap_tokens": config.summary().get("summary_total_cap_tokens"),
        # 能不能改，只取决于此刻有没有请求正在跑。
        # 存在未压完的事件（partial）不是"压缩中"——那是静止状态，可能停在那儿好几天，
        # 拿它当忙，页面就会永久不让编辑。保存时会把这些半成品作废掉。
        "editable": not busy,
        "busy": busy,
        "unfinished_event_seq": open_ev["seq"] if open_ev else None,
        "summary": ck["summary"] or "",
    }


@app.put("/admin/session/{conv_id}/summary")
async def put_summary(conv_id: str, request: Request):
    """手工改写累积摘要。写成一条新的 manual checkpoint，原来那条留着可回退。

    body: {"summary": "...", "base_seq": N}
    base_seq 是乐观锁：取回摘要之后如果又发生过压缩，seq 会变，这里直接拒绝，
    避免把模型刚压出来的新内容覆盖掉。
    """
    if (denied := _check_auth(request, admin=True)) is not None:
        return denied
    payload = await request.json()
    text = payload.get("summary")
    if not isinstance(text, str) or not text.strip():
        return JSONResponse(status_code=400, content={
            "error": {"message": "summary 不能为空；想清空整个会话状态请用 /admin/clean"}})
    text = text.strip()

    cid, conv, err = await _resolve_conv(conv_id)
    if err is not None:
        return err
    st = store.get()

    # 和压缩流程抢同一把会话锁，避免和正在跑的压缩交错写。
    # 拿到锁就说明没有请求在压这个会话了，不需要再看 checkpoint 是不是 partial——
    # partial 只表示"上次没压完"，是个可以停留很久的静止状态。
    async with compress.conversation_lock((conv or {}).get("conv_key"), cid):
        ck = await st.latest_checkpoint(cid)
        if ck is None:
            return JSONResponse(status_code=404, content={
                "error": {"message": "这个会话还没有任何 checkpoint（尚未触发过压缩）"}})
        base_seq = payload.get("base_seq")
        if base_seq is not None and int(base_seq) != int(ck["seq"]):
            return JSONResponse(status_code=409, content={"error": {"message":
                f"摘要已被更新（你基于 seq={base_seq}，当前是 seq={ck['seq']}），"
                "请重新取一次再改，以免覆盖掉新压出来的内容。"}})

        if (err2 := _summary_too_long(text)) is not None:
            return err2
        tokens = M.text_tokens(text)

        # pin=True：这条从此就是**当前生效**的摘要——标成不可裁剪，
        # 并把未压完的半成品作废掉，免得下次请求绕回那条 seq 更小的继续追加
        seq = await st.add_manual_checkpoint(
            cid, int(ck["id"]), text, int(config.summary().get("checkpoint_keep", 10)), pin=True)
    old_tokens = M.text_tokens(ck["summary"] or "")
    log.warning("[%s] 摘要被手工改写：seq %s -> %s（%d -> %d tokens），"
                "位置信息沿用第 %s 轮 / 下标 %s，已置为当前摘要，原 checkpoint 保留可回退",
                cid[:12], ck["seq"], seq, old_tokens, tokens,
                ck["round_upto"], ck["compressed_upto"])
    return {"status": "updated", "conv_id": cid, "new_seq": seq, "base_seq": ck["seq"],
            "summary_tokens": tokens, "previous_summary_tokens": old_tokens,
            "note": "下一次请求即生效；已固定为当前 checkpoint，重启后仍然是它"}


@app.post("/admin/session/{conv_id}/checkpoint/{seq}/activate")
async def activate_checkpoint(conv_id: str, seq: int, request: Request):
    """把某条 checkpoint 的**摘要正文**设为当前生效的摘要。

    body（都可省）::

        {"rewind": false}   # true = 连压缩进度一起退回那条的位置，重压中间那段

    默认 ``rewind=false``，也就是**只换内容、不动进度**：
    压缩标记（compressed_upto）和摘要内容是两件正交的事——
    已经压到第 1400 条，用户挑了一条覆盖到第 800 条的旧摘要设为生效，
    他要的是"这 1400 条对应的摘要换成这一份"，不是让进度倒回 800 去重压。
    新的 checkpoint 因此拿当前生效那条的位置 + 选中那条的正文。

    代价是 801~1400 这段既不在摘要里、也不在近期原文窗口里，**成了记忆空洞**。
    所以响应里明确给出 ``gap_rounds``，页面会在按钮上就把这个数字标出来，
    让用户自己决定是先把这段补进摘要，还是选 ``rewind=true`` 花钱重压。
    """
    if (denied := _check_auth(request, admin=True)) is not None:
        return denied
    try:
        payload = await request.json()
    except Exception:                                  # noqa: BLE001 允许空 body
        payload = {}
    rewind = bool((payload or {}).get("rewind"))

    cid, conv, err = await _resolve_conv(conv_id)
    if err is not None:
        return err
    st = store.get()
    async with compress.conversation_lock((conv or {}).get("conv_key"), cid):
        target = await st.checkpoint_by_seq(cid, int(seq))
        if target is None:
            return JSONResponse(status_code=404, content={
                "error": {"message": f"会话 {cid[:12]} 没有 seq={seq} 的 checkpoint"}})
        text = (target["summary"] or "").strip()
        if not text:
            return JSONResponse(status_code=400, content={
                "error": {"message": f"seq={seq} 的摘要是空的，设为当前等于把早期记忆全丢掉"}})
        if (err2 := _summary_too_long(text)) is not None:
            return err2
        latest = await st.latest_checkpoint(cid)
        # rewind=False：位置沿用当前生效的那条（进度不倒退）；True：连位置一起回到 target
        pos = target if (rewind or latest is None) else latest
        new_seq = await st.add_manual_checkpoint(
            cid, int(pos["id"]), text,
            int(config.summary().get("checkpoint_keep", 10)), pin=True)

    gap = max(0, int(pos["round_upto"] or 0) - int(target["round_upto"] or 0))
    log.warning("[%s] 手工指定当前摘要：正文取 seq %s（覆盖到第 %s 轮），位置取 seq %s"
                "（第 %s 轮 / 下标 %s），写成 seq %s 并置顶%s",
                cid[:12], target["seq"], target["round_upto"], pos["seq"],
                pos["round_upto"], pos["compressed_upto"], new_seq,
                f"；第 {target['round_upto']}~{pos['round_upto']} 轮成为记忆空洞" if gap else "")
    return {"status": "activated", "conv_id": cid, "from_seq": int(seq), "new_seq": new_seq,
            "rewind": rewind,
            "compressed_upto": pos["compressed_upto"], "round_upto": pos["round_upto"],
            "summary_covers_round": target["round_upto"],
            "gap_rounds": gap,
            "summary_tokens": M.text_tokens(text),
            "note": ("已固定为当前 checkpoint：下一次请求即用它发送，后续压缩也在它之上继续"
                     + (f"。注意第 {target['round_upto'] + 1}~{pos['round_upto']} 轮"
                        f"（{gap} 轮）既不在这份摘要里、也不在近期原文窗口里，"
                        "需要的话请把这段内容补进摘要，或用 rewind=true 回退进度重压"
                        if gap else ""))}


@app.get("/admin/session/{conv_id}/checkpoint/{seq}")
async def get_checkpoint(conv_id: str, seq: int, request: Request):
    """取某个槽位的完整摘要正文（列表接口只给预览，全文走这里）。"""
    if (denied := _check_auth(request, admin=True)) is not None:
        return denied
    cid, _conv, err = await _resolve_conv(conv_id)
    if err is not None:
        return err
    ck = await store.get().checkpoint_by_seq(cid, int(seq))
    if ck is None:
        return JSONResponse(status_code=404, content={
            "error": {"message": f"会话 {cid[:12]} 没有 seq={seq} 的 checkpoint"}})
    return {"conv_id": cid, "seq": ck["seq"], "kind": ck["kind"], "status": ck["status"],
            "pinned": ck.get("pinned", 0),
            "compressed_upto": ck["compressed_upto"], "round_upto": ck["round_upto"],
            "total_rounds": ck["total_rounds"],
            "summary_tokens": M.text_tokens(ck["summary"] or ""),
            "summary": ck["summary"] or ""}


@app.put("/admin/session/{conv_id}/checkpoint/{seq}")
async def overwrite_checkpoint(conv_id: str, seq: int, request: Request):
    """把正文写回**某个已有槽位**，位置信息和 seq 都不动。

    body::

        {"summary": "...", "activate": false}

    存档式编辑：改完存回同一个格子，而不是每存一次就往链上加一条——
    那样 checkpoint_keep 很快会把有用的旧档挤掉。
    覆盖槽位**不改变谁生效**（除非 activate=true），这是两个动作。
    """
    if (denied := _check_auth(request, admin=True)) is not None:
        return denied
    payload = await request.json()
    text = payload.get("summary")
    if not isinstance(text, str) or not text.strip():
        return JSONResponse(status_code=400, content={
            "error": {"message": "summary 不能为空"}})
    text = text.strip()
    if (err2 := _summary_too_long(text)) is not None:
        return err2

    cid, conv, err = await _resolve_conv(conv_id)
    if err is not None:
        return err
    st = store.get()
    async with compress.conversation_lock((conv or {}).get("conv_key"), cid):
        ck = await st.checkpoint_by_seq(cid, int(seq))
        if ck is None:
            return JSONResponse(status_code=404, content={
                "error": {"message": f"会话 {cid[:12]} 没有 seq={seq} 的 checkpoint"}})
        if ck["status"] == "partial":
            return JSONResponse(status_code=409, content={"error": {"message":
                f"seq={seq} 是还没压完的半成品，下次请求会继续往它里面追加内容，"
                "现在写回去会被覆盖。请存到别的槽位，或直接用「保存并设为生效」。"}})
        await st.overwrite_summary(cid, int(seq), text)
        new_seq = None
        if payload.get("activate"):
            new_seq = await st.add_manual_checkpoint(
                cid, int((await st.latest_checkpoint(cid))["id"]), text,
                int(config.summary().get("checkpoint_keep", 10)), pin=True)
    log.warning("[%s] 槽位 seq=%s 的摘要被覆盖（%d tokens）%s", cid[:12], seq,
                M.text_tokens(text), f"，并写成生效档 seq={new_seq}" if new_seq else "")
    return {"status": "overwritten", "conv_id": cid, "seq": int(seq),
            "summary_tokens": M.text_tokens(text), "activated_seq": new_seq,
            "note": ("已存回该槽位" + ("，并设为当前生效" if new_seq else
                                       "；它不是当前生效的那条，要生效请点「设为生效」"))}


PROMPT_LABELS = {
    "batch": "批次摘要：整条 user 消息发给摘要模型，{{context}} 处插入待压原文",
    "recompress": "二次重压：累积摘要超过 cap 时逐片精简，{{context}} 处插入待精简摘要",
    "injection": "注入给主模型的包装语（{{summary}} 是占位符，必须保留）",
    "fallback_notice": "定位兜底时追加的警告语",
}


@app.get("/admin/prompts")
async def get_prompts(request: Request):
    if (denied := _check_auth(request, admin=True)) is not None:
        return denied
    src = config.prompt_sources()
    return {"prompts": [{"name": k, "label": PROMPT_LABELS.get(k, k), **v}
                        for k, v in src.items()]}


@app.put("/admin/prompts")
async def put_prompts(request: Request):
    """保存提示词，**直接写回 config.yaml**（只换提示词正文，文件里的注释原样保留）。

    某条提交空字符串 = 恢复成内置默认值。
    写文件失败时（只读挂载、权限不足）退回内存覆盖项，让这次修改仍然当场生效，
    并在响应里说清楚"重启会丢"。
    """
    if (denied := _check_auth(request, admin=True)) is not None:
        return denied
    payload = await request.json()
    incoming = payload.get("prompts")
    if not isinstance(incoming, dict):
        return JSONResponse(status_code=400, content={
            "error": {"message": "body 需要 {\"prompts\": {name: text}}"}})

    to_write: dict[str, str] = {}
    for name, text in incoming.items():
        if name not in config.FALLBACK_PROMPTS:
            return JSONResponse(status_code=400, content={
                "error": {"message": f"未知的提示词 {name!r}，可用：{list(config.FALLBACK_PROMPTS)}"}})
        text = text if isinstance(text, str) else ""
        if not text.strip():
            text = config.FALLBACK_PROMPTS[name]      # 清空 = 恢复内置默认
        if (err := config.check_prompt(name, text)) is not None:
            return JSONResponse(status_code=400, content={"error": {"message": err}})
        to_write[name] = config.normalize_prompt(name, text)

    if not to_write:
        return {"status": "saved", "written": [], "target": "config.yaml"}

    try:
        changed = await asyncio.to_thread(config.write_prompts_to_file, to_write)
    except Exception as e:                            # noqa: BLE001 写不进去也得让改动生效
        log.error("提示词回写 config.yaml 失败：%s；退回内存覆盖项（重启会丢）", e)
        current = config.prompt_overrides()
        current.update(to_write)
        st = store.get()
        if st.enabled:
            await st.set_meta(PROMPT_OVERRIDE_KEY, json.dumps(current, ensure_ascii=False))
        config.set_prompt_overrides(current)
        return {"status": "saved_in_memory_only", "written": [],
                "overridden": sorted(current),
                "warning": f"写 config.yaml 失败（{e}）。改动已生效，但重启后会丢；"
                           "检查文件权限后再保存一次"}

    # 写进文件了，之前的内存覆盖项就该退场，否则它会一直盖住文件里的新值
    st = store.get()
    if st.enabled:
        await st.set_meta(PROMPT_OVERRIDE_KEY, "{}")
    config.set_prompt_overrides({})
    do_reload()
    log.warning("提示词已写回 config.yaml：%s（已备份 config.yaml.bak）", ", ".join(sorted(changed)))
    return {"status": "saved", "written": sorted(changed), "target": "config.yaml",
            "note": "已写入配置文件并热重载，重启后依然生效；原文件备份在 config.yaml.bak"}


@app.get("/admin/session/{conv_id}/timeline")
async def get_timeline(conv_id: str, request: Request):
    if (denied := _check_auth(request, admin=True)) is not None:
        return denied
    cid, _conv, err = await _resolve_conv(conv_id)
    if err is not None:
        return err
    tl = await store.get().load_timeline(cid)
    if tl is None:
        return JSONResponse(status_code=404, content={"error": {"message":
            "还没有快照。把 config.yaml 的 observability.capture_timeline 设为 true 并 reload，"
            "然后这个会话再发一次消息就有了。" if not config.observability().get("capture_timeline")
            else "这个会话在开启快照后还没有新的请求。"}})
    return tl


# ===== 控制台：压缩任务 =====

@app.get("/admin/tasks")
async def admin_tasks(request: Request):
    """此刻在跑的压缩任务，含进度和最近一批的摘要输出。

    注意这是**进程内**的实时状态，不是数据库里的。多进程部署时每个进程只看得到自己的
    （本项目默认单进程；要多开就得先把这块换成共享存储）。
    """
    if (denied := _check_auth(request, admin=True)) is not None:
        return denied
    st = store.get()
    stats = await st.stats()
    return {"running": compress.running_tasks(),
            "unfinished_compressions": stats.get("unfinished_events", 0),
            "note": "unfinished_compressions 是「上次没压完、下次请求接着压」的静止状态，"
                    "不是正在跑的任务"}


@app.post("/admin/session/{conv_id}/cancel")
async def cancel_compression(conv_id: str, request: Request):
    """中止某个会话正在跑的压缩。

    只在**批与批之间**生效：已经落盘的批一条都不撕——钱已经花了，结果留着下次能接着用。
    中止之后这次请求多半会撞上出口闸门返回 503（因为没压到阈值以下），
    这是预期行为，进度都在，重发消息就从断点继续。
    """
    if (denied := _check_auth(request, admin=True)) is not None:
        return denied
    cid, _conv, err = await _resolve_conv(conv_id)
    if err is not None:
        return err
    if not compress.request_cancel(cid):
        return JSONResponse(status_code=409, content={"error": {"message":
            f"会话 {cid[:12]} 此刻没有正在跑的压缩任务，没什么可中止的"}})
    log.warning("[%s] 收到中止请求，将在当前批次结束后停下", cid[:12])
    return {"status": "cancelling", "conv_id": cid,
            "note": "会在当前这一批压完后停下；已完成的批次全部保留，下次请求接着压"}


# ===== 控制台：模型与供应商配置 =====

def _mask_key(key: str | None) -> str:
    """密钥只回显首尾几位。页面已经能看到全部对话摘要了，别再把上游 key 也送出去。"""
    if not key:
        return ""
    if len(key) <= 10:
        return key[:2] + "***"
    return f"{key[:5]}***{key[-4:]}"


MASKED = "__unchanged__"      # 页面把掩码原样提交回来时的占位：表示"这项别动"


def _resolve_key(submitted: Any, current: str | None) -> str:
    """页面提交的 key：留空或原样回传掩码 = 沿用旧值。"""
    if not isinstance(submitted, str) or submitted == MASKED or not submitted.strip():
        return current or ""
    if submitted.strip() == _mask_key(current):
        return current or ""
    return submitted.strip()


@app.get("/admin/models")
async def get_models(request: Request):
    """控制台读取当前的供应商与摘要模型配置（密钥掩码）。"""
    if (denied := _check_auth(request, admin=True)) is not None:
        return denied
    cfg = config.cfg()
    s = cfg["summary"]
    provs = []
    for p in cfg.get("providers") or []:
        name = str(p.get("name", ""))
        live = config.provider(name) or {}
        provs.append({
            "name": name,
            "base_url": p.get("base_url", ""),
            "api_key_masked": _mask_key(live.get("api_key")),
            "has_key": bool(live.get("api_key")),
            "key_from_env": bool(os.environ.get(config.provider_env_name(name))),
            "multimodal": bool(p.get("multimodal", True)),
            "timeout_seconds": p.get("timeout_seconds", 300),
            "connect_timeout_seconds": p.get("connect_timeout_seconds", 30),
            "extra_body": p.get("extra_body") or {},
            "forward_headers": p.get("forward_headers") or [],
            "forward_query": bool(p.get("forward_query", False)),
        })
    eps = {e["tag"]: e for e in config.summary_endpoints()}
    fb = s.get("fallback") or {}
    return {
        "providers": provs,
        "summary": {
            "base_url": s.get("base_url", ""), "model": s.get("model", ""),
            "api_key_masked": _mask_key((eps.get("primary") or {}).get("api_key")),
            "has_key": "primary" in eps,
            "key_from_env": bool(os.environ.get("SUMMARY_API_KEY")),
            "extra_body": s.get("extra_body") or {},
            "summary_max_tokens": s.get("summary_max_tokens"),
            "max_tokens_field": config.max_tokens_field(),
            "timeout_seconds": s.get("timeout_seconds", 180),
            "main_max_attempts": s.get("main_max_attempts", 2),
            "min_output_tokens": s.get("min_output_tokens", 50),
        },
        "fallback": {
            "enabled": bool(fb.get("enabled")), "base_url": fb.get("base_url", ""),
            "model": fb.get("model", ""),
            "api_key_masked": _mask_key((eps.get("fallback") or {}).get("api_key")),
            "has_key": "fallback" in eps,
            "key_from_env": bool(os.environ.get("SUMMARY_FALLBACK_API_KEY")),
            "max_attempts": fb.get("max_attempts", 3),
            "extra_body": fb.get("extra_body") or None,
        },
        "max_tokens_fields": list(config.MAX_TOKENS_FIELDS),
        "protected_body_keys": list(config.PROTECTED_BODY_KEYS),
        "masked_placeholder": MASKED,
    }


@app.post("/admin/models/test")
async def test_model(request: Request):
    """拿一份**还没保存**的配置去真实调一次，把完整响应带回来。

    body::

        {"base_url": "...", "api_key": "...", "model": "...",
         "extra_body": {...}, "max_tokens_field": "max_tokens",
         "detect_dialect": false}

    为什么要回显完整响应：中转站经常把自己的故障当成模型输出发回来
    （HTTP 200 + choices 里塞一句"池子中没有可用账号"，或者干脆是 Cloudflare 的
    HTML 页面）。只看状态码会把这种当成成功。所以这里标出可疑特征，
    但**不替用户下结论**——原文摆出来，让人自己看模型到底吐了什么。
    """
    if (denied := _check_auth(request, admin=True)) is not None:
        return denied
    payload = await request.json()
    base_url = str(payload.get("base_url", "")).strip().rstrip("/")
    model = str(payload.get("model", "")).strip()
    if not base_url or not model:
        return JSONResponse(status_code=400, content={
            "error": {"message": "base_url 和 model 都得填"}})
    key = _resolve_key(payload.get("api_key"), _current_key_for(payload))
    extra = payload.get("extra_body") or {}
    if not isinstance(extra, dict):
        return JSONResponse(status_code=400, content={
            "error": {"message": "extra_body 必须是一个 JSON 对象"}})
    bad = [k for k in extra if k in config.PROTECTED_BODY_KEYS]
    if bad:
        return JSONResponse(status_code=400, content={"error": {"message":
            f"extra_body 里不能出现 {bad}：改了它们等于绕过压缩、破坏流式处理"}})

    field = str(payload.get("max_tokens_field") or "max_tokens")
    if field not in config.MAX_TOKENS_FIELDS:
        field = "max_tokens"
    timeout = float(payload.get("timeout_seconds") or 60)

    if payload.get("detect_dialect"):
        out = await probe.detect_dialect(base_url, key, model, timeout=timeout)
        return {"mode": "dialect", **out}

    url = f"{base_url}/chat/completions"
    body = probe.build_body(model, extra, max_tokens_field=field)
    async with httpx.AsyncClient(timeout=httpx.Timeout(
            connect=20.0, read=timeout, write=timeout, pool=timeout)) as client:
        res = await probe.probe(client, url, probe.headers_for(key), body, "test")
    d = res.to_dict()
    d["mode"] = "single"
    d["verdict"] = ("ok" if res.ok and not res.suspect else
                    "suspect" if res.ok else "failed")
    d["advice"] = _test_advice(res)
    return d


def _current_key_for(payload: dict) -> str | None:
    """页面只提交掩码时，从现有配置里把真 key 找回来。"""
    target = str(payload.get("target") or "")
    if target.startswith("provider:"):
        return (config.provider(target.split(":", 1)[1]) or {}).get("api_key")
    eps = {e["tag"]: e for e in config.summary_endpoints()}
    if target == "summary":
        return (eps.get("primary") or {}).get("api_key")
    if target == "fallback":
        return (eps.get("fallback") or {}).get("api_key")
    return None


def _test_advice(res: probe.ProbeResult) -> str:
    if res.suspect:
        return (f"HTTP {res.status} 看起来成功，但{res.suspect}。"
                "中转站常把自己的故障当成模型输出发回来——请点开下面的原始响应，"
                "确认这确实是模型说的话，再决定保不保存。")
    if res.ok:
        return "上游正常返回了内容。建议还是扫一眼下面的模型输出，确认不是一句报错。"
    if res.kind == probe.REJECT:
        return ("上游明确拒绝了这个请求体（400/422）。多半是 extra_body 里有它不认的字段，"
                "或者字段值不合法。照着下面的原文改。")
    return (f"这次没问出结果（{res.status}）：限流 / 鉴权 / 上游故障都会这样，"
            "跟你的参数不一定有关系。过一会儿再试一次。")


@app.put("/admin/models")
async def put_models(request: Request):
    """保存供应商与摘要模型配置，写回 config.yaml。

    **保存前会真实调一次**：调不通就不保存（宁可让用户对着报错改，也不能把一份
    坏配置写进去——写进去的下一秒对话就全挂了）。
    ``force: true`` 可以跳过这道校验（比如上游临时限流，但你确信配置是对的）。
    """
    if (denied := _check_auth(request, admin=True)) is not None:
        return denied
    payload = await request.json()
    force = bool(payload.get("force"))
    try:
        draft = _build_config_draft(payload)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": {"message": str(e)}})

    checks: list[dict] = []
    if not force:
        for target in draft["to_test"]:
            url = f"{target['base_url']}/chat/completions"
            body = probe.build_body(target["model"], target.get("extra_body"),
                                    max_tokens_field=target.get("max_tokens_field", "max_tokens"))
            async with httpx.AsyncClient(timeout=httpx.Timeout(
                    connect=20.0, read=60.0, write=60.0, pool=60.0)) as client:
                res = await probe.probe(client, url, probe.headers_for(target["api_key"]),
                                        body, target["label"])
            d = res.to_dict()
            d["label"] = target["label"]
            d["verdict"] = ("ok" if res.ok and not res.suspect else
                            "suspect" if res.ok else "failed")
            d["advice"] = _test_advice(res)
            checks.append(d)
        failed = [c for c in checks if c["verdict"] == "failed"]
        if failed:
            return JSONResponse(status_code=400, content={
                "error": {"message": "保存前的验证没通过，配置未写入。"
                                     "改好再存，或确信没问题就带 force=true 强制保存。"},
                "checks": checks})

    try:
        await asyncio.to_thread(config.write_models_to_file, draft["providers"],
                                draft["summary"], draft["fallback"])
    except Exception as e:                            # noqa: BLE001
        log.error("模型配置回写 config.yaml 失败：%s", e)
        return JSONResponse(status_code=500, content={
            "error": {"message": f"写 config.yaml 失败：{e}"}, "checks": checks})
    do_reload()
    log.warning("模型配置已写回 config.yaml：%d 个供应商，摘要模型 %s",
                len(draft["providers"]), draft["summary"].get("model"))
    suspect = [c for c in checks if c["verdict"] == "suspect"]
    return {"status": "saved", "checks": checks,
            "providers": [p["name"] for p in draft["providers"]],
            "warning": ("有端点返回了可疑内容，已按你的要求保存，但请点开原始响应确认"
                        if suspect else None),
            "note": "已写入 config.yaml 并热重载；原文件备份在 config.yaml.bak"}


def _build_config_draft(payload: dict) -> dict:
    """把页面提交的东西整理成可写入的结构，顺便挑出需要验证的端点。"""
    provs_in = payload.get("providers")
    if not isinstance(provs_in, list) or not provs_in:
        raise ValueError("至少要有一个供应商")
    seen: set[str] = set()
    providers: list[dict] = []
    to_test: list[dict] = []
    for p in provs_in:
        name = str(p.get("name", "")).strip()
        if not config.NAME_RE.match(name):
            raise ValueError(f"供应商名 {name!r} 不合法：只能用字母数字下划线连字符")
        if name in seen:
            raise ValueError(f"供应商名 {name!r} 重复了")
        seen.add(name)
        base_url = str(p.get("base_url", "")).strip().rstrip("/")
        if not base_url:
            raise ValueError(f"供应商 {name} 缺 base_url")
        key = _resolve_key(p.get("api_key"), (config.provider(name) or {}).get("api_key"))
        extra = p.get("extra_body") or {}
        if not isinstance(extra, dict):
            raise ValueError(f"供应商 {name} 的 extra_body 必须是 JSON 对象")
        if bad := [k for k in extra if k in config.PROTECTED_BODY_KEYS]:
            raise ValueError(f"供应商 {name} 的 extra_body 里不能出现 {bad}")
        entry = {"name": name, "base_url": base_url, "api_key": key,
                 "timeout_seconds": int(p.get("timeout_seconds") or 300),
                 "connect_timeout_seconds": int(p.get("connect_timeout_seconds") or 30),
                 "multimodal": bool(p.get("multimodal", True))}
        if extra:
            entry["extra_body"] = extra
        if p.get("forward_headers"):
            entry["forward_headers"] = [str(h) for h in p["forward_headers"]]
        if p.get("forward_query"):
            entry["forward_query"] = True
        providers.append(entry)
        if p.get("test", True) and key:
            # 供应商侧没有"默认模型"，用页面上填的探测模型；没填就跳过验证
            tm = str(p.get("test_model") or "").strip()
            if tm:
                to_test.append({"label": f"provider:{name}", "base_url": base_url,
                                "api_key": key, "model": tm,
                                "extra_body": extra})

    s_in = payload.get("summary") or {}
    cur_eps = {e["tag"]: e for e in config.summary_endpoints()}
    summary = {
        "base_url": str(s_in.get("base_url", "")).strip().rstrip("/"),
        "model": str(s_in.get("model", "")).strip(),
        "api_key": _resolve_key(s_in.get("api_key"), (cur_eps.get("primary") or {}).get("api_key")),
        "summary_max_tokens": int(s_in.get("summary_max_tokens")
                                  or config.SUMMARY_DEFAULT_MAX_TOKENS),
        "max_tokens_field": (str(s_in.get("max_tokens_field") or "max_tokens")
                             if str(s_in.get("max_tokens_field")) in config.MAX_TOKENS_FIELDS
                             else "max_tokens"),
        "timeout_seconds": int(s_in.get("timeout_seconds") or 180),
        "main_max_attempts": int(s_in.get("main_max_attempts") or 2),
        "min_output_tokens": int(s_in.get("min_output_tokens") or 50),
        "extra_body": s_in.get("extra_body") or {},
    }
    if not isinstance(summary["extra_body"], dict):
        raise ValueError("摘要模型的 extra_body 必须是 JSON 对象")
    if bad := [k for k in summary["extra_body"] if k in config.PROTECTED_BODY_KEYS]:
        raise ValueError(f"摘要模型的 extra_body 里不能出现 {bad}")
    if summary["base_url"] and summary["model"] and summary["api_key"]:
        to_test.append({"label": "summary", "base_url": summary["base_url"],
                        "api_key": summary["api_key"], "model": summary["model"],
                        "extra_body": summary["extra_body"],
                        "max_tokens_field": summary["max_tokens_field"]})

    f_in = payload.get("fallback") or {}
    fallback = {
        "enabled": bool(f_in.get("enabled")),
        "base_url": str(f_in.get("base_url", "")).strip().rstrip("/"),
        "model": str(f_in.get("model", "")).strip(),
        "api_key": _resolve_key(f_in.get("api_key"),
                                (cur_eps.get("fallback") or {}).get("api_key")),
        "max_attempts": int(f_in.get("max_attempts") or 3),
    }
    if f_in.get("extra_body"):
        if not isinstance(f_in["extra_body"], dict):
            raise ValueError("备用摘要模型的 extra_body 必须是 JSON 对象")
        fallback["extra_body"] = f_in["extra_body"]
    if fallback["enabled"]:
        if not (fallback["base_url"] and fallback["model"] and fallback["api_key"]):
            raise ValueError("启用了备用摘要模型，就得把 base_url / model / api_key 填全")
        to_test.append({"label": "fallback", "base_url": fallback["base_url"],
                        "api_key": fallback["api_key"], "model": fallback["model"],
                        "extra_body": fallback.get("extra_body") or summary["extra_body"],
                        "max_tokens_field": summary["max_tokens_field"]})

    return {"providers": providers, "summary": summary, "fallback": fallback,
            "to_test": to_test}


@app.post("/admin/clean")
async def admin_clean(request: Request):
    if (denied := _check_auth(request, admin=True)) is not None:
        return denied
    body = await request.json()
    target = (body.get("target") or "").strip()
    if not target:
        return JSONResponse(status_code=400, content={
            "error": {"message": "missing 'target' (use 'all' or conv_id prefix)"}})
    st = store.get()
    ids = [r["conv_id"] for r in await st.list_conversations(100000)] if target == "all" \
        else await st.match_prefix(target)
    await st.delete(ids)
    log.info("已清除 %d 个会话（target=%r）", len(ids), target)
    return {"status": "cleaned", "removed_count": len(ids), "removed": [c[:16] for c in ids]}


@app.get("/ui")
async def ui_page():
    """可视化页面。ui_token 没配就整个不存在，避免无意中把后台裸奔在公网上。"""
    if not config.ui_token():
        return JSONResponse(status_code=404, content={"error": {"message":
            "可视化页面未启用：在 config.yaml 的 server.ui_token 里设一个密钥"
            "（./ctl.sh ui-token 可生成），然后 ./ctl.sh reload"}})
    return HTMLResponse(ui.PAGE)


# ===== 转发 =====
@app.post("/{provider}/v1/chat/completions")
async def chat_completions(provider: str, request: Request):
    if (denied := _check_auth(request)) is not None:
        return denied
    up = config.provider(provider)
    if up is None:
        return JSONResponse(status_code=404, content={
            "error": {"message": f"unknown provider: {provider!r}", "type": "not_found",
                      "available": list(config.providers())}})

    body = await request.json()
    messages = body.get("messages") or []
    url = _upstream_url(up, request)
    headers = _upstream_headers(up, request)
    body = _apply_extra_body(body, up, provider)
    model_name = body.get("model", "")

    if not bool(body.get("stream", False)):
        return await _non_stream(url, headers, body, messages, up, provider)
    return await _stream(url, headers, body, messages, up, provider, model_name)


def _upstream_url(up: dict, request: Request) -> str:
    """拼上游 URL。开了 forward_query 就把客户端的查询串带过去（Azure 的 api-version 等）。"""
    url = f"{up['base_url'].rstrip('/')}/chat/completions"
    q = request.url.query
    if up.get("forward_query") and q:
        url = f"{url}{'&' if '?' in url else '?'}{q}"
    return url


def _upstream_headers(up: dict, request: Request) -> dict[str, str]:
    """鉴权头必须换成供应商的 key（客户端发来的是代理的 token）。

    其余头默认**不透传**——无脑转发会把 cookie、x-forwarded-for 之类一起漏给上游。
    需要哪个就在 providers[].forward_headers 里按名字白名单放行。
    """
    headers = {"Authorization": f"Bearer {up['api_key']}", "Content-Type": "application/json"}
    for name in up.get("forward_headers") or []:
        v = request.headers.get(name)
        if v:
            headers[name] = v
    return headers


def _apply_extra_body(body: dict, up: dict, provider: str) -> dict:
    """把 providers[].extra_body 合进请求体。

    **覆盖客户端的同名字段**——这个配置的用途就是强制某个客户端界面上表达不了的参数
    （比如思考强度）。messages / stream 在加载配置时就被挡掉了，这里不会被改。
    """
    extra = up.get("extra_body") or {}
    if not extra:
        return body
    overridden = [k for k in extra if k in body and body[k] != extra[k]]
    if overridden:
        log.info("[%s] extra_body 覆盖了客户端参数：%s", provider,
                 ", ".join(f"{k}={body[k]!r}→{extra[k]!r}" for k in overridden))
    return {**body, **extra}


async def _non_stream(url: str, headers: dict, body: dict, messages: list[dict],
                      up: dict, provider: str):
    try:
        prepared = await compress.prepare(messages, up)
    except compress.CompressionRefused as e:
        log.error("[%s] 拒绝转发：%s", provider, e.message)
        return JSONResponse(status_code=503, content=_refusal_payload(e))
    except Exception as e:
        log.exception("[%s] 压缩阶段异常，拒绝转发（不降级为全量转发）", provider)
        return JSONResponse(status_code=503, content={"error": {
            "message": f"上下文压缩失败，已拦截本次请求以避免按全量 token 计费：{type(e).__name__}: {e}",
            "type": "context_compression_error"}})

    await _save_timeline(prepared)
    body = {**body, "messages": prepared.messages}
    try:
        async with httpx.AsyncClient(timeout=up["timeout_seconds"]) as client:
            r = await client.post(url, headers=headers, json=body)
    except httpx.HTTPError as e:
        log.warning("[%s] 上游请求失败：%s", provider, e)
        return JSONResponse(status_code=502, content={
            "error": {"message": f"upstream error: {e}", "type": "upstream_error"}})
    try:
        return JSONResponse(status_code=r.status_code, content=r.json())
    except Exception:
        return JSONResponse(status_code=r.status_code, content={
            "error": {"message": r.text[:2000], "type": "upstream_non_json"}})


async def _stream(url: str, headers: dict, body: dict, messages: list[dict],
                  up: dict, provider: str, model_name: str):
    connect_timeout = up.get("connect_timeout_seconds", 30)
    stream_timeout = httpx.Timeout(connect=connect_timeout, read=None,
                                   write=connect_timeout, pool=connect_timeout)

    async def event_stream() -> AsyncIterator[bytes]:
        q: "asyncio.Queue[bytes | None]" = asyncio.Queue()

        async def on_event(kind: str, cur: int = 0, total: int = 0) -> None:
            if kind == "compress":
                txt = f"【正在整理之前的对话… {cur}/{total}】\n" if total > 1 else "【正在整理之前的对话…】\n"
            elif kind == "recompress":
                txt = (f"【正在整理更早的记忆… {cur}/{total}】\n" if total > 1
                       else "【正在整理更早的记忆…】\n")
            else:
                return
            await q.put(_sse(_chunk(model_name, reasoning=txt)))

        async def run_compress():
            try:
                return await compress.prepare(messages, up, on_event=on_event)
            finally:
                await q.put(None)

        comp_task = asyncio.create_task(run_compress())
        try:
            while True:
                item = await q.get()
                if item is None:
                    break
                yield item
            prepared = await comp_task           # 用 await，不要 .result()：任务未完成会抛 InvalidStateError
        except asyncio.CancelledError:
            # 客户端断连：取消压缩任务并静默退出，别让 CancelledError 从异步生成器里逃逸
            comp_task.cancel()
            log.info("[%s] 客户端在压缩阶段断开连接，已取消压缩任务", provider)
            raise
        except compress.CompressionRefused as e:
            log.error("[%s] 拒绝转发：%s", provider, e.message)
            # 压缩阶段已经往 think 区吐过内容，不能再返 HTTP 4xx/5xx，只能以 SSE 事件返回
            yield _sse(_chunk(model_name, content=e.user_text(), finish=None))
            yield _sse(_refusal_payload(e))
            yield _sse(_chunk(model_name, finish="stop"))
            yield b"data: [DONE]\n\n"
            return
        except Exception as e:
            log.exception("[%s] 压缩阶段异常，拒绝转发", provider)
            msg = f"⚠️ 上下文压缩失败，已拦截本次请求以避免按全量 token 计费：{type(e).__name__}: {e}"
            yield _sse(_chunk(model_name, content=msg, finish="stop"))
            yield b"data: [DONE]\n\n"
            return

        await _save_timeline(prepared)
        out_body = {**body, "messages": prepared.messages}
        async for piece in _forward_stream(url, headers, out_body, stream_timeout,
                                           provider, model_name):
            yield piece

    return StreamingResponse(event_stream(), media_type="text/event-stream",
                             headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache",
                                      "Connection": "keep-alive"})


async def _forward_stream(url: str, headers: dict, body: dict, timeout: httpx.Timeout,
                          provider: str, model_name: str) -> AsyncIterator[bytes]:
    """转发上游 SSE，读取与吐出解耦，带"积压即冲刷"的平滑。

    旧实现每 2 字符切一片 + 每片 sleep 10ms：3000 字的回复要发 1500 个事件、睡十几秒。
    读取和吐出还是串在一起的，上游生产远快于下游消费时，响应全堆在 httpx 的缓冲里，
    连接闲置到超时——日志里的 `peer closed connection without sending complete
    message body` 就是它。

    现在上游读取放在独立任务里全速消费，平滑只作用于吐出；一旦**上游已结束**
    或**待发积压超过 flush_backlog_chars**，立刻丢弃剩余延迟，把缓冲一次性拼接吐完。
    """
    scfg = config.stream_cfg()
    chars = int(scfg.get("smooth_chars", 0) or 0)
    delay = float(scfg.get("smooth_delay", 0.0) or 0.0)
    backlog_limit = max(chars + 1, int(scfg.get("flush_backlog_chars", 600) or 600))

    q: "asyncio.Queue[tuple[str, Any] | None]" = asyncio.Queue()
    upstream_done = asyncio.Event()

    async def producer() -> None:
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream("POST", url, headers=headers, json=body) as r:
                    if r.status_code >= 400:
                        raw = await r.aread()
                        try:
                            err = json.loads(raw)
                        except Exception:
                            err = {"error": {"message": raw.decode("utf-8", "ignore")[:2000],
                                             "type": "upstream_error"}}
                        log.warning("[%s] 上游流式返回 %d", provider, r.status_code)
                        await q.put(("raw", _sse(err)))
                        return
                    async for line in r.aiter_lines():
                        if not line:
                            continue
                        if not line.startswith("data:"):
                            await q.put(("raw", (line + "\n\n").encode("utf-8")))
                            continue
                        data_str = line[5:].strip()
                        if data_str == "[DONE]":
                            await q.put(("raw", b"data: [DONE]\n\n"))
                            continue
                        if chars <= 0:
                            await q.put(("raw", (line + "\n\n").encode("utf-8")))
                            continue
                        try:
                            obj = json.loads(data_str)
                            choice = (obj.get("choices") or [{}])[0]
                            delta = choice.get("delta") or {}
                            text = delta.get("content")
                            other = {k: v for k, v in delta.items() if k != "content"}
                        except Exception:
                            await q.put(("raw", (line + "\n\n").encode("utf-8")))
                            continue
                        if text:
                            await q.put(("text", text))
                        # 正文之外的字段（思考内容 / tool_calls / finish_reason）原样带过去，
                        # 顺序上排在正文之后，消费端会先把缓冲冲干净
                        if other or not text:
                            await q.put(("raw", _sse({**obj, "choices": [{**choice, "delta": other}]})))
        except asyncio.CancelledError:
            raise
        except httpx.HTTPError as e:
            log.warning("[%s] 流式上游请求失败：%s", provider, e)
            await q.put(("raw", _sse({"error": {"message": f"upstream error: {e}",
                                                "type": "upstream_error"}})))
        except Exception as e:
            log.exception("[%s] 流式转发内部异常", provider)
            await q.put(("raw", _sse({"error": {"message": f"proxy stream error: {e}",
                                                "type": "proxy_error"}})))
        finally:
            upstream_done.set()
            await q.put(None)

    task = asyncio.create_task(producer())
    buf = ""
    saw_done = False
    try:
        while True:
            item = await q.get()
            if item is None:
                break
            kind, val = item
            if kind == "raw":
                if buf:
                    yield _sse(_chunk(model_name, content=buf))
                    buf = ""
                if val == b"data: [DONE]\n\n":
                    saw_done = True
                yield val
                continue
            buf += val
            while len(buf) >= chars:
                # 上游已经吐完 或 积压过多：丢掉剩余延迟，一次性发走
                if upstream_done.is_set() or len(buf) >= backlog_limit:
                    yield _sse(_chunk(model_name, content=buf))
                    buf = ""
                    break
                piece, buf = buf[:chars], buf[chars:]
                yield _sse(_chunk(model_name, content=piece))
                if delay:
                    await asyncio.sleep(delay)
        if buf:
            yield _sse(_chunk(model_name, content=buf))
        if not saw_done:
            yield b"data: [DONE]\n\n"
    except asyncio.CancelledError:
        log.info("[%s] 客户端在转发阶段断开连接", provider)
        task.cancel()
        raise
    finally:
        if not task.done():
            task.cancel()


@app.get("/{provider}/v1/models")
async def models(provider: str, request: Request):
    if (denied := _check_auth(request)) is not None:
        return denied
    up = config.provider(provider)
    if up is None:
        return JSONResponse(status_code=404, content={
            "error": {"message": f"unknown provider: {provider!r}", "type": "not_found",
                      "available": list(config.providers())}})
    try:
        async with httpx.AsyncClient(timeout=up["timeout_seconds"]) as client:
            r = await client.get(f"{up['base_url'].rstrip('/')}/models",
                                 headers={"Authorization": f"Bearer {up['api_key']}"})
            return JSONResponse(status_code=r.status_code, content=r.json())
    except httpx.HTTPError as e:
        log.warning("[%s] 拉取模型列表失败：%s", provider, e)
        return JSONResponse(status_code=502, content={
            "error": {"message": f"upstream error: {e}", "type": "upstream_error"}})


def install_sighup() -> None:
    def _on_sighup(_signum, _frame):
        try:
            do_reload()
        except Exception:
            log.exception("SIGHUP 热重载失败，保留旧配置")

    try:
        signal.signal(signal.SIGHUP, _on_sighup)
    except (AttributeError, ValueError):
        pass
