"""配置加载与热重载。

除 ``tokenizer.encoding`` 外的所有字段都在读取时才从 ``CONFIG`` 取值，
因此 ``/admin/reload`` 或 SIGHUP 后立即生效（含 ``per_message_overhead``）。
"""

from __future__ import annotations

import copy
import os
import re
import shutil
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
        "summary_max_tokens": 2048,
        # token 上限的字段名：OpenAI 新模型只认 max_completion_tokens。
        # 别按 URL 猜，用 /admin/models 的「测试」按钮探一次，探到什么填什么。
        "max_tokens_field": "max_tokens",
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

# 发给摘要模型的提示词是**整条 user 消息的模板**（不再拆成 system + 硬编码包装语），
# 待处理的正文由 {{context}} 占位符插入，用户在页面上能改的就是这一整段。
CONTEXT_VAR = "{{context}}"

# 缺 {{context}} 的旧提示词自动补上的尾巴（保证老配置升级后照样能跑）
CONTEXT_TAIL = {
    "batch": "\n\n以下是待压片段\n" + CONTEXT_VAR,
    "recompress": "\n\n以下是待压片段\n" + CONTEXT_VAR,
}

FALLBACK_PROMPTS: dict[str, str] = {
    "batch": ("你是一个对话历史压缩器，请把给到的对话片段压缩成不丢关键信息的结构化要点，禁止编造。"
              + CONTEXT_TAIL["batch"]),
    "recompress": ("请对下面的摘要做无损精简：删冗余、并同类，保留全部事实与具体值，禁止编造。"
                   + CONTEXT_TAIL["recompress"]),
    "injection": "以下是本次对话更早部分的摘要，请当作你自己的记忆继续对话：\n\n{{summary}}",
    "fallback_notice": "\n\n【重要】用户可能从较早的消息处创建了分支，摘要与后续原文衔接处可能重叠或跳跃，冲突以原文为准。",
}

# 发给摘要模型的两条必须带 {{context}}；注入语必须带 {{summary}}（兼容旧的单花括号写法）
REQUIRED_VAR: dict[str, tuple[str, ...]] = {
    "batch": (CONTEXT_VAR,),
    "recompress": (CONTEXT_VAR,),
    "injection": ("{{summary}}", "{summary}"),
}

# 旧键名 -> 新键名。batch_system 时代提示词是走 system 角色的，现在整条走 user
LEGACY_PROMPT_KEYS = {"batch_system": "batch", "recompress_chunk": "recompress"}


# 近期原文下限的硬上限：不得超过 trigger_tokens 的这个比例。
# 下限是"保不住就报错"的硬指标，它一旦逼近触发阈值就会和出口闸门打架——
# 压完一次剩不下多少余量，很快又触发，叠上摘要就顶穿闸门。
# 所以配置里写多大都没用，实际生效值在这里封顶。
KEEP_RECENT_MAX_RATIO = 0.5

# 可视化页面密钥的建议长度。短于这个只警告不拦截，但页面能看到全部摘要，别图省事。
UI_TOKEN_MIN_LEN = 16

# extra_body 里不允许出现的键：改了它们就不是"调参"而是把压缩本身绕过去了。
PROTECTED_BODY_KEYS = ("messages", "stream")

# 摘要模型没配 extra_body 时的兜底：摘要是"照着原文复述要点"，
# 思考没什么用还慢又贵。关思考的字段名各家不同，所以这里只留一个空模板，
# 由 /admin/models 探测后填进去；探不出来就什么都不加（不加也能用）。
SUMMARY_DEFAULT_MAX_TOKENS = 2048

# 合法的 token 上限字段名
MAX_TOKENS_FIELDS = ("max_tokens", "max_completion_tokens")

