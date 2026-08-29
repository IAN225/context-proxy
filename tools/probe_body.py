#!/usr/bin/env python3
"""探测上游到底认哪些 body 字段——填 extra_body 之前先跑一遍这个。

背景：``extra_body`` 里的字段名各家不一样（``reasoning_effort`` / ``thinking`` /
``enable_thinking``），写错的后果分两种：有的网关直接 400 让整条对话报错，
有的网关默默丢掉，你以为思考开了其实没开。靠猜是猜不出来的，所以直接问上游。

用法（在项目根目录）::

    python3 tools/probe_body.py provider-a          # 探测某个上游供应商
    python3 tools/probe_body.py summary             # 探测主摘要模型
    python3 tools/probe_body.py provider-a --via-proxy   # 走本地代理，顺带验证透传
    python3 tools/probe_body.py provider-a --only reasoning_effort,thinking
    python3 tools/probe_body.py provider-a --extra '{"foo": 1}'   # 试自己的字段

判定规则（重要）：先发一个**故意瞎编**的字段做对照。
- 对照被 400 拒掉 → 这家会校验未知字段，那么某字段返回 200 就是真的认识它。
- 对照返回 200   → 这家对未知字段照单全收，200 **不代表生效**，只代表不报错；
  这种情况下只能看"响应里有没有思考内容""token 用量有没有变"这类间接信号。

脚本只发很短的请求（几十 token），一个字段一次，默认 12 个字段，
按你的模型价格算通常是几分钱的事，但**确实会走真实计费**，心里有数。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from cproxy import config  # noqa: E402

# 一次探测发的最小请求：要求模型只回一个字，省钱也省时间
BASE_BODY: dict = {
    "messages": [{"role": "user", "content": "回答一个字：好"}],
    "max_tokens": 16,
    "stream": False,
}

# (探针名, body 片段)。名字排在前面的先跑。
PROBES: list[tuple[str, dict]] = [
    ("__control_unknown_field__", {"__cproxy_probe_nonexistent__": "x"}),
    ("temperature", {"temperature": 0.5}),
    ("top_p", {"top_p": 0.9}),
    ("reasoning_effort=minimal", {"reasoning_effort": "minimal"}),
    ("reasoning_effort=high", {"reasoning_effort": "high"}),
    ("reasoning_effort=xhigh", {"reasoning_effort": "xhigh"}),
    ("reasoning_effort=none", {"reasoning_effort": "none"}),
    ("thinking=enabled", {"thinking": {"type": "enabled", "budget_tokens": 1024}}),
    ("thinking=disabled", {"thinking": {"type": "disabled"}}),
    ("enable_thinking=true", {"enable_thinking": True}),
    ("enable_thinking=false", {"enable_thinking": False}),
    ("seed", {"seed": 42}),
    ("presence_penalty", {"presence_penalty": 0.1}),
]

CONTROL = "__control_unknown_field__"


def _endpoint(target: str, via_proxy: bool) -> tuple[str, str, str]:
    """返回 (base_url, api_key, model)。"""
    if via_proxy:
        port = int(config.cfg().get("server", {}).get("port", 8787))
        key = config.auth_token() or ""
        if target == "summary":
            sys.exit("--via-proxy 只能配供应商名（摘要模型不经过 /v1/chat/completions）")
        p = config.provider(target)
        if not p:
            sys.exit(f"config.yaml 里没有名为 {target} 的 provider")
        return f"http://127.0.0.1:{port}/{target}/v1", key, ""
    if target == "summary":
        eps = config.summary_endpoints()
        if not eps:
            sys.exit("摘要模型未配置（summary.base_url / api_key / model）")
        ep = eps[0]
        return ep["base_url"], ep["api_key"], ep["model"]
    p = config.provider(target)
    if not p:
        names = ", ".join(config.providers()) or "(空)"
        sys.exit(f"config.yaml 里没有名为 {target} 的 provider。已配置：{names}")
    # 供应商侧没有"默认模型"这一说（模型是客户端每次请求带的），所以必须 --model
    return p["base_url"], p["api_key"], ""


def _reasoning_of(data: dict) -> str:
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


async def _probe(client: httpx.AsyncClient, url: str, headers: dict, model: str,
                 name: str, patch: dict) -> dict:
    body = dict(BASE_BODY)
    if model:
        body["model"] = model
    body.update(patch)
    try:
        r = await client.post(url, headers=headers, json=body)
    except Exception as e:                            # noqa: BLE001 网络层错误也是结果
        return {"name": name, "status": 0, "ok": False, "note": f"请求失败: {type(e).__name__}: {e}"}

    out: dict = {"name": name, "status": r.status_code, "ok": r.is_success}
    if not r.is_success:
        out["note"] = r.text.strip().replace("\n", " ")[:220]
        return out
    try:
        data = r.json()
    except ValueError:
        out["ok"] = False
        out["note"] = "200 但响应不是 JSON（多半是网关的错误页）：" + r.text[:160]
        return out
    usage = data.get("usage") or {}
    reasoning = _reasoning_of(data)
    out["reasoning_chars"] = len(reasoning)
    out["completion_tokens"] = usage.get("completion_tokens")
    out["reasoning_tokens"] = ((usage.get("completion_tokens_details") or {})
                               .get("reasoning_tokens"))
    return out


async def main() -> int:
    ap = argparse.ArgumentParser(description="探测上游接受哪些 body 字段")
    ap.add_argument("target", help="provider 名，或 summary（主摘要模型）")
    ap.add_argument("--model", default="", help="覆盖用于探测的模型名")
    ap.add_argument("--via-proxy", action="store_true",
                    help="走本地代理而不是直连上游（顺带验证 extra_body/透传是否生效）")
    ap.add_argument("--only", default="", help="只跑这些探针，逗号分隔，支持前缀匹配")
    ap.add_argument("--extra", default="", help="额外探针，JSON 对象，如 '{\"top_k\": 20}'")
    ap.add_argument("--timeout", type=float, default=120.0)
    args = ap.parse_args()

    config.reload(lambda *a, **k: None)
    base_url, api_key, model = _endpoint(args.target, args.via_proxy)
    model = args.model or model
    if not model:
        return _die("没有可用的模型名，用 --model 指定一个（走代理时代理不会替你填 model）")

    probes = list(PROBES)
    if args.only:
        want = [w.strip() for w in args.only.split(",") if w.strip()]
        probes = [p for p in probes
                  if p[0] == CONTROL or any(p[0].startswith(w) for w in want)]
    if args.extra:
        try:
            patch = json.loads(args.extra)
            assert isinstance(patch, dict)
        except (ValueError, AssertionError):
            return _die("--extra 必须是一个 JSON 对象")
        for k, v in patch.items():
            probes.append((f"{k}(自定义)", {k: v}))

    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    print(f"目标  : {url}")
    print(f"模型  : {model}")
    print(f"探针  : {len(probes)} 个（每个一次真实调用，会计费）\n")

    results = []
    async with httpx.AsyncClient(timeout=args.timeout) as client:
        for name, patch in probes:
            # 串行发：并发容易撞上游限流，429 会被误读成"这个字段不认"
            res = await _probe(client, url, headers, model, name, patch)
            results.append(res)
            _print_row(res)

    return _verdict(results)


def _print_row(res: dict) -> None:
    mark = "OK  " if res["ok"] else "拒绝"
    bits = [f"{mark} {res['status']:>3}  {res['name']}"]
    if res.get("reasoning_chars"):
        bits.append(f"思考 {res['reasoning_chars']} 字")
    if res.get("reasoning_tokens") is not None:
        bits.append(f"reasoning_tokens={res['reasoning_tokens']}")
    if res.get("note"):
        bits.append(res["note"])
    print("  ".join(bits))


def _verdict(results: list[dict]) -> int:
    ctl = next((r for r in results if r["name"] == CONTROL), None)
    rest = [r for r in results if r["name"] != CONTROL]
    ok = [r["name"] for r in rest if r["ok"]]
    bad = [r["name"] for r in rest if not r["ok"]]

    print("\n" + "=" * 60)
    if ctl is None:
        strict = None
    elif ctl["ok"]:
        strict = False
        print("对照组（瞎编的字段）返回 200 → 这家**不校验未知字段**。")
        print("所以下面的 OK 只说明「不报错」，不说明生效。判断是否真生效只能看：")
        print("  - 思考类字段：开/关两次的「思考 N 字」或 reasoning_tokens 是否有差别")
        print("  - 其他字段：多跑几次看输出是否随之变化")
    else:
        strict = True
        print(f"对照组（瞎编的字段）被拒（{ctl['status']}）→ 这家**会校验未知字段**，")
        print("因此下面 OK 的字段是上游真的认识的，可以放心写进 extra_body。")

    print(f"\n可用 ({len(ok)})：{', '.join(ok) or '无'}")
    print(f"被拒 ({len(bad)})：{', '.join(bad) or '无'}")

    thinking = [r for r in rest if r.get("reasoning_chars")]
    if thinking:
        print("\n真的产生了思考内容的组合：")
        for r in thinking:
            print(f"  - {r['name']}：{r['reasoning_chars']} 字")
    elif strict is False:
        print("\n没有任何一个组合返回思考内容——要么这个模型不带思考，")
        print("要么这家网关不回传思考过程，此时「关闭思考」类字段也无从验证。")

    print("\n把可用的那一行抄进 config.yaml 对应的 extra_body，其余删掉。")
    return 0


def _die(msg: str) -> int:
    print(msg, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
