"""配置加载与热重载。

除 ``tokenizer.encoding`` 外的所有字段都在读取时才从 ``CONFIG`` 取值，
因此 ``/admin/reload`` 或 SIGHUP 后立即生效（含 ``per_message_overhead``）。
"""

from __future__ import annotations

import copy
import os
import re
import threading
from typing import Any

import yaml

CONFIG_PATH = os.environ.get("PROXY_CONFIG", "config.yaml")

_LOCK = threading.RLock()
_CONFIG: dict[str, Any] = {}
_PROVIDERS: dict[str, dict[str, Any]] = {}
_SECRETS: dict[str, str | None] = {}
# 页面上保存的提示词覆盖项（来源是数据库，不是 config.yaml）
_OVERRIDES: dict[str, str] = {}

DEFAULTS: dict[str, Any] = {
    "summary": {
        "enabled": True,
        "trigger_tokens": 39200,
        "keep_recent_tokens": 19200,
        "summary_total_cap_tokens": 12800,
        "summary_max_tokens": 2400,
        "summary_batch_tokens": 10000,
        "max_batches_per_request": 4,
        "exit_gate_ratio": 1.2,
        "checkpoint_keep": 10,
        "summary_role": "system",
        "persist_db": "logs/sessions.db",
        "cache_max_entries": 1024,
        "timeout_seconds": 180,
        "main_max_attempts": 2,
        "min_output_tokens": 50,
    },
    "observability": {"capture_timeline": False, "preview_chars": 60},
    "stream": {"smooth_chars": 24, "smooth_delay": 0.008, "flush_backlog_chars": 600},
    "tokenizer": {"encoding": "cl100k_base", "per_message_overhead": 12, "image_tokens": 1100},
    "server": {"host": "0.0.0.0", "port": 8787},
    "logging": {"level": "INFO", "file": "logs/proxy.log",
                "max_bytes": 20 * 1024 * 1024, "backup_count": 5},
}

FALLBACK_PROMPTS: dict[str, str] = {
    "batch_system": "你是一个对话历史压缩器，请把给到的对话片段压缩成不丢关键信息的结构化要点，禁止编造。",
    "recompress": "请对下面的摘要做无损精简：删冗余、并同类，保留全部事实与具体值，禁止编造。",
    "injection": "以下是本次对话更早部分的摘要，请当作你自己的记忆继续对话：\n\n{summary}",
    "fallback_notice": "\n\n【重要】用户可能从较早的消息处创建了分支，摘要与后续原文衔接处可能重叠或跳跃，冲突以原文为准。",
}


# 近期原文下限的硬上限：不得超过 trigger_tokens 的这个比例。
# 下限是"保不住就报错"的硬指标，它一旦逼近触发阈值就会和出口闸门打架——
# 压完一次剩不下多少余量，很快又触发，叠上摘要就顶穿闸门。
# 所以配置里写多大都没用，实际生效值在这里封顶。
KEEP_RECENT_MAX_RATIO = 0.5

# 可视化页面密钥的建议长度。短于这个只警告不拦截，但页面能看到全部摘要，别图省事。
UI_TOKEN_MIN_LEN = 16

# extra_body 里不允许出现的键：改了它们就不是"调参"而是把压缩本身绕过去了。
PROTECTED_BODY_KEYS = ("messages", "stream")

# forward_headers 永远不放行的头：鉴权头必须换成供应商的 key，
# 其余几个由 httpx 按实际请求重算，透传过去只会自相矛盾。
BLOCKED_HEADERS = {"authorization", "host", "content-length", "content-type",
                   "connection", "transfer-encoding", "cookie", "accept-encoding"}


class ConfigError(RuntimeError):
    pass


def _resolve_secret(env_name: str, cfg_value: Any) -> str | None:
    return os.environ.get(env_name) or (str(cfg_value).strip() if cfg_value else None) or None


def provider_env_name(name: str) -> str:
    return f"UPSTREAM_API_KEY__{re.sub(r'[^A-Za-z0-9]', '_', name).upper()}"


