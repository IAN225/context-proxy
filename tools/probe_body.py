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

判定分三步，任何一步不成立都**不下结论**——错误的结论比没有结论更糟：

1. **基线**：先发一条不带任何探针字段的最小请求。
   它必须 2xx，否则 key / 模型名 / base_url / 余额里有问题，
   后面所有结果都是噪声，直接停在这里。
2. **对照**：再发一个故意瞎编的字段。
   - 400/422 且报错文本像"未知字段" → 这家**会校验**未知字段，
     可用项已确认被上游识别，可按需写入 extra_body；
   - 2xx → 这家对未知字段**照单全收**，可用项仅确认不会报错，
     是否生效要结合 reasoning / content / token 等信号判断，
     不要仅凭本次结果修改 extra_body；
   - 401/403/429/5xx/网络错误/看不出原因的 400 → **无法判断**，不给结论。
3. **逐字段**：每个字段同样只在 2xx / 400-422 时下结论；
   429、5xx、网络错误一律归入"无法判断"（会先自动重试一次），
   不会把一次限流误报成"这个字段不认"。

退出码：0 = 给出了结论（严格或宽松），1 = 跑完了但结论不确定，2 = 没能开始跑。

脚本只发很短的请求（几十 token），一个字段一次，默认 12 个字段外加基线和对照，
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
from cproxy import probe as P  # noqa: E402

# 判定规则（未知字段措辞、瞬时错误状态码、三档分类）统一放在 cproxy/probe.py，
# 控制台的保存校验用的是同一套，省得两边结论不一致
UNKNOWN_FIELD_PAT = P.UNKNOWN_FIELD_PAT
ACCEPT, REJECT, UNKNOWN = P.ACCEPT, P.REJECT, P.UNKNOWN

BASELINE = "__baseline__"
CONTROL = "__control_unknown_field__"

# (探针名, body 片段)。基线和对照必须排在最前面。
PROBES: list[tuple[str, dict]] = [
    (BASELINE, {}),
    (CONTROL, {"__cproxy_probe_nonexistent__": "x"}),
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


async def _probe(client: httpx.AsyncClient, url: str, headers: dict, model: str,
                 name: str, patch: dict, *, retry_delay: float = 2.0) -> dict:
    body = P.build_body(model, patch, max_tokens=P.PROBE_MAX_TOKENS)
    res = await P.probe(client, url, headers, body, name, retry_delay=retry_delay)
    d = res.to_dict()
    # 200 但不是 JSON / 没有 choices 这类，probe 已经判成不 ok 了，note 里有原因
    return d


async def main() -> int:
    ap = argparse.ArgumentParser(description="探测上游接受哪些 body 字段")
    ap.add_argument("target", help="provider 名，或 summary（主摘要模型）")
    ap.add_argument("--model", default="", help="覆盖用于探测的模型名")
    ap.add_argument("--via-proxy", action="store_true",
                    help="走本地代理而不是直连上游（顺带验证 extra_body/透传是否生效）")
    ap.add_argument("--only", default="", help="只跑这些探针，逗号分隔，支持前缀匹配")
    ap.add_argument("--extra", default="", help="额外探针，JSON 对象，如 '{\"top_k\": 20}'")
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--retry-delay", type=float, default=2.0,
                    help="限流/5xx 后重试前等待的秒数")
    args = ap.parse_args()

    config.reload(lambda *a, **k: None)
    base_url, api_key, model = _endpoint(args.target, args.via_proxy)
    model = args.model or model
    if not model:
        return _die("没有可用的模型名，用 --model 指定一个（走代理时代理不会替你填 model）")

    fixed = {BASELINE, CONTROL}
    probes = list(PROBES)
    if args.only:
        want = [w.strip() for w in args.only.split(",") if w.strip()]
        probes = [p for p in probes
                  if p[0] in fixed or any(p[0].startswith(w) for w in want)]
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
    print(f"探针  : {len(probes)} 个（含基线与对照，每个一次真实调用，会计费）\n")

    async with httpx.AsyncClient(timeout=args.timeout) as client:
        async def run(name, patch):
            res = await _probe(client, url, headers, model, name, patch,
                               retry_delay=args.retry_delay)
            _print_row(res)
            return res

        # 1) 基线：连不带探针字段的请求都发不出去，后面全是噪声
        base = await run(*probes[0])
        if not base["ok"]:
            return _abort_on_baseline(base)

        # 2) 对照 + 逐字段。串行发：并发容易撞上游限流
        results = [base]
        for name, patch in probes[1:]:
            results.append(await run(name, patch))

        # 3) 有拿不准的结果时，回头再确认一次端点还活着——
        #    如果基线这会儿也挂了，说明是端点中途出了问题，不是这些字段有问题
        recheck = None
        if any(r["kind"] == UNKNOWN for r in results):
            print("\n有结果无法判断，复查一次基线……")
            recheck = await run(BASELINE + "(复查)", {})

    return _verdict(results, recheck)