# provider name 会进 URL，只允许这些字符
NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")

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
    # 兼容旧配置：batch_system -> batch（角色从 system 换成 user），
    # 二次重压从"分片 + 合并"两套提示词合并成了一套
    for old_k, new_k in LEGACY_PROMPT_KEYS.items():
        if new_k not in prompts and prompts.get(old_k):
            prompts[new_k] = prompts[old_k]
    for k in ("batch_system", "recompress_chunk", "recompress_merge"):
        prompts.pop(k, None)
    for k, v in FALLBACK_PROMPTS.items():
        prompts.setdefault(k, v)
    out["summary"]["prompts"] = {k: normalize_prompt(k, v)
                                 for k, v in prompts.items() if k in FALLBACK_PROMPTS}
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
        if not name or not NAME_RE.match(name):
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
    if not secrets["auth"]:
        warn("server.auth_token 为空 = 不鉴权：任何人碰得到这个端口就能用你的上游额度。"
             "仓库里的 config.yaml 默认留空是为了不提交真实密钥，自己填一个，"
             "或用环境变量 PROXY_AUTH_TOKEN")
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


def max_tokens_field() -> str:
    """摘要调用该用哪个字段名发 token 上限。写错会被 OpenAI 新模型直接 400。"""
    v = str(summary().get("max_tokens_field", "max_tokens") or "max_tokens")
    return v if v in MAX_TOKENS_FIELDS else "max_tokens"


def summary() -> dict[str, Any]:
    return _CONFIG["summary"]


def keep_recent_tokens() -> int:
    """近期原文下限的**有效值**：配置值与 trigger×KEEP_RECENT_MAX_RATIO 取小。

    别处一律用这个函数，不要直接读 summary()["keep_recent_tokens"]。
    """
    s = summary()
    return min(int(s["keep_recent_tokens"]),
               int(int(s["trigger_tokens"]) * KEEP_RECENT_MAX_RATIO))


def normalize_prompt(name: str, text: str) -> str:
    """补齐必需占位符。老配置里的提示词没有 {{context}}，直接用会把正文丢掉。"""
    text = (text or "").rstrip()
    if name in CONTEXT_TAIL and CONTEXT_VAR not in text:
        text += CONTEXT_TAIL[name]
    return text


def check_prompt(name: str, text: str) -> str | None:
    """校验一条提示词，返回错误说明；None = 通过。"""
    if not isinstance(text, str) or not text.strip():
        return f"{name} 不能为空"
    need = REQUIRED_VAR.get(name)
    if need and not any(v in text for v in need):
        return (f"{name} 必须包含 {need[0]} 占位符"
                + ("（待压正文插在这里，没有它模型就只收到一句指令）"
                   if need[0] == CONTEXT_VAR else "（摘要正文插在这里）"))
    return None


def render_prompt(name: str, context: str) -> str:
    """把 {{context}} 换成正文，得到发给摘要模型的那条 user 消息。"""
    return prompts()[name].replace(CONTEXT_VAR, context)


def prompts() -> dict[str, str]:
    """生效的提示词 = config.yaml 的值，被内存里的覆盖项盖住。

    页面上保存时会**直接写回 config.yaml**（只替换提示词正文，保留文件里的注释），
    覆盖项只在写文件失败时兜底，让这次修改仍然当场生效。
    """
    base = dict(_CONFIG["summary"]["prompts"])
    base.update({k: normalize_prompt(k, v) for k, v in _OVERRIDES.items()
                 if k in FALLBACK_PROMPTS and v})
    return base


def prompt_sources() -> dict[str, dict[str, Any]]:
    """给页面用：每条提示词的文件值、覆盖值、当前生效值。"""
    file_vals = _CONFIG["summary"]["prompts"]
    return {k: {"effective": _OVERRIDES.get(k) or file_vals.get(k, FALLBACK_PROMPTS[k]),
                "from_file": file_vals.get(k, FALLBACK_PROMPTS[k]),
                "overridden": bool(_OVERRIDES.get(k)),
                "requires": list(REQUIRED_VAR.get(k, ()))[:1]}
            for k in FALLBACK_PROMPTS}


