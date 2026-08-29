"""纯函数单测：轮次、指纹、对齐、净化。不需要起服务。"""

import os
import pathlib
import sys
import tempfile

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="cproxy-unit-")
_CFG = pathlib.Path(_TMP) / "config.yaml"
_CFG.write_text(yaml.safe_dump({
    "providers": [{"name": "p", "base_url": "http://127.0.0.1:1/v1", "api_key": "k"}],
    "summary": {"base_url": "http://127.0.0.1:1/v1", "api_key": "k", "model": "m",
                "persist_db": ""},
}, allow_unicode=True), encoding="utf-8")
os.environ["PROXY_CONFIG"] = str(_CFG)

from cproxy import config, locate  # noqa: E402
from cproxy import messages as M   # noqa: E402

config.reload()


def test_rounds_definition():
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2", "tool_calls": [{"id": "t1", "function": {"name": "f", "arguments": "{}"}}]},
        {"role": "tool", "content": "r", "tool_call_id": "t1"},
        {"role": "assistant", "content": "a2b"},
        {"role": "user", "content": "u3"},
    ]
    rounds = M.split_rounds(msgs)
    # 首轮吞掉开头的 system；工具调用整段留在第 2 轮内，不会被切开
    assert rounds == [(0, 3), (3, 7), (7, 8)]
    assert M.rounds_before(rounds, 3) == 1
    assert M.rounds_before(rounds, 7) == 2
    assert M.round_of_index(rounds, 5) == 2


def test_fingerprint_is_stable_when_image_ref_changes():
    a = {"role": "user", "content": [{"type": "text", "text": "看这张图"},
                                     {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}
    b = {"role": "user", "content": [{"type": "text", "text": "看这张图"},
                                     {"type": "image_url", "image_url": {"url": "file-9c1e2f"}}]}
    # 客户端把已删除图片的 base64 换成私有 id，指纹必须不变，否则整段历史假分叉
    assert M.msg_fingerprint(a) == M.msg_fingerprint(b)
    assert M.msg_tokens(a) == M.msg_tokens(b) > 1000


def test_token_accounting_covers_non_text():
    plain = {"role": "assistant", "content": "hello"}
    with_tool = {"role": "assistant", "content": "hello",
                 "tool_calls": [{"id": "1", "function": {"name": "search",
                                                         "arguments": '{"q":"very long query text"}'}}]}
    assert M.msg_tokens(with_tool) > M.msg_tokens(plain)
    # count_tokens 就是 msg_tokens 的求和，两者口径一致
    assert M.count_tokens([plain, with_tool]) == M.msg_tokens(plain) + M.msg_tokens(with_tool)


def _sig(*items):
    return list(items)


def test_align_identical():
    cur = _sig(*"abcdefgh")
    al = locate.align(cur, cur)
    assert not al.forked and al.matched_ref == 8 and al.shifts == 0


def test_align_appended_tail():
    ref = _sig(*"abcd")
    cur = _sig(*"abcdefg")
    al = locate.align(cur, ref)
    assert not al.forked and al.matched_ref == 4
    assert al.cur_index_for(4) == 4


def test_align_client_inserted_messages():
    ref = _sig(*"abcdefgh")
    cur = _sig("a", "b", "X", "Y", "c", "d", "e", "f", "g", "h")
    al = locate.align(cur, ref)
    assert not al.forked, "客户端插了两条，只是错位，不能判定为分叉"
    assert al.shifts == 1 and al.net_offset == 2
    assert al.cur_index_for(4) == 6      # ref 下标 4 对应 cur 下标 6


def test_align_client_deleted_messages():
    ref = _sig(*"abcdefgh")
    cur = _sig("a", "b", "e", "f", "g", "h")
    al = locate.align(cur, ref)
    assert not al.forked and al.shifts == 1 and al.net_offset == -2


def test_align_real_fork():
    ref = _sig(*"abcdefgh")
    cur = _sig("a", "b", "c", "Z", "W", "Q", "R", "S")
    al = locate.align(cur, ref)
    assert al.forked and al.fork_cur == 3 and al.fork_ref == 3


def test_sanitize_strips_images_for_text_only_provider():
    msgs = [{"role": "user", "content": [{"type": "text", "text": "看图"},
                                         {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}]
    out, stats = M.sanitize_for_upstream(msgs, multimodal=False)
    assert stats["images_stripped"] == 1
    assert out[0]["content"] == "看图\n[图片]"


def test_sanitize_replaces_invalid_image_ref_for_multimodal():
    msgs = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "file-abc123"}}]}]
    out, stats = M.sanitize_for_upstream(msgs, multimodal=True)
    assert stats["images_invalid"] == 1
    assert "已失效" in out[0]["content"]


def test_sanitize_drops_orphan_tool_messages():
    msgs = [{"role": "tool", "content": "r", "tool_call_id": "gone"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "t1", "function": {"name": "f", "arguments": "{}"}}]},
            {"role": "tool", "content": "r", "tool_call_id": "t1"}]
    out, stats = M.sanitize_for_upstream(msgs, multimodal=True)
    assert stats["orphan_tool_dropped"] == 1 and len(out) == 2