def _merge_defaults(cfg: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(cfg) if cfg else {}
    for section, defaults in DEFAULTS.items():
        node = out.setdefault(section, {}) or {}
        if not isinstance(node, dict):
            raise ConfigError(f"config.yaml 的 {section} 必须是一个映射")
        for k, v in defaults.items():
            node.setdefault(k, v)
        out[section] = node
    prompts = out["summary"].setdefault("prompts", {}) or {}
    # 兼容旧配置：二次重压从"分片 + 合并"两套提示词合并成了一套
    if "recompress" not in prompts and prompts.get("recompress_chunk"):
        prompts["recompress"] = prompts["recompress_chunk"]
    for k in ("recompress_chunk", "recompress_merge"):
        prompts.pop(k, None)
    for k, v in FALLBACK_PROMPTS.items():
        prompts.setdefault(k, v)
    out["summary"]["prompts"] = {k: v for k, v in prompts.items() if k in FALLBACK_PROMPTS}
    fb = out["summary"].get("fallback")
    if not isinstance(fb, dict):
        fb = {}
    fb.setdefault("enabled", False)
    fb.setdefault("max_attempts", 3)
    out["summary"]["fallback"] = fb
    return out


def _load_providers(cfg: dict[str, Any], warn) -> dict[str, dict[str, Any]]:
    raw = cfg.get("providers") or []
    if not raw:
        raise ConfigError("config.yaml 缺少 providers 列表，至少配置一个供应商")
    providers: dict[str, dict[str, Any]] = {}
    for p in raw:
        name = (p.get("name") or "").strip()
        if not name or not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise ConfigError(f"非法的 provider name: {name!r}")
        if name in providers:
            raise ConfigError(f"重复的 provider name: {name!r}")
        base_url = (p.get("base_url") or "").strip().rstrip("/")
        if not base_url:
            raise ConfigError(f"provider {name!r} 缺少 base_url")
        key = _resolve_secret(provider_env_name(name), p.get("api_key"))
        if not key:
            warn("provider %r 未配置 api_key", name)
        providers[name] = {
            "name": name,
            "base_url": base_url,
            "api_key": key,
            "timeout_seconds": p.get("timeout_seconds", 300),
            "connect_timeout_seconds": p.get("connect_timeout_seconds", 30),
            "multimodal": bool(p.get("multimodal", True)),
            "extra_body": _clean_extra_body(p.get("extra_body"), f"provider {name!r}", warn),
            "forward_headers": _clean_headers(p.get("forward_headers"), f"provider {name!r}", warn),
            "forward_query": bool(p.get("forward_query", False)),
        }
    return providers


def _clean_extra_body(raw: Any, who: str, warn) -> dict[str, Any]:
    """校验 extra_body：必须是映射，且不许覆盖 messages / stream。"""
    if not raw:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{who} 的 extra_body 必须是一个映射")
    out = dict(raw)
    for k in PROTECTED_BODY_KEYS:
        if k in out:
            warn("%s 的 extra_body 里的 %r 会被忽略：改它等于绕过压缩/破坏流式处理", who, k)
            out.pop(k)
    return out


def _clean_headers(raw: Any, who: str, warn) -> list[str]:
    """校验 forward_headers 白名单，剔除永远不该透传的头。"""
    if not raw:
        return []
    if not isinstance(raw, list):
        raise ConfigError(f"{who} 的 forward_headers 必须是一个列表")
    out = []
    for h in raw:
        h = str(h).strip().lower()
        if not h:
            continue
        if h in BLOCKED_HEADERS:
            warn("%s 的 forward_headers 里的 %r 会被忽略（鉴权/传输层的头不能透传）", who, h)
            continue
        out.append(h)
    return out


def reload(warn=lambda *a, **k: None) -> dict[str, Any]:
    """重新读取 config.yaml。校验失败时抛异常，调用方负责保留旧配置。"""
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    cfg = _merge_defaults(raw)
    providers = _load_providers(cfg, warn)
    trig, want = int(cfg["summary"]["trigger_tokens"]), int(cfg["summary"]["keep_recent_tokens"])
    hard = int(trig * KEEP_RECENT_MAX_RATIO)
    if want > hard:
        warn("keep_recent_tokens=%d 超过 trigger_tokens(%d) 的 %d%%，实际按 %d 生效"
             "（近期原文是硬性下限，留太多会和出口闸门打架）",
             want, trig, int(KEEP_RECENT_MAX_RATIO * 100), hard)
    secrets = {
        "summary": _resolve_secret("SUMMARY_API_KEY", cfg["summary"].get("api_key")),
        "summary_fallback": _resolve_secret("SUMMARY_FALLBACK_API_KEY",
                                            cfg["summary"]["fallback"].get("api_key")),
        "auth": _resolve_secret("PROXY_AUTH_TOKEN", cfg["server"].get("auth_token")),
        "ui": _resolve_secret("PROXY_UI_TOKEN", cfg["server"].get("ui_token")),
    }
    if secrets["ui"] and len(secrets["ui"]) < UI_TOKEN_MIN_LEN:
        warn("server.ui_token 只有 %d 位，建议至少 %d 位——这个页面能看到全部对话摘要，"
             "而且往往开在公网端口上（用 ./ctl.sh ui-token 生成一个）",
             len(secrets["ui"]), UI_TOKEN_MIN_LEN)
    with _LOCK:
        global _CONFIG, _PROVIDERS, _SECRETS
        _CONFIG, _PROVIDERS, _SECRETS = cfg, providers, secrets
    return {
        "providers": list(providers),
        "summary_model": cfg["summary"].get("model"),
        "summary_fallback_model": cfg["summary"]["fallback"].get("model")
        if cfg["summary"]["fallback"].get("enabled") else None,
        "auth_enabled": bool(secrets["auth"]),
    }


# ===== 读取接口（全部实时取值，保证热重载生效）=====
def cfg() -> dict[str, Any]:
    return _CONFIG


def summary() -> dict[str, Any]:
    return _CONFIG["summary"]


def keep_recent_tokens() -> int:
    """近期原文下限的**有效值**：配置值与 trigger×KEEP_RECENT_MAX_RATIO 取小。

    别处一律用这个函数，不要直接读 summary()["keep_recent_tokens"]。
    """
    s = summary()
    return min(int(s["keep_recent_tokens"]),
               int(int(s["trigger_tokens"]) * KEEP_RECENT_MAX_RATIO))


def prompts() -> dict[str, str]:
    """生效的提示词 = config.yaml 的值，被页面上保存的覆盖项盖住。

    覆盖项存在数据库里而不是回写 config.yaml——回写会把文件里的注释和排版冲掉，
    而这份配置的注释本身就是文档。想恢复成文件里的值，删掉覆盖项即可。
    """
    base = dict(_CONFIG["summary"]["prompts"])
    base.update({k: v for k, v in _OVERRIDES.items() if k in FALLBACK_PROMPTS and v})
    return base


def prompt_sources() -> dict[str, dict[str, Any]]:
    """给页面用：每条提示词的文件值、覆盖值、当前生效值。"""
    file_vals = _CONFIG["summary"]["prompts"]
    return {k: {"effective": _OVERRIDES.get(k) or file_vals.get(k, FALLBACK_PROMPTS[k]),
                "from_file": file_vals.get(k, FALLBACK_PROMPTS[k]),
                "overridden": bool(_OVERRIDES.get(k))}
            for k in FALLBACK_PROMPTS}


def set_prompt_overrides(overrides: dict[str, str]) -> None:
    with _LOCK:
        _OVERRIDES.clear()
        _OVERRIDES.update({k: v for k, v in (overrides or {}).items()
                           if k in FALLBACK_PROMPTS and isinstance(v, str) and v.strip()})


def prompt_overrides() -> dict[str, str]:
    return dict(_OVERRIDES)


def observability() -> dict[str, Any]:
    return _CONFIG["observability"]


def stream_cfg() -> dict[str, Any]:
    return _CONFIG["stream"]


def tokenizer_cfg() -> dict[str, Any]:
    return _CONFIG["tokenizer"]


def providers() -> dict[str, dict[str, Any]]:
    return _PROVIDERS


def provider(name: str) -> dict[str, Any] | None:
    return _PROVIDERS.get(name)


def auth_token() -> str | None:
    return _SECRETS.get("auth")


def ui_token() -> str | None:
    """可视化页面的独立密钥。留空 = 不启用页面（/ui 直接 404）。

    刻意和 server.auth_token 分开：那个要填进 chatbox、会跟着每个对话请求走，
    拿它当管理后台密码等于把后台钥匙散出去。ui_token 只能访问 /admin/*，
    不能拿去调 /chat/completions。
    """
    return _SECRETS.get("ui")


def summary_endpoints() -> list[dict[str, Any]]:
    """返回摘要模型调用链：[主, 备]。缺配置的条目会被过滤掉。"""
    s = summary()
    out: list[dict[str, Any]] = []
    if s.get("base_url") and _SECRETS.get("summary") and s.get("model"):
        out.append({
            "tag": "primary",
            "base_url": str(s["base_url"]).rstrip("/"),
            "api_key": _SECRETS["summary"],
            "model": s["model"],
            "max_attempts": max(1, int(s.get("main_max_attempts", 2))),
            "extra_body": _clean_extra_body(s.get("extra_body"), "summary", lambda *a: None),
        })
    fb = s.get("fallback") or {}
    if fb.get("enabled") and fb.get("base_url") and _SECRETS.get("summary_fallback") and fb.get("model"):
        out.append({
            "tag": "fallback",
            "base_url": str(fb["base_url"]).rstrip("/"),
            "api_key": _SECRETS["summary_fallback"],
            "model": fb["model"],
            # 备用模型没写 extra_body 就沿用主模型的（通常两边想关的思考是同一套）
            "extra_body": _clean_extra_body(fb.get("extra_body", s.get("extra_body")),
                                            "summary.fallback", lambda *a: None),
        })
    return out


def db_path() -> str | None:
    path = summary().get("persist_db")
    if not path:
        return None
    if not os.path.isabs(path):
        base = os.path.dirname(os.path.abspath(CONFIG_PATH)) or "."
        path = os.path.join(base, path)
    return path


def resolve_path(path: str) -> str:
    if os.path.isabs(path):
        return path
    base = os.path.dirname(os.path.abspath(CONFIG_PATH)) or "."
    return os.path.join(base, path)