def write_prompts_to_file(new_vals: dict[str, str]) -> list[str]:
    """把提示词写回 config.yaml，**只替换提示词正文，文件其余部分一字不动**。

    不用 yaml.safe_dump 整份重写：那会把全文的注释和排版全冲掉，
    而这份配置的注释本身就是文档。所以按行定位 `    <name>: |` 的块，
    只换掉它下面那段缩进正文。写之前先落一份 config.yaml.bak。

    返回实际改动的提示词名；抛异常表示没写成（调用方应退回内存覆盖项）。
    """
    path = os.path.abspath(CONFIG_PATH)
    with open(path, encoding="utf-8") as f:
        lines = f.read().split("\n")

    changed: list[str] = []
    for name, text in new_vals.items():
        if name not in FALLBACK_PROMPTS:
            continue
        start = _find_prompt_line(lines, name)
        body = [("      " + ln).rstrip() for ln in normalize_prompt(name, text).split("\n")]
        if start is None:
            # 文件里没有这条（配置压根没写 prompts、或者只写了其中几条）：整块追加进去
            anchor = _find_prompts_block_end(lines)
            lines[anchor:anchor] = [f"    {name}: |"] + body
        else:
            end = _prompt_body_end(lines, start)
            lines[start] = f"    {name}: |"
            lines[start + 1:end] = body
        changed.append(name)

    text_out = "\n".join(lines)
    yaml.safe_load(text_out)          # 写坏了宁可抛异常，也不能把配置文件毁掉
    try:
        shutil.copyfile(path, path + ".bak")
    except OSError:
        pass                          # 备份失败不该挡住保存本身
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text_out)
    os.replace(tmp, path)             # 原子替换：中途断电也不会留下半个配置文件
    return changed


def write_models_to_file(providers: list[dict[str, Any]], summary: dict[str, Any],
                         fallback: dict[str, Any]) -> None:
    """把控制台改好的供应商 / 摘要模型配置写回 config.yaml。

    和提示词那边（只换块标量的正文）不同，这里动的是**嵌套结构**：
    providers 是一个对象列表，摘要模型是 summary 下的十来个平级键。
    对这种结构做逐行手术太脆，所以策略是：

    - ``providers:`` **整段用 yaml 重新生成**（段前的注释保留，段内的注释会丢——
      那一段本来就是控制台在管了，所以在段首补一行说明）；
    - ``summary:`` 下面的标量键**逐个原地替换**（`key: value` 换值不换行位置），
      文件里那一大片阈值注释和提示词块全都不受影响；
    - ``summary.extra_body`` / ``summary.fallback`` 是小块结构，整块替换。

    写前备份 config.yaml.bak，写后先 yaml 解析校验再原子替换。
    """
    path = os.path.abspath(CONFIG_PATH)
    with open(path, encoding="utf-8") as f:
        lines = f.read().split("\n")

    # 列表项缩进 2 格：既是原文件的写法，也让整段（含说明注释）都落在 providers 的
    # 缩进范围内——不然下次保存时 _block_range 会在顶格的注释行上就停住，旧条目留在原地
    lines = _replace_top_block(lines, "providers", _dump_block(
        [_provider_node(p) for p in providers], indent=2),
        header=["  # 这一段由 /ui 的控制台管理：手改可以，但控制台保存时会整段重写（段内注释会丢）"])

    for key in ("base_url", "model", "summary_max_tokens", "max_tokens_field",
                "timeout_seconds", "main_max_attempts", "min_output_tokens"):
        if key in summary:
            lines = _replace_scalar(lines, "summary", key, summary[key])
    _set_secret_line(lines, "summary", "api_key", summary.get("api_key", ""))

    lines = _replace_sub_block(lines, "summary", "extra_body",
                               summary.get("extra_body") or {})
    fb_node = {k: v for k, v in fallback.items() if k != "api_key"}
    fb_node["api_key"] = fallback.get("api_key", "")
    lines = _replace_sub_block(lines, "summary", "fallback", fb_node)

    text_out = "\n".join(lines)
    yaml.safe_load(text_out)          # 写坏了宁可抛异常，也不能把配置文件毁掉
    try:
        shutil.copyfile(path, path + ".bak")
    except OSError:
        pass
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text_out)
    os.replace(tmp, path)


def _provider_node(p: dict[str, Any]) -> dict[str, Any]:
    """写进文件的 provider 节点。字段顺序固定，读起来稳定。"""
    node: dict[str, Any] = {"name": p["name"], "base_url": p["base_url"],
                            "api_key": p.get("api_key", "")}
    for k in ("timeout_seconds", "connect_timeout_seconds", "multimodal"):
        if k in p:
            node[k] = p[k]
    for k in ("extra_body", "forward_headers", "forward_query"):
        if p.get(k):
            node[k] = p[k]
    return node


