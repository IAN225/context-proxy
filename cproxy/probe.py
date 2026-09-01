"""向上游发一次最小请求，看它到底认什么。

两个地方用它：``tools/probe_body.py``（命令行逐字段探测）和 ``/admin/models/test``
（控制台保存模型配置前的验证）。判定规则只写一份，两边结论一致。

核心立场和 probe_body 一样：**证据不足就不下结论**。这里多一条——
中转站经常把自己的错误当成模型输出发回来（HTTP 200 + choices 里塞一句
"池子中没有可用账号"，或者干脆是 Cloudflare 的 HTML 页面）。
只看状态码会把这种当成成功，所以：

1. 标出可疑特征（``suspect``），但**不替用户下结论**；
2. 把**完整原始响应**带回去，让用户自己看模型到底吐了什么。
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

# 报错文本长这样才算"这家在校验未知字段"。各家措辞不同，覆盖常见几种。
UNKNOWN_FIELD_PAT = re.compile(
    r"unknown|unrecogni[sz]ed|unexpected|unsupported|not\s+(?:a\s+)?(?:permitted|allowed|supported)"
    r"|extra\s+(?:field|input|propert)|additional\s+propert|no\s+such\s+(?:field|parameter)"
    r"|invalid[^.;]{0,24}(?:field|parameter|argument|propert|key)"
    r"|未知|无法识别|不支持|不允许|非法参数|多余(?:的)?(?:字段|参数)",
    re.I)

# 这些结果是"这次没问上去"，不是"上游不认这个字段"
TRANSIENT = {0, 408, 409, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}

ACCEPT, REJECT, UNKNOWN = "accept", "reject", "unknown"

# 200 的正文里出现这些，多半是中转站把自己的故障当成模型输出发过来了。
# 只用来提示，不用来判失败——正常对话里也可能出现"error"这种词。
SUSPECT_PAT = re.compile(
    r"<!doctype\s+html|<html|cloudflare|bad\s+gateway|502\s+bad|504\s+gateway"
    r"|no\s+available\s+(?:account|channel|key)|insufficient|quota|余额|欠费"
    r"|池子|无可用|没有可用|渠道|上游负载|系统繁忙|请稍后",
    re.I)

# 一次探测发的最小请求：要求模型只回一个字，省钱也省时间
PROBE_PROMPT = "回答一个字：好"
PROBE_MAX_TOKENS = 16


@dataclass
class ProbeResult:
    """一次探测的结果。``raw`` 永远带着，供人工判读。"""
    name: str
    status: int = 0
    ok: bool = False
    kind: str = UNKNOWN
    note: str = ""                      # 失败时的上游原文（截断）
    raw: str = ""                       # 完整响应正文（截断到 raw_limit）
    content: str = ""                   # 解析出来的模型正文
    reasoning: str = ""                 # 解析出来的思考内容
    usage: dict[str, Any] = field(default_factory=dict)
    suspect: str = ""                   # 命中的可疑特征，空 = 没命中
    elapsed_ms: int = 0
    retried: bool = False
    request_body: dict[str, Any] = field(default_factory=dict)

    @property
    def reasoning_chars(self) -> int:
        return len(self.reasoning)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "ok": self.ok, "kind": self.kind,
                "note": self.note, "raw": self.raw, "content": self.content,
                "reasoning_chars": self.reasoning_chars, "reasoning": self.reasoning[:2000],
                "usage": self.usage, "suspect": self.suspect, "elapsed_ms": self.elapsed_ms,
                "retried": self.retried, "request_body": self.request_body}


def classify(status: int, ok: bool) -> str:
    """这次结果说明了什么。

    只有 2xx 和 400/422 能说明"上游认不认这个字段"；
    401/403/429/5xx/网络错误说明的是**这次没问上去**，必须区分对待，
    否则一次限流就会让人把一个好字段从 extra_body 里删掉。
    """
    if ok:
        return ACCEPT
    if status in (400, 422):
        return REJECT
    return UNKNOWN


def reasoning_of(data: dict) -> str:
    """把各家的思考字段捞出来，用来判断"思考到底开没开"。"""
    try:
        msg = data["choices"][0].get("message") or {}
    except (KeyError, IndexError, TypeError):
        return ""
    for k in ("reasoning_content", "reasoning", "thinking"):
        v = msg.get(k)
        if isinstance(v, str) and v.strip():
            return v
        if isinstance(v, list):                      # Anthropic 风格的 block 数组
            return " ".join(str(b.get("thinking", "")) for b in v if isinstance(b, dict))
    return ""


def content_of(data: dict) -> str:
    try:
        msg = data["choices"][0].get("message") or {}
    except (KeyError, IndexError, TypeError):
        return ""
    c = msg.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):                          # 内容块数组
        return "".join(str(b.get("text", "")) for b in c if isinstance(b, dict))
    return ""


async def probe_once(client: httpx.AsyncClient, url: str, headers: dict[str, str],
                     body: dict[str, Any], name: str, *, raw_limit: int = 4000) -> ProbeResult:
    t0 = time.time()
    safe_body = {k: v for k, v in body.items() if k != "messages"}
    try:
        r = await client.post(url, headers=headers, json=body)
    except Exception as e:                            # noqa: BLE001 网络层错误也是结果
        return ProbeResult(name=name, status=0, ok=False, kind=UNKNOWN,
                           note=f"请求失败: {type(e).__name__}: {e}",
                           elapsed_ms=int((time.time() - t0) * 1000), request_body=safe_body)

    ms = int((time.time() - t0) * 1000)
    text = r.text or ""
    res = ProbeResult(name=name, status=r.status_code, ok=r.is_success,
                      raw=text[:raw_limit], elapsed_ms=ms, request_body=safe_body)
    if not r.is_success:
        res.note = text.strip().replace("\n", " ")[:400]
        res.kind = classify(r.status_code, False)
        return res

    try:
        data = r.json()
    except ValueError:
        # 200 但不是 JSON —— 典型的中转站错误页，绝不能当成成功
        res.ok = False
        res.kind = UNKNOWN
        res.note = "HTTP 200 但响应不是 JSON（多半是网关的错误页）"
        res.suspect = "响应不是 JSON"
        return res

    res.content = content_of(data)
    res.reasoning = reasoning_of(data)
    res.usage = data.get("usage") or {}
    if not (data.get("choices") or []):
        res.ok = False
        res.kind = UNKNOWN
        res.note = "HTTP 200 但响应里没有 choices，不是一个正常的补全结果"
        res.suspect = "没有 choices"
        return res
    if (hit := SUSPECT_PAT.search(res.content)):
        # 不改 ok/kind：这只是提示，判断权交给看得到原文的人
        res.suspect = f"正文里出现可疑字样 {hit.group(0)!r}"
    res.kind = ACCEPT
    return res


async def probe(client: httpx.AsyncClient, url: str, headers: dict[str, str],
                body: dict[str, Any], name: str, *, retry_delay: float = 2.0,
                raw_limit: int = 4000) -> ProbeResult:
    """发一次；碰上限流/5xx/网络错误就再试一次，别让一次抖动变成一条结论。"""
    res = await probe_once(client, url, headers, body, name, raw_limit=raw_limit)
    if not res.ok and res.status in TRANSIENT:
        await asyncio.sleep(retry_delay)
        again = await probe_once(client, url, headers, body, name, raw_limit=raw_limit)
        again.retried = True
        res = again
    return res


def build_body(model: str, extra_body: dict[str, Any] | None = None, *,
               max_tokens: int = PROBE_MAX_TOKENS, max_tokens_field: str = "max_tokens",
               prompt: str = PROBE_PROMPT) -> dict[str, Any]:
    body: dict[str, Any] = {**(extra_body or {})}
    body.update({"model": model, "messages": [{"role": "user", "content": prompt}],
                 "stream": False})
    body.pop("max_tokens", None)
    body.pop("max_completion_tokens", None)
    body[max_tokens_field] = max_tokens
    return body


def headers_for(api_key: str) -> dict[str, str]:
    h = {"Content-Type": "application/json"}
    if api_key:
        h["Authorization"] = f"Bearer {api_key}"
    return h


# ===== 传参方言检测 =====
# 端点长什么样（/v1/chat/completions）说明不了传参风格：不少中转站用 OpenAI 的路径
# 转发 Claude，内部却只认 Anthropic 那套字段。所以**不看 URL，只看上游认不认**。
#
# 关闭思考的写法正好是两派分歧最大的地方，就拿它当探针：
DIALECT_PROBES: list[tuple[str, str, dict[str, Any]]] = [
    ("openai", "reasoning_effort=minimal", {"reasoning_effort": "minimal"}),
    ("openai", "reasoning_effort=none", {"reasoning_effort": "none"}),
    ("anthropic", "thinking=disabled", {"thinking": {"type": "disabled"}}),
    ("qwen", "enable_thinking=false", {"enable_thinking": False}),
]

# token 上限的字段名也分家：OpenAI 新模型只认 max_completion_tokens
TOKEN_FIELDS = ["max_tokens", "max_completion_tokens"]


async def detect_dialect(base_url: str, api_key: str, model: str, *,
                         timeout: float = 60.0, retry_delay: float = 2.0
                         ) -> dict[str, Any]:
    """探测这个端点认哪套传参，返回结论 + 全部原始结果。

    先跑一条不带任何附加字段的基线；基线不通就直接停——
    后面每个探针都会"失败"，那份清单没有意义（和 probe_body 一个道理）。
    """
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = headers_for(api_key)
    out: dict[str, Any] = {"model": model, "results": [], "dialect": None,
                           "thinking_off": None, "max_tokens_field": None, "notes": []}

    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=20.0, read=timeout,
                                                       write=timeout, pool=timeout)) as client:
        base = await probe(client, url, headers, build_body(model), "baseline",
                           retry_delay=retry_delay)
        out["results"].append(base.to_dict())
        if not base.ok:
            out["notes"].append(
                f"基线请求就没成功（{base.status}），探测中止。问题不在参数上，"
                "而在 api_key / 模型名 / base_url / 余额 / 网络其中之一。")
            return out
        if base.suspect:
            out["notes"].append(f"基线返回 200，但{base.suspect}——"
                                "请点开原始响应确认这是模型的输出，不是中转站的报错。")

        # 1) token 上限字段名：max_tokens 基线已经验过了，只在它被拒时才试新名字
        out["max_tokens_field"] = "max_tokens"
        alt = await probe(client, url, headers,
                          build_body(model, max_tokens_field="max_completion_tokens"),
                          "max_completion_tokens", retry_delay=retry_delay)
        out["results"].append(alt.to_dict())
        if alt.ok:
            out["notes"].append("max_tokens 和 max_completion_tokens 都收，用前者。")
        else:
            out["notes"].append(f"只认 max_tokens（max_completion_tokens 返回 {alt.status}）。")

        # 2) 关闭思考的写法 = 传参方言
        hits: list[str] = []
        for dialect, name, patch in DIALECT_PROBES:
            res = await probe(client, url, headers, build_body(model, patch), name,
                              retry_delay=retry_delay)
            out["results"].append(res.to_dict())
            if res.kind == ACCEPT:
                hits.append(dialect)
                if out["thinking_off"] is None:
                    out["thinking_off"] = patch
            elif res.kind == UNKNOWN:
                out["notes"].append(f"{name} 返回 {res.status}，这次没问出结果，不作数。")

        uniq = sorted(set(hits))
        if not uniq:
            out["dialect"] = "unknown"
            out["notes"].append("四种关思考的写法都被拒了：这个模型可能压根不带思考，"
                                "也可能这家用了别的字段名。摘要模型不填思考开关也能用。")
        elif len(uniq) == 1:
            out["dialect"] = uniq[0]
            out["notes"].append(f"只有 {uniq[0]} 那套写法被接受，按它来。")
        else:
            # 宽松网关：什么都收。这时 200 不代表生效，只能挑一个最常见的
            out["dialect"] = "permissive"
            out["notes"].append(
                f"{'、'.join(uniq)} 几套写法都返回 200 —— 这家不校验未知字段，"
                "「收下了」不等于「生效了」。已按 OpenAI 那套填，"
                "真要确认思考关没关，看各次响应的 reasoning 字数差别。")
            for d, _n, patch in DIALECT_PROBES:
                if d == "openai":
                    out["thinking_off"] = patch
                    break
    return out
