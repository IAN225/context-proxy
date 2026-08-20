"""FastAPI 应用：鉴权、转发、流式平滑、管理接口。"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import compress, config, logging_setup, store
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
    log.info("配置已加载：providers=%s summary=%s fallback=%s",
             list(config.providers()), config.summary().get("model"),
             (config.summary().get("fallback") or {}).get("model")
             if (config.summary().get("fallback") or {}).get("enabled") else "off")


def do_reload() -> dict[str, Any]:
    summary = config.reload(warn=log.warning)
    logging_setup.setup()
    store.init(config.db_path())
    log.info("配置已热重载：%s", summary)
    return summary


# ===== 工具 =====
def _check_auth(request: Request) -> JSONResponse | None:
    token = config.auth_token()
    if not token:
        return None
    auth = request.headers.get("authorization", "")
    got = auth[7:].strip() if auth.lower().startswith("bearer ") else auth.strip()
    if got != token:
        return JSONResponse(status_code=401,
                            content={"error": {"message": "unauthorized", "type": "auth_error"}})
    return None


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
        "open_compression_events": stats.get("open_events", 0),
        # 兜底定位每触发一次都说明上面的定位逻辑漏了一种情况，这个数字应当长期为 0
        "fallback_activations": stats.get("fallback_activations", 0),
        "conversation_locks": len(compress._LOCKS),
    }


@app.post("/admin/reload")
async def admin_reload(request: Request):
    if (denied := _check_auth(request)) is not None:
        return denied
    try:
        return {"status": "reloaded", **do_reload()}
    except Exception as e:
        log.exception("热重载失败，保留旧配置")
        return JSONResponse(status_code=400, content={
            "error": {"message": f"reload failed: {e}", "type": "reload_error"}})


@app.get("/admin/sessions")
async def admin_sessions(request: Request, limit: int = 100):
    if (denied := _check_auth(request)) is not None:
        return denied
    rows = await store.get().list_conversations(limit)
    for r in rows:
        r["conv_id_short"] = r["conv_id"][:16]
        r["age_seconds"] = int(time.time() - (r.get("updated_at") or 0))
    return {"count": len(rows), "sessions": rows}


@app.get("/admin/session/{conv_id}")
async def admin_session(conv_id: str, request: Request):
    if (denied := _check_auth(request)) is not None:
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
            "summary": c["summary"],
            "updated_at": c["updated_at"],
        } for c in cks],
    }


@app.post("/admin/clean")
async def admin_clean(request: Request):
    if (denied := _check_auth(request)) is not None:
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
    url = f"{up['base_url'].rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {up['api_key']}", "Content-Type": "application/json"}
    model_name = body.get("model", "")

    if not bool(body.get("stream", False)):
        return await _non_stream(url, headers, body, messages, up, provider)
    return await _stream(url, headers, body, messages, up, provider, model_name)


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