def _dump_block(node: Any, indent: int) -> list[str]:
    text = yaml.safe_dump(node, allow_unicode=True, sort_keys=False, default_flow_style=False)
    pad = " " * indent
    return [(pad + ln).rstrip() for ln in text.rstrip("\n").split("\n")]


def _block_range(lines: list[str], start: int, indent: int) -> int:
    """从 start+1 起，属于这个块的行：空行、缩进更深的行，或**同缩进的序列项**。

    最后那条别漏：`providers:` 下面的 `- name: ...` 按 YAML 规矩可以顶格写
    （yaml.safe_dump 就是这么输出的），漏了它会在第一个列表项上就判定块结束，
    旧条目留在原地，新写的插在前面，直接写出一份坏配置。
    """
    i = start + 1
    while i < len(lines):
        ln = lines[i]
        cur = len(ln) - len(ln.lstrip(" "))
        if ln.strip() == "" or cur > indent or (cur == indent and ln.lstrip().startswith("- ")):
            i += 1
            continue
        break
    while i - 1 > start and lines[i - 1].strip() == "":
        i -= 1
    return i


def _own_comment(line: str) -> bool:
    """是不是"属于本层键"的注释行。缩进比本层深的注释是上一个子块里的，别把它挤出来。"""
    stripped = line.lstrip()
    return stripped.startswith("#") and (len(line) - len(stripped)) <= 2


def _replace_top_block(lines: list[str], key: str, body: list[str],
                       header: list[str] | None = None) -> list[str]:
    pat = re.compile(rf"^{re.escape(key)}\s*:\s*$")
    start = next((i for i, ln in enumerate(lines) if pat.match(ln)), None)
    if start is None:
        raise ConfigError(f"config.yaml 里找不到顶层的 {key}:")
    end = _block_range(lines, start, 0)
    return lines[:start] + [f"{key}:"] + (header or []) + body + lines[end:]


def _replace_scalar(lines: list[str], section: str, key: str, value: Any) -> list[str]:
    """替换 `  key: value` 的值，**保留同一行尾部的注释**（那些注释就是文档）。"""
    start, end = _section_range(lines, section)
    pat = re.compile(rf"^  {re.escape(key)}\s*:")
    for i in range(start + 1, end):
        if pat.match(lines[i]):
            comment = _trailing_comment(lines[i])
            new_line = f"  {key}: {_scalar(value)}"
            if comment:
                # 尽量把注释对回原来的列，对不齐就退回两个空格
                col = max(len(new_line) + 2, lines[i].index(comment))
                new_line = new_line.ljust(col) + comment
            lines[i] = new_line.rstrip()
            return lines
    # 段里没有这个键就补一行进去
    lines.insert(_insert_point(lines, start, end), f"  {key}: {_scalar(value)}")
    return lines


def _insert_point(lines: list[str], start: int, end: int) -> int:
    """新键插在哪：提示词那一大块之前。插在段末的话会跑到几十行提示词后面，读起来找不着。"""
    for i in range(start + 1, end):
        if re.match(r"^  prompts\s*:", lines[i]):
            j = i
            while j - 1 > start and (lines[j - 1].strip() == "" or _own_comment(lines[j - 1])):
                j -= 1              # 连同它上面的注释块一起让位
            return j
    return end


def _trailing_comment(line: str) -> str:
    """取出行尾注释。值里也可能有 #，所以逐个候选位置试着解析，第一个解析得通的才算。"""
    for m in re.finditer(r"\s#", line):
        head = line[:m.start()]
        try:
            yaml.safe_load(head.strip() + "\n")
        except yaml.YAMLError:
            continue
        return line[m.start() + 1:]
    return ""


def _set_secret_line(lines: list[str], section: str, key: str, value: str) -> None:
    _replace_scalar(lines, section, key, value)


