"""消息层：token 估算、逐条指纹、轮次切分、上游消息净化。

三条口径统一在这里，别处不要再自己数 token 或算哈希：

1. **token 口径**：`per_message_overhead + role + content`，文本走 tiktoken，
   图片按 `tokenizer.image_tokens` 估值，`tool_calls` 按序列化文本估值。
   `count_tokens()` 就是 `msg_tokens()` 的求和，两者永远一致。

2. **指纹口径**：`sha256(role + 规范化内容)` 取前 16 位。
   图片**不把 URL/base64 计入指纹**，只留一个稳定占位符——
   某些客户端会把云端已删除图片的 base64 换成私有 id，若计入指纹会造成整段历史假分叉。

3. **轮次定义**：一轮 = 从一条 user 消息开始，到下一条 user 消息前（不含）为止；
   末尾未收束的自成一轮；第一条 user 之前的 system/tool 等消息归入其后的第一轮。
   压缩的切点只允许落在轮边界上，禁止在一轮中间（例如 assistant 与其 tool 返回之间）切开。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import OrderedDict
from typing import Any, Iterable

import tiktoken

from . import config

SUMMARY_TAG = "[CONTEXT_SUMMARY]"
BRANCH_TAG = "[CONTEXT_BRANCH_WARNING]"

_IMAGE_TYPES = {"image_url", "image", "input_image"}
_AUDIO_TYPES = {"input_audio", "audio"}

_ENC: Any = None
_ENC_NAME: str | None = None

# 文本 -> token 数缓存。key 是内容指纹，value 是"不含 overhead/role"的纯内容 token 数，
# 这样 per_message_overhead 热重载后缓存依然有效。
_TOK_CACHE: "OrderedDict[str, int]" = OrderedDict()
_TOK_CACHE_MAX = 20000


def encoding():
    global _ENC, _ENC_NAME
    name = str(config.tokenizer_cfg().get("encoding", "cl100k_base"))
    if _ENC is None:
        _ENC, _ENC_NAME = tiktoken.get_encoding(name), name
    return _ENC


def _encode_len(text: str) -> int:
    if not text:
        return 0
    return len(encoding().encode(text, disallowed_special=()))


def text_tokens(text: str) -> int:
    """带缓存的纯文本 token 数（摘要正文这类反复计算的长文本受益明显）。"""
    if not text:
        return 0
    key = hashlib.sha256(text.encode("utf-8")).hexdigest()
    hit = _TOK_CACHE.get(key)
    if hit is not None:
        _TOK_CACHE.move_to_end(key)
        return hit
    n = _encode_len(text)
    _TOK_CACHE[key] = n
    _TOK_CACHE.move_to_end(key)
    while len(_TOK_CACHE) > _TOK_CACHE_MAX:
        _TOK_CACHE.popitem(last=False)
    return n


# ===== 内容规范化 =====
def _image_ref(part: dict) -> str:
    src = part.get("image_url")
    if isinstance(src, dict):
        return str(src.get("url") or "")
    if isinstance(src, str):
        return src
    return str(part.get("url") or "")


def is_valid_image_ref(url: str) -> bool:
    """只有 data: URI 和 http(s) URL 才是上游能消费的图片引用。

    客户端删掉云端文件后常把 base64 替换成自家的私有 id（形如 `file-xxx` / 裸 uuid），
    这种引用发给上游必定报错，转发前要换成占位文本。
    """
    u = (url or "").strip()
    if not u:
        return False
    low = u.lower()
    return low.startswith("data:image/") or low.startswith("http://") or low.startswith("https://")


def content_to_text(content: Any, *, for_summary: bool = False) -> str:
    """把 content 规范化成纯文本；非文本 part 用稳定占位符表示。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                out.append(str(part))
                continue
            ptype = part.get("type")
            if ptype == "text" or (ptype is None and "text" in part):
                out.append(str(part.get("text") or ""))
            elif ptype in _IMAGE_TYPES:
                out.append("[图片]")
            elif ptype in _AUDIO_TYPES:
                out.append("[音频]")
            else:
                out.append(f"[{ptype or '未知内容'}]")
        return "\n".join(x for x in out if x)
    if isinstance(content, dict):
        return content_to_text([content], for_summary=for_summary)
    return str(content)


def _tool_calls_text(m: dict) -> str:
    calls = m.get("tool_calls")
    if not calls:
        return ""
    chunks = []
    for c in calls if isinstance(calls, list) else []:
        fn = (c or {}).get("function") or {}
        name = fn.get("name") or (c or {}).get("name") or "?"
        args = fn.get("arguments")
        if not isinstance(args, str):
            args = json.dumps(args, ensure_ascii=False, sort_keys=True) if args is not None else ""
        chunks.append(f"[调用工具 {name}({args})]")
    return "\n".join(chunks)


def message_text(m: dict) -> str:
    """送给摘要模型 / 参与指纹计算的消息文本表示。"""
    parts = [content_to_text(m.get("content"))]
    tc = _tool_calls_text(m)
    if tc:
        parts.append(tc)
    if m.get("role") == "tool":
        parts.insert(0, "[工具返回]")
    return "\n".join(p for p in parts if p)


