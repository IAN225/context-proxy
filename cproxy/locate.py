"""会话定位与分支点检测。

分两步：

**一、找到是哪个会话**（宽松匹配，尽量别误判成新会话）

1. 精确键：前 5 条 user 消息全文的哈希，命中直接走——覆盖绝大多数请求。
2. 失配则锚点匹配：从当前 body 取 5 个锚点（第 1、2 条合格消息 + 25%/50%/75% 位置往后
   第一条合格消息），在 fp 倒排表里查，**不依赖顺序**。按命中数计分，硬性要求 ≥2 分；
   多个会话同分取最近更新的那个并打 WARNING；低于 2 分视为新会话。
3. 再失配则试旧版指纹（迁移过来的老会话）。

**二、在这个会话里找到能用的 checkpoint**

拿当前请求的指纹数组和 checkpoint 的指纹数组做最长公共前缀比对。遇到不等时先用
±4 条小窗口 + 三连比对探测「客户端在中间插了/删了几条」——那只是错位，历史本体没变，
修正偏移量继续比即可，不能当成分叉。确实对不上才是真分叉，记下分叉轮次，
回退到 ``compressed_upto`` 仍落在公共前缀内的最新 checkpoint。

全都对不上时返回 ``fallback`` 模式，由调用方走兜底路径。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from . import messages as M
from . import store

log = logging.getLogger("proxy.locate")

WINDOW = 4      # 插/删探测窗口：±4 条
CONFIRM = 3     # 三连比对确认


@dataclass
class Alignment:
    """当前指纹数组与某个 checkpoint 指纹数组的对齐结果。"""
    matched_ref: int                     # checkpoint 侧已确认匹配到的下标（不含）
    matched_cur: int                     # 当前请求侧对应的下标（不含）
    forked: bool                         # 是否检测到真分叉
    fork_ref: int | None = None          # 分叉点在 checkpoint 侧的下标
    fork_cur: int | None = None          # 分叉点在当前请求侧的下标
    shifts: int = 0                      # 修正过几次插/删错位
    net_offset: int = 0                  # cur - ref 的净偏移
    ref2cur: list[int] = field(default_factory=list)

    def cur_index_for(self, ref_index: int) -> int:
        if ref_index <= 0:
            return 0
        if ref_index - 1 < len(self.ref2cur):
            return self.ref2cur[ref_index - 1] + 1
        return min(ref_index + self.net_offset, self.matched_cur)


def _triple_eq(a: list[str], ia: int, b: list[str], ib: int, n: int = CONFIRM) -> bool:
    """a[ia:ia+n] == b[ib:ib+n]；越界时按可比长度比（至少要能比 1 条）。"""
    avail = min(n, len(a) - ia, len(b) - ib)
    if avail <= 0:
        return False
    for k in range(avail):
        if a[ia + k] != b[ib + k]:
            return False
    return True


def align(cur: list[str], ref: list[str]) -> Alignment:
    """把当前指纹数组 cur 对齐到 checkpoint 指纹数组 ref。"""
    i = j = 0                     # i 走 ref，j 走 cur
    ref2cur = [-1] * len(ref)
    shifts = 0
    while i < len(ref) and j < len(cur):
        if ref[i] == cur[j]:
            ref2cur[i] = j
            i += 1
            j += 1
            continue
        found = None
        for d in range(1, WINDOW + 1):
            # 客户端删掉了 d 条：ref 往前跳 d
            if i + d < len(ref) and _triple_eq(ref, i + d, cur, j):
                found = ("del", d)
                break
            # 客户端插入了 d 条：cur 往前跳 d
            if j + d < len(cur) and _triple_eq(ref, i, cur, j + d):
                found = ("ins", d)
                break
        if found is None:
            return Alignment(matched_ref=i, matched_cur=j, forked=True, fork_ref=i, fork_cur=j,
                             shifts=shifts, net_offset=j - i, ref2cur=ref2cur)
        shifts += 1
        if found[0] == "del":
            for k in range(found[1]):
                ref2cur[i + k] = j - 1      # 被删掉的 ref 条目挂到上一条 cur 上
            i += found[1]
        else:
            j += found[1]
    return Alignment(matched_ref=i, matched_cur=j, forked=False, shifts=shifts,
                     net_offset=j - i, ref2cur=ref2cur)


@dataclass
class Located:
    mode: str                      # new | exact | anchor | legacy | fallback
    conv_id: str | None
    checkpoint: dict | None
    already: int                   # 当前请求 body 里"已压缩到"的下标
    summary: str
    fork_round: int | None = None
    fork_index: int | None = None
    shifts: int = 0
    match_score: int | None = None
    notes: list[str] = field(default_factory=list)


async def candidates(store, conv_key: str, anchors: list[str],
                     legacy_id: str) -> list[tuple[str, str, int | None]]:
    """返回按可信度排序的候选会话 [(conv_id, mode, score), ...]。

    精确哈希排第一；锚点匹配（≥2 分）按分数补在后面，作为精确命中却对不齐时的备选——
    极少数情况下两个会话的前 5 条 user 消息完全相同，精确键会撞在一起，
    这时靠锚点候选还能救回来，而不是直接掉进兜底。
    """
    if not store.enabled:
        return []
    out: list[tuple[str, str, int | None]] = []
    seen: set[str] = set()

    cid = await store.find_by_key(conv_key)
    if cid:
        out.append((cid, "exact", None))
        seen.add(cid)

    scored = await store.match_anchors(anchors)
    hits = [s for s in scored if s[1] >= 2]
    if scored and not hits:
        log.info("锚点匹配最高仅 %d 分(<2)，不作为候选", scored[0][1])
    if hits:
        top = hits[0][1]
        tied = [s for s in hits if s[1] == top]
        if len(tied) > 1:
            log.warning("锚点匹配出现 %d 个同分(%d 分)会话 [%s]，取最近更新的 %s",
                        len(tied), top, ", ".join(c[:12] for c, _s, _u in tied), tied[0][0][:12])
    for c, sc, _u in hits:
        if c not in seen:
            out.append((c, "anchor", sc))
            seen.add(c)

    cid = await store.find_by_legacy(legacy_id)
    if cid and cid not in seen:
        out.append((cid, "legacy", None))
    return out


def _legacy_usable(ck: dict, body: list[dict]) -> bool:
    """迁移来的 checkpoint 没有指纹数组，只能用旧的边界指纹做一次弱校验。"""
    upto = int(ck.get("compressed_upto") or 0)
    if upto <= 0 or upto > len(body):
        return False
    fp = ck.get("legacy_fp")
    if not fp:
        return False
    return M.legacy_boundary_fp(body[upto - 1]) == fp


def choose_checkpoint(checkpoints: list[dict], cur_sig: list[str], body: list[dict],
                      rounds: list[tuple[int, int]]) -> Located:
    """在候选 checkpoint（已按 seq 倒序）里挑一个仍然有效的。"""
    first_fork_round: int | None = None
    first_fork_index: int | None = None

    for ck in checkpoints:
        sig = store.checkpoint_signature(ck)
        upto = int(ck.get("compressed_upto") or 0)
        if sig is None:
            if _legacy_usable(ck, body):
                return Located(mode="ok", conv_id=ck["conv_id"], checkpoint=ck, already=upto,
                               summary=ck.get("summary") or "",
                               notes=["旧库迁移的 checkpoint，用边界指纹校验通过"])
            continue

        al = align(cur_sig, sig)
        if al.forked and first_fork_index is None:
            first_fork_index = al.fork_cur
            first_fork_round = M.round_of_index(rounds, al.fork_cur or 0)

        if al.matched_ref >= upto:
            already = al.cur_index_for(upto)
            already = max(0, min(already, len(cur_sig)))
            notes = []
            if al.shifts:
                notes.append(f"客户端在历史中插/删了内容，已修正 {al.shifts} 处错位"
                             f"（净偏移 {al.net_offset:+d} 条）")
            if al.forked:
                notes.append(f"分叉点在第 {M.round_of_index(rounds, al.fork_cur or 0)} 轮"
                             f"（消息下标 {al.fork_cur}），位于已压缩区之后，不影响本 checkpoint")
            return Located(mode="ok", conv_id=ck["conv_id"], checkpoint=ck, already=already,
                           summary=ck.get("summary") or "",
                           fork_round=M.round_of_index(rounds, al.fork_cur) if al.forked else None,
                           fork_index=al.fork_cur, shifts=al.shifts, notes=notes)

    return Located(mode="fallback", conv_id=None, checkpoint=None, already=0, summary="",
                   fork_round=first_fork_round, fork_index=first_fork_index)