def _replace_sub_block(lines: list[str], section: str, key: str, node: Any) -> list[str]:
    """替换 `  key:` 下面那一小块结构（extra_body / fallback）。空 dict = 整块删掉。"""
    start, end = _section_range(lines, section)
    pat = re.compile(rf"^  {re.escape(key)}\s*:")
    idx = next((i for i in range(start + 1, end) if pat.match(lines[i])), None)
    if not node:
        if idx is None:
            return lines
        stop = _block_range(lines, idx, 2)
        return lines[:idx] + lines[stop:]
    body = _dump_block(node, indent=4)
    if idx is None:
        at = _insert_point(lines, start, end)
        tail = [""] if (at < len(lines) and lines[at].strip() != "") else []
        return lines[:at] + [f"  {key}:"] + body + tail + lines[at:]
    stop = _block_range(lines, idx, 2)
    return lines[:idx] + [f"  {key}:"] + body + lines[stop:]


def _section_range(lines: list[str], section: str) -> tuple[int, int]:
    pat = re.compile(rf"^{re.escape(section)}\s*:\s*$")
    start = next((i for i, ln in enumerate(lines) if pat.match(ln)), None)
    if start is None:
        raise ConfigError(f"config.yaml 里找不到顶层的 {section}:")
    return start, _block_range(lines, start, 0)


def _scalar(value: Any) -> str:
    """标量的 YAML 写法。safe_dump 会给纯标量加一行文档结束符 `...`，得去掉。"""
    text = yaml.safe_dump(value, allow_unicode=True, default_flow_style=True)
    parts = [ln for ln in text.split("\n") if ln.strip() and ln.strip() != "..."]
    return " ".join(parts).strip()


def _find_prompt_line(lines: list[str], name: str) -> int | None:
    """找 `    <name>: |` 这一行（只认 prompts 块里的四空格缩进）。"""
    pat = re.compile(rf"^    {re.escape(name)}\s*:\s*[|>][-+0-9]*\s*$")
    inside = False
    for i, ln in enumerate(lines):
        if re.match(r"^  prompts\s*:\s*$", ln):
            inside = True
            continue
        if inside and ln and not ln.startswith(" ") :
            inside = False
        if inside and pat.match(ln):
            return i
    return None


def _prompt_body_end(lines: list[str], start: int) -> int:
    """块标量的正文范围：start 之后所有空行或缩进 > 4 的行。"""
    i = start + 1
    while i < len(lines):
        ln = lines[i]
        if ln.strip() == "" or ln.startswith("      "):
            i += 1
            continue
        break
    while i - 1 > start and lines[i - 1].strip() == "":
        i -= 1                        # 尾随空行留给下一条，别吞掉分隔
    return i


def _find_prompts_block_end(lines: list[str]) -> int:
    """返回可以往 prompts 块尾部插新条目的行号；块不存在就先建出来。"""
    start = next((i for i, ln in enumerate(lines) if re.match(r"^  prompts\s*:\s*$", ln)), None)
    if start is None:
        start = _append_prompts_block(lines)
    i = start + 1
    while i < len(lines) and (lines[i].strip() == "" or lines[i].startswith("    ")):
        i += 1
    while i - 1 > start and lines[i - 1].strip() == "":
        i -= 1
    return i


def _append_prompts_block(lines: list[str]) -> int:
    """在 summary: 这一节末尾补一个空的 `  prompts:`，返回它的行号。

    没写 prompts 的配置是合法的（走内置默认值），页面第一次保存时才需要把这一节建出来。
    """
    head = next((i for i, ln in enumerate(lines) if re.match(r"^summary\s*:\s*$", ln)), None)
    if head is None:
        raise ConfigError("config.yaml 里找不到顶层的 summary:，无法写回提示词")
    i = head + 1
    while i < len(lines) and (lines[i].strip() == "" or lines[i].startswith(" ")):
        i += 1
    while i - 1 > head and lines[i - 1].strip() == "":
        i -= 1
    lines[i:i] = ["  prompts:"]
    return i


def set_prompt_overrides(overrides: dict[str, str]) -> None:
    """内存覆盖项。正常路径是写回 config.yaml，这里只在写文件失败时兜底。"""
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
            "max_attempts": max(1, int(fb.get("max_attempts", 3))),
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