def test_anchor_selection():
    infos = M.analyze([{"role": "user", "content": "短"}] +
                      [{"role": "user", "content": f"这是第 {i} 条足够长的消息内容用于锚点匹配测试"}
                       for i in range(20)])
    anchors = M.pick_anchors(infos)
    assert 2 <= len(anchors) <= 5
    assert all(isinstance(a, str) for a in anchors)


def test_conv_key_uses_first_five_user_messages_verbatim():
    base = [{"role": "user", "content": "第一条" * 300}, {"role": "assistant", "content": "回"},
            {"role": "user", "content": "第二条" * 300}]
    same = base + [{"role": "assistant", "content": "再回"}]
    assert M.conv_key(base) == M.conv_key(same)
    # 旧算法按 500 字节截断，改动 500 字节之后的内容认不出差别；新算法必须能区分
    changed = [{"role": "user", "content": "第一条" * 299 + "不同"},
               {"role": "assistant", "content": "回"},
               {"role": "user", "content": "第二条" * 300}]
    assert M.conv_key(base) != M.conv_key(changed)
    assert M.legacy_conv_id(base) == M.legacy_conv_id(changed)


def _reload_with(**server) -> list[str]:
    """用一份临时配置跑一遍 config.reload，返回它发出的告警。"""
    path = pathlib.Path(_TMP) / "cfg_warn.yaml"
    path.write_text(yaml.safe_dump({
        "providers": [{"name": "p", "base_url": "http://127.0.0.1:1/v1", "api_key": "k"}],
        "summary": {"base_url": "http://127.0.0.1:1/v1", "api_key": "k", "model": "m",
                    "persist_db": ""},
        "server": server,
    }, allow_unicode=True), encoding="utf-8")
    old, warns = config.CONFIG_PATH, []
    config.CONFIG_PATH = str(path)
    try:
        config.reload(lambda f, *a: warns.append(f % a if a else f))
    finally:
        config.CONFIG_PATH = old
    return warns


def test_empty_auth_token_warns_loudly():
    """仓库里的 config.yaml 默认留空（不提交真实密钥），那就必须在启动时喊一声。"""
    assert any("auth_token" in w and "不鉴权" in w for w in _reload_with()), _reload_with()
    assert not any("auth_token" in w for w in _reload_with(auth_token="sk-proxy-x"))


def test_admin_token_isolation_rules():
    from cproxy.app import _accepted_tokens
    _reload_with(auth_token="sk-chat", ui_token="uiTOKEN0123456789")
    assert _accepted_tokens(admin=False) == ["sk-chat"]
    assert _accepted_tokens(admin=True) == ["uiTOKEN0123456789"], "配了 ui_token 就别再认 auth_token"

    _reload_with(auth_token="sk-chat")
    assert _accepted_tokens(admin=False) == ["sk-chat"]
    assert _accepted_tokens(admin=True) == ["sk-chat"], "没配 ui_token 时得留给 ctl.sh 用"

    _reload_with()
    assert _accepted_tokens(admin=False) == [] and _accepted_tokens(admin=True) == []


def test_fallback_endpoint_has_max_attempts():
    """漏了这个字段，切备用摘要模型时会 KeyError 而不是重试。"""
    path = pathlib.Path(_TMP) / "cfg_fb.yaml"
    path.write_text(yaml.safe_dump({
        "providers": [{"name": "p", "base_url": "http://127.0.0.1:1/v1", "api_key": "k"}],
        "summary": {"base_url": "http://127.0.0.1:1/v1", "api_key": "k", "model": "m",
                    "persist_db": "", "main_max_attempts": 2,
                    "extra_body": {"temperature": 0.5},
                    "fallback": {"enabled": True, "base_url": "http://127.0.0.1:1/v1",
                                 "api_key": "k2", "model": "m2"}},
    }, allow_unicode=True), encoding="utf-8")
    old = config.CONFIG_PATH
    config.CONFIG_PATH = str(path)
    try:
        config.reload()
        eps = config.summary_endpoints()
    finally:
        config.CONFIG_PATH = old
        config.reload()
    assert [e["tag"] for e in eps] == ["primary", "fallback"]
    assert eps[0]["max_attempts"] == 2
    assert eps[1]["max_attempts"] == 3, "fallback.max_attempts 没写时默认 3"
    assert eps[1]["extra_body"] == {"temperature": 0.5}, "备用没写 extra_body 时沿用主模型的"


def test_timeline_respects_retain_from_zero():
    """窗口回退到 0（还没压过，或为了保住近期原文下限退到头）时，一轮都不该标成"已折叠"。"""
    from cproxy import compress
    body = [{"role": "user", "content": "问"}, {"role": "assistant", "content": "答"}] * 3
    infos = M.analyze(body)
    rounds = M.split_rounds(body)
    # compressed_upto 还留着历史值，但这次实际从 0 开始逐字发——用 or 会把 0 当缺失
    tl = compress.build_timeline(body, infos, rounds,
                                 {"retain_from": 0, "compressed_upto": 4}, "p")
    assert tl["retain_from"] == 0
    assert all(r["c"] == 0 for r in tl["rounds"]), tl["rounds"]

    tl2 = compress.build_timeline(body, infos, rounds,
                                  {"retain_from": 4, "compressed_upto": 4}, "p")
    assert [r["c"] for r in tl2["rounds"]] == [1, 1, 0]
