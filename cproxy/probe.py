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
INLINE_THINK_PAT = re.compile(
    r"<(?:think|thinking)\b[^>]*>(.*?)</(?:think|thinking)>", re.I | re.S)


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
    request_body: dict[str, Any] = field(default_factory=dict)       # 不含 messages，供摘要展示
    request_payload: dict[str, Any] = field(default_factory=dict)    # 完整请求体，供二级折叠查看
    input_text: str = ""                                            # 探测输入，供可读视图展示

    @property
    def reasoning_chars(self) -> int:
        return len(self.reasoning)

    @property
    def reasoning_tokens(self) -> int:
        """兼容各家 usage 结构，取其中最大的 reasoning_tokens 信号。"""
        found: list[int] = []

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    if key == "reasoning_tokens" and isinstance(item, (int, float)):
                        found.append(max(0, int(item)))
                    else:
                        walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)

        walk(self.usage)
        return max(found, default=0)

    @property
    def has_reasoning(self) -> bool:
        return bool(self.reasoning.strip()) or self.reasoning_tokens > 0

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "ok": self.ok, "kind": self.kind,
                "note": self.note, "raw": self.raw, "content": self.content,
                "reasoning_chars": self.reasoning_chars, "reasoning": self.reasoning[:2000],
                "reasoning_tokens": self.reasoning_tokens,
                "has_reasoning": self.has_reasoning,
                "usage": self.usage, "suspect": self.suspect, "elapsed_ms": self.elapsed_ms,
                "retried": self.retried, "request_body": self.request_body,
                "request_payload": self.request_payload, "input": self.input_text}


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
    input_text = "\n".join(
        str(m.get("content", "")) for m in (body.get("messages") or []) if isinstance(m, dict))
    try:
        r = await client.post(url, headers=headers, json=body)
    except Exception as e:                            # noqa: BLE001 网络层错误也是结果
        return ProbeResult(name=name, status=0, ok=False, kind=UNKNOWN,
                           note=f"请求失败: {type(e).__name__}: {e}",
                           elapsed_ms=int((time.time() - t0) * 1000), request_body=safe_body,
                           request_payload=body, input_text=input_text)

    ms = int((time.time() - t0) * 1000)
    text = r.text or ""
    res = ProbeResult(name=name, status=r.status_code, ok=r.is_success,
                      raw=text[:raw_limit], elapsed_ms=ms, request_body=safe_body,
                      request_payload=body, input_text=input_text)
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
    if not res.reasoning and (inline := INLINE_THINK_PAT.search(res.content)):
        res.reasoning = inline.group(1).strip()
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
               max_tokens: int = PROBE_MAX_TOKENS,
               max_tokens_field: str | None = "max_tokens",
               prompt: str = PROBE_PROMPT) -> dict[str, Any]:
    body: dict[str, Any] = {**(extra_body or {})}
    body.update({"model": model, "messages": [{"role": "user", "content": prompt}],
                 "stream": False})
    body.pop("max_tokens", None)
    body.pop("max_completion_tokens", None)
    if max_tokens_field:
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
                           "thinking_off": None, "suggested_extra_body": None,
                           "thinking_off_evidence": None,
                           "max_tokens_field": None, "notes": []}

    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=20.0, read=timeout,
                                                       write=timeout, pool=timeout)) as client:
        # 基线只验证端点、密钥与模型，不携带任何 token 上限或方言探针字段。
        base = await probe(client, url, headers,
                           build_body(model, max_tokens_field=None), "baseline",
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

        # 1) token 上限字段名：与无上限字段的基线分开，两个候选字段各测一次。
        token_results: dict[str, ProbeResult] = {}
        for field in TOKEN_FIELDS:
            res = await probe(client, url, headers,
                              build_body(model, max_tokens_field=field), field,
                              retry_delay=retry_delay)
            token_results[field] = res
            out["results"].append(res.to_dict())

        accepted_token_fields = [field for field in TOKEN_FIELDS if token_results[field].ok]
        if "max_tokens" in accepted_token_fields:
            out["max_tokens_field"] = "max_tokens"
        elif "max_completion_tokens" in accepted_token_fields:
            out["max_tokens_field"] = "max_completion_tokens"

        if len(accepted_token_fields) == 2:
            out["notes"].append(
                "max_tokens 与 max_completion_tokens 均返回成功；默认使用 max_tokens。")
        elif accepted_token_fields:
            out["notes"].append(
                f"已确认 token 上限字段为 {accepted_token_fields[0]}。")
        else:
            states = "、".join(
                f"{field}={token_results[field].status}" for field in TOKEN_FIELDS)
            out["notes"].append(
                f"两个 token 上限字段均未验证成功（{states}），不自动修改当前配置。")

        # 2) 关闭思考的写法 = 传参方言
        accepted: list[tuple[str, str, dict[str, Any], ProbeResult]] = []
        for dialect, name, patch in DIALECT_PROBES:
            res = await probe(
                client, url, headers,
                build_body(model, patch, max_tokens_field=out["max_tokens_field"]), name,
                              retry_delay=retry_delay)
            out["results"].append(res.to_dict())
            if res.kind == ACCEPT:
                accepted.append((dialect, name, patch, res))
            elif res.kind == UNKNOWN:
                out["notes"].append(f"{name} 返回 {res.status}，这次没问出结果，不作数。")

        uniq = sorted({item[0] for item in accepted})
        if not uniq:
            out["dialect"] = "unknown"
            out["notes"].append("四种关思考的写法都被拒了：这个模型可能压根不带思考，"
                                "也可能这家用了别的字段名。摘要模型不填思考开关也能用。")
        elif len(uniq) == 1:
            out["dialect"] = uniq[0]
            out["notes"].append(f"只有 {uniq[0]} 方言的写法被接受。")
        else:
            # 宽松网关：什么都收。此时必须依靠响应中的实际思考信号继续判断。
            out["dialect"] = "permissive"
            out["notes"].append(
                f"{'、'.join(uniq)} 几套写法都返回 200 —— 这家不校验未知字段，"
                "字段被接收不代表实际生效，将继续比较各次响应中的思考信号。")

        # 自动填写优先依赖真实输出差异：同一个字段的一种取值有思考、另一种没有时，
        # 无思考的取值才算被证明有效。若差异发生在不同字段之间，也保留该证据，
        # 但优先选择与“有思考”对照使用同一字段的候选项。
        with_reasoning = [item for item in accepted if item[3].has_reasoning]
        without_reasoning = [item for item in accepted if not item[3].has_reasoning]
        chosen: tuple[str, str, dict[str, Any], ProbeResult] | None = None
        if without_reasoning and (with_reasoning or base.has_reasoning):
            reasoning_keys = {next(iter(item[2]), "") for item in with_reasoning}
            same_field = [item for item in without_reasoning
                          if next(iter(item[2]), "") in reasoning_keys]
            chosen = (same_field or without_reasoning)[0]
            out["thinking_off_evidence"] = "response_difference"
            positive_names = [item[1] for item in with_reasoning]
            if base.has_reasoning:
                positive_names.insert(0, "baseline")
            out["notes"].append(
                f"检测到实际输出差异：{'、'.join(positive_names)} 包含思考内容，"
                f"{chosen[1]} 不包含思考内容；已确认后者的关闭思考参数生效。")
        elif len(accepted) == 1:
            # 只有一个写法被严格接收时，字段有效性已有上游校验作为证据；
            # 模型本身不产生思考时无法再做输出对照，仍保留原有自动填写行为。
            chosen = accepted[0]
            out["thinking_off_evidence"] = "only_accepted"
            out["notes"].append(
                f"仅 {chosen[1]} 被接受，已将该参数作为可用写法。")
        elif accepted:
            out["notes"].append(
                "各写法的思考输出没有形成可验证差异，因此不自动填写关闭思考参数。")

        if chosen:
            out["thinking_off"] = chosen[2]
            out["suggested_extra_body"] = chosen[2]
    return out
