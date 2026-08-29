"""测试用的假上游：同时扮演对话模型和摘要模型。

行为可以通过 /__ctl 动态调整，用来构造超时、429、余额不足、短输出等失败场景。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

STATE = {
    "fail_next": 0,          # 接下来 N 次摘要请求返回 status
    "fail_after": -1,        # 先成功 N 次摘要，之后一直失败（-1 = 关闭）
    "fail_status": 500,
    "fail_body": {"error": {"message": "boom"}},
    "short_next": 0,         # 接下来 N 次摘要返回过短内容
    "calls": [],             # 记录每次收到的请求
    "summary_calls": 0,
    "summary_text": None,    # 固定摘要文本（None = 自动生成）
    "strict_body": False,    # true = 遇到白名单外的 body 字段返回 400（模拟严格网关）
    "strict_message": "Unrecognized request argument supplied: {fields}",
    "reasoning_for": [],     # 命中这些 body 字段时在响应里带上思考内容
    "chat_status": 0,        # 非 0 = 对话请求返回这个状态（模拟 key 错、端点挂了）
    "chat_status_after": 0,  # chat_status 从第几次对话请求开始生效（0 = 从第一次就生效）
    "field_status": {},      # {body 字段名: 状态码}，命中就返回该状态（模拟限流打在某个探针上）
    "field_status_once": False,   # true = field_status 只生效一次（用来验证重试）
}

# 严格模式下允许出现的 body 字段
KNOWN_BODY_KEYS = {"model", "messages", "stream", "max_tokens", "temperature", "top_p",
                   "seed", "presence_penalty", "frequency_penalty", "stop", "user"}

app = FastAPI()


@app.post("/__ctl")
async def ctl(request: Request):
    STATE.update(await request.json())
    return {"ok": True, "state": {k: v for k, v in STATE.items() if k != "calls"}}


@app.get("/__calls")
async def calls():
    return {"count": len(STATE["calls"]), "calls": STATE["calls"]}


@app.post("/__reset")
async def reset():
    STATE.update({"fail_next": 0, "fail_after": -1, "fail_status": 500, "short_next": 0,
                  "calls": [], "summary_calls": 0, "summary_text": None,
                  "strict_body": False, "reasoning_for": [], "chat_status": 0,
                  "chat_status_after": 0,
                  "field_status": {}, "field_status_once": False,
                  "strict_message": "Unrecognized request argument supplied: {fields}"})
    return {"ok": True}


@app.get("/v1/models")
async def models():
    return {"object": "list", "data": [{"id": "mock-chat"}, {"id": "mock-summary"},
                                      {"id": "mock-summary-backup"}]}


def _is_summary(body: dict) -> bool:
    return str(body.get("model", "")).startswith("mock-summary")


def _is_backup(body: dict) -> bool:
    """备用摘要模型：失败注入只作用于主模型，备用永远成功，方便断言"切过去了"。"""
    return str(body.get("model", "")) == "mock-summary-backup"


@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    msgs = body.get("messages") or []
    STATE["calls"].append({
        "model": body.get("model"),
        "n_messages": len(msgs),
        "roles": [m.get("role") for m in msgs],
        # 逐条 (role, 内容指纹)，用来验证请求前缀在两次压缩之间是否逐字节稳定
        "fp": [f"{m.get('role')}:"
               f"{hashlib.sha1(json.dumps(m.get('content'), ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:12]}"
               for m in msgs],
        "chars": sum(len(json.dumps(m.get("content"), ensure_ascii=False)) for m in msgs),
        "has_image": any(isinstance(m.get("content"), list) and
                         any(isinstance(p, dict) and p.get("type") == "image_url"
                             for p in m["content"]) for m in msgs),
        "system_preview": next((str(m.get("content"))[:120] for m in msgs
                                if m.get("role") == "system"), None),
        # 摘要请求的 user prompt（里面是被渲染成文本的那一批原文）
        "prompt": str(msgs[-1].get("content", ""))[:4000] if msgs else "",
        "stream": bool(body.get("stream")),
        "extra": {k: v for k, v in body.items()
                  if k not in ("model", "messages", "stream", "max_tokens")},
        "headers": {k: v for k, v in request.headers.items()
                    if k.lower() not in ("host", "content-length", "accept-encoding",
                                         "connection", "user-agent", "accept", "content-type")},
        "query": str(request.url.query),
    })

    if STATE["chat_status"] and not _is_summary(body):
        n_chat = sum(1 for c in STATE["calls"] if not str(c["model"]).startswith("mock-summary"))
        if n_chat >= max(1, int(STATE["chat_status_after"])):
            return JSONResponse(status_code=STATE["chat_status"],
                                content={"error": {"message": "upstream says no"}})

    for field, status in list(STATE["field_status"].items()):
        if field in body:
            if STATE["field_status_once"]:
                STATE["field_status"].pop(field, None)
            return JSONResponse(status_code=int(status),
                                content={"error": {"message": f"transient on {field}"}})

    if STATE["strict_body"]:
        unknown = [k for k in body if k not in KNOWN_BODY_KEYS]
        if unknown:
            return JSONResponse(status_code=400, content={"error": {
                "message": STATE["strict_message"].format(fields=", ".join(unknown)),
                "type": "invalid_request_error"}})

    hit = [k for k in STATE["reasoning_for"] if k in body]

    if _is_summary(body):
        STATE["summary_calls"] += 1
        exhausted = 0 <= STATE["fail_after"] < STATE["summary_calls"]
        if _is_backup(body):
            return _completion(STATE["summary_text"] or _fake_summary(msgs))
        if STATE["fail_next"] > 0 or exhausted:
            if STATE["fail_next"] > 0:
                STATE["fail_next"] -= 1
            headers = {"Retry-After": "0"} if STATE["fail_status"] == 429 else {}
            return JSONResponse(status_code=STATE["fail_status"],
                                content=STATE["fail_body"], headers=headers)
        if STATE["short_next"] > 0:
            STATE["short_next"] -= 1
            return _completion("太短")
        text = STATE["summary_text"] or _fake_summary(msgs)
        return _completion(text)

    reply = "好的，我记住了。" * 20
    if hit and not body.get("stream"):
        return _completion(reply, reasoning="让我想想……" * 3)
    if body.get("stream"):
        return StreamingResponse(_sse(reply, body.get("model", "mock-chat")),
                                 media_type="text/event-stream")
    return _completion(reply)


def _fake_summary(msgs: list[dict]) -> str:
    """短摘要：真实摘要模型是压缩的，测试里也必须比输入小得多。"""
    user = str(msgs[-1].get("content", "")) if msgs else ""
    return ("## 关键事实与专有名词\n"
            f"- 覆盖片段 #{len(user) % 997}，保留数值 12345 与名称 Alpha。")


def _completion(text: str, reasoning: str = "") -> JSONResponse:
    msg = {"role": "assistant", "content": text}
    usage = {"prompt_tokens": 1, "completion_tokens": 1}
    if reasoning:
        msg["reasoning_content"] = reasoning
        usage["completion_tokens_details"] = {"reasoning_tokens": len(reasoning)}
    return JSONResponse({
        "id": "cmpl-mock", "object": "chat.completion", "model": "mock",
        "choices": [{"index": 0, "message": msg, "finish_reason": "stop"}],
        "usage": usage,
    })


async def _sse(text: str, model: str):
    step = 37
    for i in range(0, len(text), step):
        obj = {"id": "chatcmpl-mock", "object": "chat.completion.chunk", "model": model,
               "choices": [{"index": 0, "delta": {"content": text[i:i + step]},
                            "finish_reason": None}]}
        yield f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode()
        await asyncio.sleep(0)
    obj = {"id": "chatcmpl-mock", "object": "chat.completion.chunk", "model": model,
           "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
    yield f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode()
    yield b"data: [DONE]\n\n"


def serve(port: int) -> threading.Thread:
    cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(cfg)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            return t
        time.sleep(0.05)
    return t


if __name__ == "__main__":
    import sys
    serve(int(sys.argv[1]) if len(sys.argv) > 1 else 9911)
    while True:
        time.sleep(3600)