def _print_row(res: dict) -> None:
    mark = {ACCEPT: "OK  ", REJECT: "拒绝", UNKNOWN: "存疑"}.get(
        res.get("kind"), "OK  " if res["ok"] else "存疑")
    bits = [f"{mark} {res['status']:>3}  {res['name']}"]
    if res.get("retried"):
        bits.append("(重试过)")
    if res.get("reasoning_chars"):
        bits.append(f"思考 {res['reasoning_chars']} 字")
    if res.get("reasoning_tokens") is not None:
        bits.append(f"reasoning_tokens={res['reasoning_tokens']}")
    if res.get("note"):
        bits.append(res["note"])
    print("  ".join(bits))


def _abort_on_baseline(base: dict) -> int:
    print("\n" + "=" * 60)
    print(f"基线请求就没成功（{base['status']}），**探测中止**。")
    print("这条请求不带任何探针字段，它失败说明问题不在 body 字段上，")
    print("而在 api_key / 模型名 / base_url / 余额 / 网络其中之一。")
    if base.get("note"):
        print(f"\n上游原文：{base['note']}")
    print("\n先把这条最小请求跑通，再回来探测字段——否则每个字段都会「失败」，")
    print("那份清单没有任何意义。")
    return 2


def _control_verdict(ctl: dict | None) -> tuple[bool | None, str]:
    """返回 (是否严格校验未知字段, 说明)。None = 不下结论。"""
    if ctl is None:
        return None, "没跑对照组，无法判断这家认不认未知字段。"
    if ctl["kind"] == ACCEPT:
        return False, ("对照组（瞎编的字段）返回 2xx → 这家**不校验未知字段**。\n\n"
                       "这些字段仅确认不会报错，是否生效仍需结合 reasoning / content / token\n"
                       "等信号判断，不要仅凭本次结果修改 extra_body。怎么看：\n"
                       "  - 思考类字段：开/关两次的「思考 N 字」或 reasoning_tokens 是否有差别\n"
                       "  - 其他字段：多跑几次看输出是否随之变化")
    if ctl["kind"] == REJECT and UNKNOWN_FIELD_PAT.search(ctl.get("note", "")):
        return True, (f"对照组（瞎编的字段）被拒（{ctl['status']}，报错文本指向未知字段）→\n"
                      "这家**会校验未知字段**。\n\n"
                      "可用项已确认被上游识别，可按需写入 extra_body。")
    if ctl["kind"] == REJECT:
        return None, (f"对照组被拒（{ctl['status']}），但报错文本看不出是不是在校验未知字段：\n"
                      f"  {ctl.get('note', '')[:200]}\n"
                      "可能是这家的措辞不一样，也可能是这条请求本身还有别的毛病。\n"
                      "**不下结论**：下面「可用」的字段只是没报错，不能据此断定上游认识它。")
    return None, (f"对照组既没成功也不是 400/422（{ctl['status']}）→ **无法判断**。\n"
                  f"  {ctl.get('note', '')[:200]}\n"
                  "这类状态说明的是这次没问上去（限流 / 鉴权 / 上游故障），\n"
                  "不能当成「这家会校验未知字段」。过一会儿重跑。")


def _verdict(results: list[dict], recheck: dict | None) -> int:
    by_name = {r["name"]: r for r in results}
    ctl = by_name.get(CONTROL)
    rest = [r for r in results if r["name"] not in (BASELINE, CONTROL)]
    ok = [r["name"] for r in rest if r["kind"] == ACCEPT]
    bad = [r["name"] for r in rest if r["kind"] == REJECT]
    unsure = [r["name"] for r in rest if r["kind"] == UNKNOWN]

    strict, explain = _control_verdict(ctl)

    print("\n" + "=" * 60)
    print(explain)

    print(f"\n可用 ({len(ok)})：{', '.join(ok) or '无'}")
    print(f"被拒 ({len(bad)})：{', '.join(bad) or '无'}")
    if unsure:
        print(f"无法判断 ({len(unsure)})：{', '.join(unsure)}")
        print("  ↑ 这些不是「上游不认」，是这次没问出结果（限流 / 鉴权 / 上游故障），")
        print("    别据此改 config.yaml，过一会儿单独重跑：--only " + unsure[0].split("=")[0])
    if recheck is not None:
        print("  基线复查：" + ("仍然正常，说明上面存疑的是那几个字段各自的问题"
                                if recheck["ok"] else
                                f"也失败了（{recheck['status']}）——是端点中途出了问题，"
                                "整轮结果都不可信，等恢复后重跑"))

    thinking = [r for r in rest if r.get("reasoning_chars")]
    if thinking:
        print("\n真的产生了思考内容的组合：")
        for r in thinking:
            print(f"  - {r['name']}：{r['reasoning_chars']} 字")
    elif strict is False:
        print("\n没有任何一个组合返回思考内容——要么这个模型不带思考，")
        print("要么这家网关不回传思考过程，此时「关闭思考」类字段也无从验证。")

    if strict is None or (recheck is not None and not recheck["ok"]):
        # 端点中途挂掉时，前面那些"可用"是在什么状态下测出来的已经说不清了
        print("\n结论不确定，先别照着改 config.yaml。")
        return 1
    if strict:
        print("\n把可用的那一行抄进 config.yaml 对应的 extra_body，其余删掉。")
    else:
        print("\n先按上面的信号确认字段真的生效了，再决定写不写进 config.yaml。")
    return 0


def _die(msg: str) -> int:
    print(msg, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
