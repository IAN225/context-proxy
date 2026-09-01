"""摘要模型调用：主模型 → fallback，按错误类型分流重试。

错误分类（决定重试策略）：

============================  ==========================================
类型                          处理
============================  ==========================================
``timeout`` / 5xx / 429       指数退避重试；429 优先读 ``Retry-After``
``context_length_exceeded``   **不重试**，直接把输入二分后重投
``auth``（401/402/余额不足）  立即失败，错误信息里点明是鉴权/余额问题
``short_output``              输出（不含思考）< min_output_tokens，判定失败后重试/切备用
其他 4xx                      不重试，切备用模型
============================  ==========================================

主模型耗尽 ``main_max_attempts`` 后切 fallback；fallback 最多 ``max_attempts``（默认 3）次，
仍失败则抛 :class:`SummaryFailure`，由上层结束本次请求（**已落盘的批次全部保留**）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from typing import Any, Callable

import httpx

from . import config
from . import messages as M

log = logging.getLogger("proxy.summary")

_CTX_PAT = re.compile(
    r"context[_ ]length|maximum context|too many tokens|context window|prompt is too long|"
    r"reduce the length|input length", re.I)
_BALANCE_PAT = re.compile(r"insufficient|balance|quota|arrears|欠费|余额", re.I)


class SummaryFailure(Exception):
    """摘要彻底失败（主备都用尽）。"""

    def __init__(self, kind: str, message: str, attempts: list[str] | None = None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.attempts = attempts or []


class _CallError(Exception):
    def __init__(self, kind: str, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.kind = kind            # timeout | server | rate_limit | context | auth | bad | short
        self.message = message
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        return self.kind in ("timeout", "server", "rate_limit", "short")


def _extract_text(data: dict) -> tuple[str, str]:
    """返回 (正文, 思考内容)。思考部分不计入长度校验。"""
    try:
        msg = data["choices"][0]["message"]
    except Exception:
        return "", ""
    content = msg.get("content")
    if isinstance(content, list):
        content = "\n".join(p.get("text", "") for p in content
                            if isinstance(p, dict) and p.get("type") == "text")
    reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
    return (content or "").strip(), (reasoning or "").strip() if isinstance(reasoning, str) else ""


async def _one_call(ep: dict[str, Any], user_prompt: str,
                    max_tokens: int, timeout_s: float) -> str:
    # extra_body 先铺底，再让本函数的固定字段覆盖它——
    # model / max_tokens 由摘要配置决定，messages / stream 是这条调用链的骨架，都不接受改写。
    #
    # 只发一条 user 消息：提示词和待压正文是同一段模板（{{context}} 插正文），
    # 用户在页面上改的就是这一整段，拆成 system + 硬编码包装语的话，
    # 包装语那部分他改不到，看到的和实际发出去的也对不上。
    payload = {
        **(ep.get("extra_body") or {}),
        "model": ep["model"],
        "stream": False,
        "messages": [{"role": "user", "content": user_prompt}],
    }
    # token 上限的字段名不是一家说了算：OpenAI 新模型只认 max_completion_tokens，
    # 别的只认 max_tokens。填哪个由 summary.max_tokens_field 决定（控制台能探测出来），
    # 并且把 extra_body 里可能写着的另一个名字清掉，免得两个一起发过去被判冲突。
    for f in config.MAX_TOKENS_FIELDS:
        payload.pop(f, None)
    payload[config.max_tokens_field()] = max_tokens
    to = httpx.Timeout(connect=30.0, read=timeout_s, write=timeout_s, pool=timeout_s)
    try:
        async with httpx.AsyncClient(timeout=to) as client:
            r = await asyncio.wait_for(
                client.post(f"{ep['base_url']}/chat/completions",
                            headers={"Authorization": f"Bearer {ep['api_key']}",
                                     "Content-Type": "application/json"},
                            json=payload),
                timeout=timeout_s + 30)
    except (asyncio.TimeoutError, httpx.TimeoutException) as e:
        raise _CallError("timeout", f"摘要请求超时（{timeout_s}s）: {type(e).__name__}") from e
    except httpx.HTTPError as e:
        raise _CallError("server", f"摘要请求网络错误: {type(e).__name__}: {e}") from e

    if r.status_code >= 400:
        body = r.text[:1500]
        if r.status_code == 429:
            ra = r.headers.get("Retry-After")
            wait = None
            try:
                wait = float(ra) if ra else None
            except ValueError:
                wait = None
            raise _CallError("rate_limit", f"摘要模型限流 429: {body}", retry_after=wait)
        if r.status_code in (401, 403):
            raise _CallError("auth", f"摘要模型鉴权失败 {r.status_code}（api_key 无效或无权限）: {body}")
        if r.status_code == 402 or _BALANCE_PAT.search(body):
            raise _CallError("auth", f"摘要模型余额不足 / 配额耗尽 {r.status_code}: {body}")
        if r.status_code >= 500:
            raise _CallError("server", f"摘要模型上游 {r.status_code}: {body}")
        if _CTX_PAT.search(body):
            raise _CallError("context", f"单批输入超出摘要模型上下文: {body}")
        raise _CallError("bad", f"摘要模型返回 {r.status_code}: {body}")

    try:
        data = r.json()
    except Exception as e:
        raise _CallError("bad", f"摘要模型返回非 JSON: {r.text[:500]}") from e

    if isinstance(data, dict) and data.get("error"):
        emsg = json.dumps(data["error"], ensure_ascii=False)[:1000]
        if _CTX_PAT.search(emsg):
            raise _CallError("context", f"单批输入超出摘要模型上下文: {emsg}")
        if _BALANCE_PAT.search(emsg):
            raise _CallError("auth", f"摘要模型余额不足 / 配额耗尽: {emsg}")
        raise _CallError("bad", f"摘要模型返回错误体: {emsg}")

    text, reasoning = _extract_text(data)
    min_tokens = int(config.summary().get("min_output_tokens", 50))
    ntok = M.text_tokens(text)
    if ntok < min_tokens:
        raise _CallError("short", f"摘要输出过短（{ntok} < {min_tokens} token，"
                                  f"思考部分 {M.text_tokens(reasoning)} token 不计）")
    return text.strip()


async def call_chain(user_prompt: str, max_tokens: int,
                     *, label: str) -> tuple[str, str]:
    """按 [主, 备] 顺序调用，返回 (摘要文本, 实际生效的模型标识)。

    ``context`` 类错误直接向上抛，由调用方二分输入后重投——重试同样的输入没有意义。
    """
    endpoints = config.summary_endpoints()
    if not endpoints:
        raise SummaryFailure("config", "摘要模型未配置（summary.base_url / api_key / model 至少缺一项）")
    timeout_s = float(config.summary().get("timeout_seconds", 180))
    attempts_log: list[str] = []

    for ep in endpoints:
        who = f"{ep['tag']}:{ep['model']}"
        # 用 get 而不是下标：端点字典少一个字段也只该退化成"不重试"，不该炸在这里
        max_attempts = max(1, int(ep.get("max_attempts", 1)))
        for attempt in range(1, max_attempts + 1):
            try:
                text = await _one_call(ep, user_prompt, max_tokens, timeout_s)
                if attempts_log:
                    log.info("%s 由 %s 第 %d 次尝试成功（此前失败：%s）",
                             label, who, attempt, "；".join(attempts_log[-3:]))
                return text, who
            except _CallError as e:
                attempts_log.append(f"{who}#{attempt} {e.kind}: {e.message[:200]}")
                if e.kind == "context":
                    raise
                if e.kind == "auth":
                    log.error("%s：%s 鉴权/余额错误，不重试该端点：%s", label, who, e.message[:300])
                    break
                if not e.retryable or attempt >= max_attempts:
                    log.warning("%s：%s 第 %d 次失败（%s），%s", label, who, attempt, e.kind,
                                "切换备用模型" if ep is not endpoints[-1] else "无可用备用模型")
                    break
                delay = e.retry_after if e.retry_after else min(30.0, 2.0 ** attempt)
                delay += random.uniform(0, 0.5)
                log.warning("%s：%s 第 %d 次失败（%s: %s），%.1fs 后重试",
                            label, who, attempt, e.kind, e.message[:200], delay)
                await asyncio.sleep(delay)

    detail = "；".join(attempts_log)
    kind = "auth" if any("auth:" in a for a in attempts_log) else "exhausted"
    raise SummaryFailure(kind, f"{label} 失败，主备模型均未成功：{detail}", attempts_log)


# ===== 对外：批次摘要 =====
async def summarize_batch(batch: list[dict], depth: int = 0) -> tuple[str, str]:
    """摘要一批原文消息。遇到上下文超限就二分重投（不是重试）。"""
    s = config.summary()
    user = config.render_prompt("batch", M.render_for_summary(batch))
    try:
        return await call_chain(user, int(s.get("summary_max_tokens", 2048)),
                                label=f"批次摘要({len(batch)} 条)")
    except _CallError as e:      # call_chain 只会把 context 类原样抛上来
        if e.kind != "context" or len(batch) < 2 or depth >= 3:
            raise SummaryFailure("context", f"批次摘要输入超出上下文且无法继续二分：{e.message}")
        mid = len(batch) // 2
        log.warning("批次输入超上下文，二分重投：%d -> %d + %d（第 %d 层）",
                    len(batch), mid, len(batch) - mid, depth + 1)
        left, m1 = await summarize_batch(batch[:mid], depth + 1)
        right, m2 = await summarize_batch(batch[mid:], depth + 1)
        return (left.rstrip() + "\n\n" + right.lstrip()), (m1 if m1 == m2 else f"{m1}+{m2}")


def _split_by_tokens(text: str, limit: int) -> list[str]:
    """按段落边界切成不超过 limit token 的片段，保持原顺序。"""
    paras = [p for p in re.split(r"\n{2,}", text) if p.strip()]
    chunks: list[str] = []
    cur: list[str] = []
    cur_tok = 0
    for p in paras:
        t = M.text_tokens(p)
        if cur and cur_tok + t > limit:
            chunks.append("\n\n".join(cur))
            cur, cur_tok = [], 0
        cur.append(p)
        cur_tok += t
    if cur:
        chunks.append("\n\n".join(cur))
    return chunks or [text]


async def recompress(long_summary: str, on_progress: Callable | None = None) -> tuple[str, str, int]:
    """累积摘要的二次重压：**和批次摘要同构**——按阈值分批，每批一次调用，系统拼接。

    早先这里是两段式（分片用"禁止输出章节标题"的提示词精简，再调一次模型合并成稿），
    实际用下来太零散：多一次调用、多一套提示词，产出还不见得更好。现在统一成
    「按 ``summary_batch_tokens`` 切片 → 每片用同一套 ``prompts.recompress`` 精简
    → 系统直接用空行拼接」，和普通压缩累积摘要的方式完全一致。
    """
    s = config.summary()
    chunks = _split_by_tokens(long_summary, int(s.get("summary_batch_tokens", 10000)))
    before = M.text_tokens(long_summary)
    if len(chunks) > 1:
        log.info("二次重压：累积摘要 %d tokens，按阈值分 %d 片，逐片精简后拼接", before, len(chunks))

    out: list[str] = []
    models: set[str] = set()
    for i, ch in enumerate(chunks):
        if on_progress:
            await on_progress(i + 1, len(chunks))
        text, who = await _condense(ch)
        out.append(text.strip())
        models.add(who)
        if len(chunks) > 1:
            log.info("二次重压：第 %d/%d 片完成（%d -> %d tokens，模型 %s）",
                     i + 1, len(chunks), M.text_tokens(ch), M.text_tokens(text), who)
    return "\n\n".join(x for x in out if x), "+".join(sorted(models)), len(chunks)


async def _condense(text: str, depth: int = 0) -> tuple[str, str]:
    s = config.summary()
    try:
        return await call_chain(config.render_prompt("recompress", text),
                                int(s.get("summary_max_tokens", 2048)),
                                label=f"二次重压({M.text_tokens(text)} tokens)")
    except _CallError as e:
        if e.kind != "context" or depth >= 3:
            raise SummaryFailure("context", f"二次重压分片仍超出上下文：{e.message}")
        half = len(text) // 2
        log.warning("二次重压分片超上下文，二分重投（第 %d 层）", depth + 1)
        a, m1 = await _condense(text[:half], depth + 1)
        b, m2 = await _condense(text[half:], depth + 1)
        return a.rstrip() + "\n\n" + b.lstrip(), (m1 if m1 == m2 else f"{m1}+{m2}")