def count_image_parts(m: dict) -> int:
    content = m.get("content")
    if not isinstance(content, list):
        return 0
    return sum(1 for p in content
               if isinstance(p, dict) and p.get("type") in _IMAGE_TYPES)


# ===== token =====
def msg_tokens(m: dict) -> int:
    t = config.tokenizer_cfg()
    overhead = int(t.get("per_message_overhead", 12))
    image_tokens = int(t.get("image_tokens", 1100))
    total = overhead + text_tokens(str(m.get("role") or ""))
    total += text_tokens(content_to_text(m.get("content")))
    total += count_image_parts(m) * image_tokens
    tc = _tool_calls_text(m)
    if tc:
        total += text_tokens(tc)
    if m.get("name"):
        total += text_tokens(str(m["name"]))
    return total


def tool_tokens(m: dict) -> int:
    """这条消息里由工具调用产生的 token：tool 消息整条算，assistant 只算 tool_calls 部分。

    只用于报错时的归因诊断，不参与任何阈值计算。
    """
    if m.get("role") == "tool":
        return msg_tokens(m)
    tc = _tool_calls_text(m)
    return text_tokens(tc) if tc else 0


def count_tokens(messages: Iterable[dict]) -> int:
    return sum(msg_tokens(m) for m in messages)


# ===== 指纹 =====
def msg_fingerprint(m: dict) -> str:
    h = hashlib.sha256()
    h.update((str(m.get("role") or "")).encode("utf-8"))
    h.update(b"\x00")
    h.update(message_text(m).encode("utf-8"))
    n = count_image_parts(m)
    if n:
        h.update(f"\x00img:{n}".encode("utf-8"))
    return h.hexdigest()[:16]


class MsgInfo:
    """一条消息的派生信息，整个请求周期内只算一次。"""

    __slots__ = ("index", "role", "fp", "tokens", "chars")

    def __init__(self, index: int, role: str, fp: str, tokens: int, chars: int):
        self.index, self.role, self.fp, self.tokens, self.chars = index, role, fp, tokens, chars


def analyze(messages: list[dict]) -> list[MsgInfo]:
    infos: list[MsgInfo] = []
    for i, m in enumerate(messages):
        txt = message_text(m)
        infos.append(MsgInfo(i, str(m.get("role") or ""), msg_fingerprint(m),
                             msg_tokens(m), len(txt)))
    return infos


def signature(infos: list[MsgInfo]) -> list[str]:
    return [i.fp for i in infos]


def tokens_of(infos: list[MsgInfo], start: int, end: int) -> int:
    return sum(i.tokens for i in infos[start:end])


# ===== 会话身份 =====
def conv_key(messages: list[dict], first_n: int = 5) -> str:
    """精确会话键：前 N 条 user 消息的全文（不截断、不筛选）拼接后哈希。"""
    h = hashlib.sha256()
    cnt = 0
    for m in messages:
        if m.get("role") != "user":
            continue
        h.update(message_text(m).encode("utf-8"))
        h.update(b"\x1e")
        cnt += 1
        if cnt >= first_n:
            break
    h.update(f"|n={cnt}".encode("utf-8"))
    return h.hexdigest()


MIN_ANCHOR_CHARS = 20


def pick_anchors(infos: list[MsgInfo]) -> list[str]:
    """5 个锚点指纹：第 1、第 2 条合格消息 + 25%/50%/75% 位置往后的第一条合格消息。

    合格 = 文本长度 ≥ 20 字。锚点匹配不依赖顺序（部分客户端会打乱消息顺序）。
    """
    n = len(infos)
    if n == 0:
        return []
    qualified = [i for i in infos if i.chars >= MIN_ANCHOR_CHARS]
    anchors: list[str] = []
    for info in qualified[:2]:
        anchors.append(info.fp)
    for ratio in (0.25, 0.5, 0.75):
        start = int(n * ratio)
        for info in infos[start:]:
            if info.chars >= MIN_ANCHOR_CHARS:
                anchors.append(info.fp)
                break
    # 去重但保持顺序
    seen: set[str] = set()
    out = []
    for fp in anchors:
        if fp not in seen:
            seen.add(fp)
            out.append(fp)
    return out


def legacy_conv_id(body: list[dict]) -> str:
    """旧版本的会话指纹算法，仅用于迁移旧 sessions 表时认亲。

    旧算法：前 3 条非 system 消息，每条取内容文本的前 500 **字节**。
    """
    h = hashlib.sha256()
    cnt = 0
    for m in body:
        if m.get("role") == "system":
            continue
        h.update(content_to_text(m.get("content")).encode("utf-8")[:500])
        h.update(b"\x00")
        cnt += 1
        if cnt >= 3:
            break
    return h.hexdigest()


def legacy_boundary_fp(m: dict) -> str:
    return hashlib.sha256(content_to_text(m.get("content")).encode("utf-8")).hexdigest()[:16]


