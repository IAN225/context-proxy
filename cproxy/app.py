"""FastAPI 应用：鉴权、转发、流式平滑、管理接口。"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import signal
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse

from . import compress, config, logging_setup, store, ui
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


def _check_auth(request: Request, *, admin: bool = False) -> JSONResponse | None:
    """对话接口只认 auth_token；管理接口额外接受 ui_token。

    两者刻意分开：auth_token 要填进 chatbox、跟着每个对话请求走，
    拿它当后台密码等于把后台钥匙散出去。
    """
    accepted = [t for t in ([config.auth_token()] +
                            ([config.ui_token()] if admin else [])) if t]
    if not accepted:
        return None
    ip = _client_ip(request)
    if _throttled(ip):
        return JSONResponse(status_code=429, content={"error": {
            "message": "密钥错误次数过多，请稍后再试", "type": "auth_error"}})
    got = _bearer(request)
    # compare_digest 防时序侧信道；两个都比一遍，不因为先匹配到就早退
    if any(hmac.compare_digest(got, t) for t in accepted):
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
        "open_compression_events": stats.get("open_events", 0),
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
    return {
        "conv_id": cid,
        "base_seq": ck["seq"],          # 回写时带上它做乐观并发校验
        "kind": ck["kind"], "status": ck["status"],
        "compressed_upto": ck["compressed_upto"], "round_upto": ck["round_upto"],
        "total_rounds": ck["total_rounds"],
        "summary_tokens": M.text_tokens(ck["summary"] or ""),
        "summary_cap_tokens": config.summary().get("summary_total_cap_tokens"),
        "editable": open_ev is None,
        "open_event_seq": open_ev["seq"] if open_ev else None,
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

    # 和压缩流程抢同一把会话锁，避免和正在跑的压缩交错写
    async with compress.conversation_lock((conv or {}).get("conv_key"), cid):
        ck = await st.latest_checkpoint(cid)
        if ck is None:
            return JSONResponse(status_code=404, content={
                "error": {"message": "这个会话还没有任何 checkpoint（尚未触发过压缩）"}})
        if ck["status"] == "partial":
            return JSONResponse(status_code=409, content={"error": {"message":
                f"会话正在压缩中（事件 seq={ck['seq']} 未完成），现在改会和它打架。"
                "等这轮压完（再发一条消息推进它）再改。"}})
        base_seq = payload.get("base_seq")
        if base_seq is not None and int(base_seq) != int(ck["seq"]):
            return JSONResponse(status_code=409, content={"error": {"message":
                f"摘要已被更新（你基于 seq={base_seq}，当前是 seq={ck['seq']}），"
                "请重新取一次再改，以免覆盖掉新压出来的内容。"}})

        tokens = M.text_tokens(text)
        cap = int(config.summary().get("summary_total_cap_tokens", 12800))
        if tokens > cap:
            return JSONResponse(status_code=400, content={"error": {"message":
                f"摘要 {tokens} tokens 超过 summary_total_cap_tokens={cap}，"
                "超了会在下次压缩时被自动二次重压、把你的改动洗掉。请精简后再存。"}})

        seq = await st.add_manual_checkpoint(cid, int(ck["id"]), text,
                                             int(config.summary().get("checkpoint_keep", 10)))
    old_tokens = M.text_tokens(ck["summary"] or "")
    log.warning("[%s] 摘要被手工改写：seq %s -> %s（%d -> %d tokens），"
                "位置信息沿用第 %s 轮 / 下标 %s，原 checkpoint 保留可回退",
                cid[:12], ck["seq"], seq, old_tokens, tokens,
                ck["round_upto"], ck["compressed_upto"])
    return {"status": "updated", "conv_id": cid, "new_seq": seq, "base_seq": ck["seq"],
            "summary_tokens": tokens, "previous_summary_tokens": old_tokens,
            "note": "下一次请求即生效；后续压缩会在此基础上追加"}


PROMPT_LABELS = {
    "batch_system": "批次摘要：把一段原文对话压成要点",
    "recompress": "二次重压：累积摘要超过 cap 时逐片精简（按阈值分批，系统拼接）",
    "injection": "注入给主模型的包装语（{summary} 是占位符，必须保留）",
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
    """保存提示词。写进数据库当覆盖项，不回写 config.yaml（那会把注释冲掉）。

    某条提交空字符串 = 删除该条覆盖，恢复成 config.yaml 里的值。
    """
    if (denied := _check_auth(request, admin=True)) is not None:
        return denied
    payload = await request.json()
    incoming = payload.get("prompts")
    if not isinstance(incoming, dict):
        return JSONResponse(status_code=400, content={
            "error": {"message": "body 需要 {\"prompts\": {name: text}}"}})

    current = config.prompt_overrides()
    file_vals = config.cfg()["summary"]["prompts"]
    for name, text in incoming.items():
        if name not in config.FALLBACK_PROMPTS:
            return JSONResponse(status_code=400, content={
                "error": {"message": f"未知的提示词 {name!r}，可用：{list(config.FALLBACK_PROMPTS)}"}})
        text = text if isinstance(text, str) else ""
        if not text.strip() or text.strip() == (file_vals.get(name) or "").strip():
            current.pop(name, None)              # 和文件里一样就没必要留覆盖
            continue
        if name == "injection" and "{summary}" not in text:
            return JSONResponse(status_code=400, content={"error": {"message":
                "injection 里必须保留 {summary} 占位符，否则摘要不会被注入"}})
        current[name] = text

    st = store.get()
    if st.enabled:
        await st.set_meta(PROMPT_OVERRIDE_KEY, json.dumps(current, ensure_ascii=False))
    config.set_prompt_overrides(current)
    log.warning("提示词已更新：覆盖项 = %s（未回写 config.yaml）",
                ", ".join(sorted(current)) or "无（全部恢复为文件值）")
    return {"status": "saved", "overridden": sorted(current)}


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