# ===== 轮次 =====
def split_rounds(messages: list[dict]) -> list[tuple[int, int]]:
    """返回 [(start, end), ...]，左闭右开。见模块头部的"轮"定义。"""
    if not messages:
        return []
    starts = [i for i, m in enumerate(messages) if m.get("role") == "user"]
    if not starts:
        return [(0, len(messages))]
    starts = [0] + starts[1:]          # 首轮吞掉开头的 system/tool
    return [(starts[k], starts[k + 1] if k + 1 < len(starts) else len(messages))
            for k in range(len(starts))]


def round_of_index(rounds: list[tuple[int, int]], index: int) -> int:
    """消息下标 -> 该下标所在的轮号（1 起）。越界则返回最后一轮 + 1。"""
    for r, (s, e) in enumerate(rounds):
        if s <= index < e:
            return r + 1
    return len(rounds) + 1


def rounds_before(rounds: list[tuple[int, int]], index: int) -> int:
    """下标 index 之前**已完整结束**的轮数，即"已压缩到第 N 轮"里的 N。"""
    return sum(1 for _s, e in rounds if e <= index)


def round_boundary_at_or_after(rounds: list[tuple[int, int]], index: int) -> int:
    for s, _e in rounds:
        if s >= index:
            return s
    return rounds[-1][1] if rounds else index


# ===== 上游消息净化 =====
_SUMMARY_PREFIXES = (SUMMARY_TAG, BRANCH_TAG)


def is_injected(m: dict) -> bool:
    c = m.get("content")
    if isinstance(c, str):
        return c.lstrip().startswith(_SUMMARY_PREFIXES)
    if isinstance(c, list):
        for p in c:
            if isinstance(p, dict) and p.get("type") == "text":
                return str(p.get("text") or "").lstrip().startswith(_SUMMARY_PREFIXES)
            return False
    return False


def split_head_system(messages: list[dict]) -> tuple[list[dict], list[dict]]:
    """拆出开头连续的 system 块（剔除我们上一轮注入的摘要），其余为 body。"""
    head: list[dict] = []
    i = 0
    while i < len(messages) and messages[i].get("role") == "system":
        if not is_injected(messages[i]):
            head.append(messages[i])
        i += 1
    body = [m for m in messages[i:] if not is_injected(m)]
    return head, body


def _sanitize_content(content: Any, multimodal: bool, stats: dict) -> Any:
    if not isinstance(content, list):
        return content
    out: list[Any] = []
    changed = False
    for part in content:
        if not isinstance(part, dict) or part.get("type") not in _IMAGE_TYPES:
            out.append(part)
            continue
        ref = _image_ref(part)
        if not multimodal:
            stats["images_stripped"] = stats.get("images_stripped", 0) + 1
            out.append({"type": "text", "text": "[图片]"})
            changed = True
        elif not is_valid_image_ref(ref):
            stats["images_invalid"] = stats.get("images_invalid", 0) + 1
            out.append({"type": "text", "text": "[图片（原文件已失效）]"})
            changed = True
        else:
            out.append(part)
    if not changed:
        return content
    # 全是文本了就折叠成字符串，兼容只认字符串 content 的上游
    if all(isinstance(p, dict) and p.get("type") == "text" for p in out):
        return "\n".join(str(p.get("text") or "") for p in out)
    return out


def sanitize_for_upstream(messages: list[dict], multimodal: bool) -> tuple[list[dict], dict]:
    """转发前的最后一道净化：

    - 非多模态供应商：把图片换成 ``[图片]`` 文本；
    - 多模态供应商：把失效的图片引用（既不是 data: 也不是 http(s)）换成占位文本；
    - 丢弃找不到对应 ``tool_calls`` 的孤儿 ``tool`` 消息（否则上游 400）。
    """
    stats: dict[str, int] = {}
    known_ids: set[str] = set()
    out: list[dict] = []
    for m in messages:
        role = m.get("role")
        if role == "tool":
            tcid = m.get("tool_call_id")
            if tcid and tcid not in known_ids:
                stats["orphan_tool_dropped"] = stats.get("orphan_tool_dropped", 0) + 1
                continue
        if role == "assistant" and isinstance(m.get("tool_calls"), list):
            for c in m["tool_calls"]:
                if isinstance(c, dict) and c.get("id"):
                    known_ids.add(c["id"])
        new_content = _sanitize_content(m.get("content"), multimodal, stats)
        if new_content is not m.get("content"):
            m = {**m, "content": new_content}
        out.append(m)
    return out, stats


def render_for_summary(messages: list[dict]) -> str:
    """把一段原文渲染成送进摘要模型的纯文本。"""
    lines = []
    for m in messages:
        role = m.get("role") or "user"
        lines.append(f"[{role}]: {message_text(m)}")
    return "\n\n".join(lines)


_WS = re.compile(r"[ \t]+\n")


def tidy(text: str) -> str:
    return _WS.sub("\n", (text or "").strip())
