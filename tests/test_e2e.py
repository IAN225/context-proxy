"""端到端：真起一个假上游 + 真起代理，走完整 HTTP 链路。"""

from __future__ import annotations

import json
import os
import pathlib
import sqlite3
import sys
import tempfile
import threading
import time

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

TMP = pathlib.Path(tempfile.mkdtemp(prefix="cproxy-e2e-"))
CFG_PATH = TMP / "config.yaml"
MOCK_PORT = 9911
PROXY_PORT = 9912
MOCK = f"http://127.0.0.1:{MOCK_PORT}"
PROXY = f"http://127.0.0.1:{PROXY_PORT}"
TOKEN = "sk-test"

BASE_SUMMARY = {
    "enabled": True,
    "trigger_tokens": 1200,
    "keep_recent_tokens": 300,
    "summary_total_cap_tokens": 100000,
    "summary_max_tokens": 500,
    "summary_batch_tokens": 900,
    "max_batches_per_request": 20,
    "exit_gate_ratio": 1.2,
    "checkpoint_keep": 3,
    "summary_role": "system",
    "cache_max_entries": 64,
    "base_url": f"{MOCK}/v1",
    "api_key": "sk-summary",
    "model": "mock-summary",
    "timeout_seconds": 20,
    "main_max_attempts": 1,
    "min_output_tokens": 5,
    "fallback": {"enabled": False},
}


UI_TOKEN = "uiTOKEN0123456789"

# 默认提示词模板里正文前的那句话（测试配置不写 prompts，走内置默认值）
CONTEXT_MARK = "以下是待压片段"


def write_config(db_name: str, ui_token: str = "", **summary_overrides) -> None:
    cfg = {
        "providers": [
            {"name": "mm", "base_url": f"{MOCK}/v1", "api_key": "sk-up",
             "timeout_seconds": 30, "multimodal": True},
            {"name": "text", "base_url": f"{MOCK}/v1", "api_key": "sk-up",
             "timeout_seconds": 30, "multimodal": False},
        ],
        "summary": {**BASE_SUMMARY, "persist_db": db_name, **summary_overrides},
        "stream": {"smooth_chars": 8, "smooth_delay": 0.001, "flush_backlog_chars": 200},
        "tokenizer": {"encoding": "cl100k_base", "per_message_overhead": 4, "image_tokens": 1100},
        "server": {"host": "127.0.0.1", "port": PROXY_PORT, "auth_token": TOKEN,
                   "ui_token": ui_token},
        "logging": {"level": "INFO", "file": "logs/proxy.log"},
    }
    CFG_PATH.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")


os.environ["PROXY_CONFIG"] = str(CFG_PATH)
write_config("db/initial.db")

import httpx  # noqa: E402
import uvicorn  # noqa: E402

import mock_upstream  # noqa: E402
from cproxy import app as app_module  # noqa: E402
from cproxy import config as config_mod  # noqa: E402

app_module.bootstrap()
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(scope="session", autouse=True)
def servers():
    mock_upstream.serve(MOCK_PORT)
    cfg = uvicorn.Config(app_module.app, host="127.0.0.1", port=PROXY_PORT, log_level="warning")
    server = uvicorn.Server(cfg)
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    for _ in range(100):
        try:
            httpx.get(f"{PROXY}/health", timeout=2)
            break
        except Exception:
            time.sleep(0.05)
    yield
    server.should_exit = True


_counter = {"n": 0}


_ADMIN_H = dict(HEADERS)


def admin_h() -> dict:
    """/admin/* 该用的密钥：配了 ui_token 就只能用 ui_token，没配才是 auth_token。"""
    return dict(_ADMIN_H)


def _set_admin_token(ui_token: str) -> None:
    _ADMIN_H.clear()
    _ADMIN_H["Authorization"] = f"Bearer {ui_token or TOKEN}"


def fresh(ui_token: str = "", **summary_overrides) -> None:
    """给每个用例一个干净的 DB 和配置。"""
    _counter["n"] += 1
    from cproxy import app as _a
    _a._FAIL.clear()                      # 清掉上个用例攒下的鉴权失败计数
    write_config(f"db/t{_counter['n']}.db", ui_token=ui_token, **summary_overrides)
    _set_admin_token(ui_token)
    app_module.do_reload()
    httpx.post(f"{MOCK}/__reset", timeout=5)


# ===== 构造对话 =====
FILLER = "这是一段用于把消息撑到一定长度的中文内容，重复若干次以便控制 token 数量。"


def convo(rounds: int, *, start: int = 0, filler: int = 2, head: bool = True) -> list[dict]:
    msgs: list[dict] = [{"role": "system", "content": "你是一个陪伴助手。"}] if head else []
    for i in range(start, start + rounds):
        msgs.append({"role": "user", "content": f"【第{i}轮提问】{FILLER * filler}"})
        msgs.append({"role": "assistant", "content": f"【第{i}轮回答】{FILLER * filler}"})
    return msgs


def post(provider: str, messages: list[dict], *, timeout: float = 60, **extra):
    return httpx.post(f"{PROXY}/{provider}/v1/chat/completions", headers=HEADERS,
                      json={"model": "mock-chat", "messages": messages, **extra}, timeout=timeout)


def calls():
    return httpx.get(f"{MOCK}/__calls", timeout=5).json()["calls"]


def summary_calls():
    return [c for c in calls() if str(c["model"]).startswith("mock-summary")]


def chat_calls():
    return [c for c in calls() if not str(c["model"]).startswith("mock-summary")]


def sessions():
    return httpx.get(f"{PROXY}/admin/sessions", headers=admin_h(), timeout=10).json()["sessions"]


def session_detail(conv_id: str):
    return httpx.get(f"{PROXY}/admin/session/{conv_id}", headers=admin_h(), timeout=10).json()


def ctl(**kw):
    httpx.post(f"{MOCK}/__ctl", json=kw, timeout=5)


# ===== 用例 =====
def test_short_conversation_passes_through():
    fresh()
    r = post("mm", convo(2))
    assert r.status_code == 200
    assert not summary_calls(), "没超阈值不该调摘要模型"
    assert len(sessions()) == 0, "没触发压缩就不该建档"


def test_first_compression_then_reuse():
    fresh()
    history = convo(30)
    r = post("mm", history)
    assert r.status_code == 200, r.text
    n_first = len(summary_calls())
    assert n_first >= 2, "应当分多批压缩"
    ss = sessions()
    assert len(ss) == 1 and ss[0]["status"] == "sealed"
    assert ss[0]["compressed_upto"] > 0 and ss[0]["round_upto"] > 0

    # 上游收到的消息数远小于原始历史，且第一条 system 之后是摘要
    last = chat_calls()[-1]
    assert last["n_messages"] < len(history)
    assert last["roles"][1] == "system"

    # 再追加两轮：等效总量仍低于阈值，应复用摘要且不再调摘要模型
    r = post("mm", history + convo(2, start=100))
    assert r.status_code == 200
    assert len(summary_calls()) == n_first, "复用摘要时不该再调摘要模型"


def test_incremental_progress_never_restarts_from_zero():
    fresh()
    history = convo(30)
    assert post("mm", history).status_code == 200
    upto1 = sessions()[0]["compressed_upto"]

    history = history + convo(30, start=200)
    assert post("mm", history).status_code == 200
    s = sessions()[0]
    assert s["compressed_upto"] > upto1, "增量压缩应当推进，而不是从 0 重来"
    assert s["round_upto"] > 0
    # 第二次压缩的输入不该覆盖已压过的早期内容
    assert all("第0轮提问" not in c["prompt"] for c in summary_calls()[1:])


def test_edit_recent_message_does_not_trigger_full_recompress():
    fresh()
    history = convo(30)
    assert post("mm", history).status_code == 200
    n = len(summary_calls())
    upto = sessions()[0]["compressed_upto"]

    edited = list(history)
    edited[-1] = {"role": "assistant", "content": "【被改过的最后一条回答】" + FILLER}
    assert post("mm", edited).status_code == 200
    assert sessions()[0]["compressed_upto"] >= upto
    assert len(summary_calls()) == n, "改动只发生在近期原文区，不该重新压缩"


def test_client_inserted_messages_are_not_treated_as_fork():
    fresh()
    history = convo(30)
    assert post("mm", history).status_code == 200
    upto = sessions()[0]["compressed_upto"]
    before = httpx.get(f"{PROXY}/health", timeout=5).json()["fallback_activations"]

    # 客户端在已压缩区中间插了两条（常见于重新生成/编辑）
    spliced = history[:5] + [{"role": "user", "content": "插入A" + FILLER},
                             {"role": "assistant", "content": "插入B" + FILLER}] + history[5:]
    assert post("mm", spliced).status_code == 200
    after = httpx.get(f"{PROXY}/health", timeout=5).json()["fallback_activations"]
    assert after == before, "插入几条只是错位，不该掉进兜底"
    assert sessions()[0]["compressed_upto"] >= upto - 2


def test_early_fork_rolls_back_to_a_checkpoint_not_zero():
    fresh(checkpoint_keep=10)
    history = convo(20)
    assert post("mm", history).status_code == 200
    first_upto = sessions()[0]["compressed_upto"]
    history = history + convo(20, start=300)
    assert post("mm", history).status_code == 200
    second = sessions()[0]
    assert second["compressed_upto"] > first_upto
    n_before = len(summary_calls())

    # 改一条落在「第一次压缩之后、第二次压缩之内」的消息 → 应回退到第一个 checkpoint
    idx = first_upto + 2
    forked = list(history)
    forked[idx] = {"role": forked[idx]["role"], "content": "【从这里分叉】" + FILLER * 2}
    r = post("mm", forked)
    assert r.status_code == 200, r.text
    s = sessions()[0]
    assert s["compressed_upto"] > 0, "不能从第 0 条全量重压"
    added = len(summary_calls()) - n_before
    assert added < n_before, f"回退重压的批次({added})应远少于首次全量({n_before})"
    assert httpx.get(f"{PROXY}/health", timeout=5).json()["fallback_activations"] == 0


# ---- 三个开放问题的答案，用测试钉死 ----

def test_summary_goes_under_system_right_before_the_transcript():
    """答案一：摘要以 system 注入，位置固定在开头 system 之后、近期原文之前。"""
    fresh()
    assert post("mm", convo(30)).status_code == 200
    call = chat_calls()[-1]
    assert call["roles"][0] == "system", "原有的开头 system 提示要保留在最前"
    assert call["roles"][1] == "system", "摘要必须以 system 角色注入"
    assert call["roles"][2] == "user", "摘要之后紧接着就是近期原文"
    assert "user" not in call["roles"][:2]

    # summary_role 可切到 user，给只允许一条 system 的上游用
    fresh(summary_role="user")
    assert post("mm", convo(30)).status_code == 200
    call = chat_calls()[-1]
    assert call["roles"][0] == "system" and call["roles"][1] == "user"


def test_prefix_stays_byte_stable_between_compressions():
    """答案二：摘要排在原文之前，两次压缩之间请求前缀逐字节不变，能命中上游 prefix cache。"""
    fresh()
    history = convo(30)
    assert post("mm", history).status_code == 200

    n_summary = len(summary_calls())
    prefixes = []
    for k in range(3):
        history = history + convo(1, start=500 + k, head=False)
        assert post("mm", history).status_code == 200
        prefixes.append(chat_calls()[-1]["fp"])

    assert len(summary_calls()) == n_summary, "这几轮不该再触发压缩"
    for a, b in zip(prefixes, prefixes[1:]):
        assert b[:len(a)] == a, "新增消息只能追加在尾部，前缀必须原封不动"
        assert len(b) == len(a) + 2
    # [0]=原有 system，[1]=摘要，两次请求之间必须完全一致
    assert prefixes[0][0] == prefixes[-1][0]
    assert prefixes[0][1] == prefixes[-1][1]


def _tool_convo(rounds: int) -> list[dict]:
    msgs: list[dict] = [{"role": "system", "content": "你是助手。"}]
    for i in range(rounds):
        msgs.append({"role": "user", "content": f"【第{i}轮】帮我查一下 {FILLER * 2}"})
        msgs.append({"role": "assistant", "content": "",
                     "tool_calls": [{"id": f"call_{i}", "type": "function",
                                     "function": {"name": "search",
                                                  "arguments": f'{{"q":"第{i}轮查询"}}'}}]})
        msgs.append({"role": "tool", "tool_call_id": f"call_{i}",
                     "content": f"第{i}轮工具返回：{FILLER}"})
        msgs.append({"role": "assistant", "content": f"【第{i}轮答】{FILLER * 2}"})
    return msgs


def test_tool_call_rounds_are_never_split():
    """答案三：切点只落在轮边界，assistant(tool_calls) 和它的 tool 返回永不被拆开。"""
    fresh()
    history = _tool_convo(25)
    r = post("mm", history)
    assert r.status_code == 200, r.text
    call = chat_calls()[-1]

    # 保留区的第一条必须是 user（轮的起点），不能从半个工具调用中间开始
    assert call["roles"][1] == "system"          # 摘要
    assert call["roles"][2] == "user"

    # 上游收到的消息里不能有孤儿 tool
    seen_assistant = False
    for role in call["roles"][2:]:
        if role == "assistant":
            seen_assistant = True
        if role == "tool":
            assert seen_assistant, "tool 消息前面必须有发起调用的 assistant"

    # 送进摘要模型的每一批也都从 user 开始、以 assistant 收束，
    # 即批边界只落在轮边界上，工具调用四件套不会被拆散
    for c in summary_calls():
        transcript = c["prompt"].split(CONTEXT_MARK, 1)[-1].strip()
        blocks = [b for b in transcript.split("\n\n") if b.startswith("[")]
        assert blocks[0].startswith("[user]:"), blocks[0][:80]
        assert blocks[-1].startswith("[assistant]:"), blocks[-1][:80]
        # 每个 [tool] 块前面一定紧跟着一个发起调用的 assistant
        for i, b in enumerate(blocks):
            if b.startswith("[tool]:"):
                assert blocks[i - 1].startswith("[assistant]:") and "调用工具" in blocks[i - 1]


def test_deep_early_edit_falls_back_with_branch_warning():
    fresh(checkpoint_keep=10)
    history = convo(30)
    assert post("mm", history).status_code == 200
    assert httpx.get(f"{PROXY}/health", timeout=5).json()["fallback_activations"] == 0

    # 改掉第 2 条消息：所有 checkpoint（含置顶的最早那条）的已压缩区都被波及
    forked = list(history)
    forked[1] = {"role": "user", "content": "【彻底改掉的第一条提问】" + FILLER * 3}
    r = post("mm", forked)
    assert r.status_code == 200, r.text

    health = httpx.get(f"{PROXY}/health", timeout=5).json()
    assert health["fallback_activations"] == 1, "极端早期编辑应当走兜底并计数"
    # 兜底时注入的摘要必须显式标注衔接处可能重叠或跳跃
    preview = chat_calls()[-1]["system_preview"]
    assert preview is not None
    sysmsgs = [c for c in chat_calls()[-1]["roles"] if c == "system"]
    assert len(sysmsgs) >= 2
    detail = session_detail(sessions()[0]["conv_id"])
    assert any(c["kind"] == "fallback" for c in detail["checkpoints"])


def test_batch_failure_saves_progress_and_next_request_resumes():
    fresh()
    ctl(fail_after=2)                      # 前两批成功，之后一直失败
    history = convo(40)
    r = post("mm", history)
    assert r.status_code == 503
    detail = r.json()["error"]["detail"]
    assert detail["progress_saved"] is True
    assert detail["round_upto"] > 0
    assert "已经保存" in r.json()["error"]["message"]

    saved = sessions()[0]
    assert saved["status"] == "partial", "失败的压缩事件应保持 partial 以便续压"
    assert saved["compressed_upto"] > 0

    ctl(fail_after=-1)
    r = post("mm", history)
    assert r.status_code == 200, r.text
    detail = session_detail(sessions()[0]["conv_id"])
    assert [c["seq"] for c in detail["checkpoints"]] == [1], "应当续接同一个压缩事件，而不是新开一个"
    assert detail["checkpoints"][0]["status"] == "sealed"
    # 8 批总量 = 2 次成功 + 1 次失败 + 续压 6 次，已完成的批次一次都没重压
    assert len(summary_calls()) == 9, [c["n_messages"] for c in summary_calls()]


def test_max_batches_per_request_makes_progress_each_time():
    fresh(max_batches_per_request=1)
    history = convo(60)
    seen = []
    status = None
    for _ in range(30):
        r = post("mm", history)
        status = r.status_code
        seen.append(sessions()[0]["compressed_upto"] if sessions() else 0)
        if status == 200:
            break
        assert r.status_code == 503
        assert r.json()["error"]["detail"]["progress_saved"] is True
    assert status == 200, "分多次请求最终应当压完"
    assert seen == sorted(seen) and seen[-1] > seen[0], f"每次请求都要有进度: {seen}"


def test_exit_gate_blocks_uncompressible_request():
    fresh()
    huge = [{"role": "user", "content": FILLER * 400}]
    r = post("mm", huge)
    assert r.status_code == 503
    body = r.json()["error"]
    assert body["code"] == "compression_incomplete"
    assert body["detail"]["final_tokens"] > body["detail"]["gate_tokens"]
    assert not chat_calls(), "绝不能把超标请求转发给上游"


def test_never_forwards_more_than_gate():
    fresh()
    for extra in range(0, 60, 20):
        r = post("mm", convo(30 + extra))
        assert r.status_code in (200, 503)
    for c in chat_calls():
        # 上游实际收到的消息数始终被压住
        assert c["n_messages"] < 40


def test_text_only_provider_gets_no_images():
    fresh()
    history = convo(20)
    history[3] = {"role": "user", "content": [
        {"type": "text", "text": "看看这张图" + FILLER},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}
    history.append({"role": "user", "content": [
        {"type": "text", "text": "最新一张图" + FILLER},
        {"type": "image_url", "image_url": {"url": "file-已被删除的私有id"}}]})

    r = post("text", history)
    assert r.status_code == 200, r.text
    assert not chat_calls()[-1]["has_image"], "非多模态供应商不能收到图片"

    r = post("mm", history)
    assert r.status_code == 200
    # 多模态供应商保留合法图片，但失效引用被换成占位文本
    body = json.dumps(chat_calls()[-1], ensure_ascii=False)
    assert "file-已被删除的私有id" not in body


def test_recompress_writes_new_checkpoint_and_keeps_history():
    fresh(summary_total_cap_tokens=120, checkpoint_keep=3)
    ctl(summary_text="## 用户背景与偏好\n" + "关键事实条目，保留数值 42。" * 12)
    assert post("mm", convo(30)).status_code == 200
    detail = session_detail(sessions()[0]["conv_id"])
    kinds = [c["kind"] for c in detail["checkpoints"]]
    assert "recompress" in kinds, kinds
    assert any(c["pinned"] for c in detail["checkpoints"]), "最早的 checkpoint 必须永久保留"
    # 二次重压和批次摘要同构：按阈值分批 + 系统拼接，没有额外的"合并成稿"调用
    recompress_calls = [c for c in summary_calls() if "无损精简" in c["prompt"]]
    assert recompress_calls, "应当调过二次重压"
    assert not any("合并" in c["prompt"] for c in summary_calls()), "不该再有单独的合并调用"
    merged = _summary_api(sessions()[0]["conv_id"]).json()["summary"]
    assert merged and len(merged) < 4000


def test_checkpoint_window_keeps_earliest_and_recent():
    fresh(checkpoint_keep=2)
    history = convo(30)
    assert post("mm", history).status_code == 200
    for k in range(5):
        history = history + convo(25, start=1000 + k * 100)
        post("mm", history)
    detail = session_detail(sessions()[0]["conv_id"])
    seqs = sorted(c["seq"] for c in detail["checkpoints"])
    assert any(c["pinned"] for c in detail["checkpoints"])
    assert seqs[0] == 1, "置顶的最早 checkpoint 不能被淘汰"
    assert len(seqs) <= 2 + 1 + 1


def test_concurrent_identical_requests_compress_once():
    fresh()
    history = convo(40)
    results: list[int] = []

    def go():
        results.append(post("mm", history).status_code)

    ts = [threading.Thread(target=go) for _ in range(3)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert results == [200, 200, 200]
    ss = sessions()
    assert len(ss) == 1, f"并发请求不该产生多个会话: {ss}"
    # 有锁的话第二、三个请求进来时已经压完了，只会复用
    assert len(summary_calls()) <= 8


def test_stream_completes_and_terminates():
    fresh()
    with httpx.stream("POST", f"{PROXY}/mm/v1/chat/completions", headers=HEADERS, timeout=60,
                      json={"model": "mock-chat", "messages": convo(30), "stream": True}) as r:
        assert r.status_code == 200
        lines = [ln for ln in r.iter_lines() if ln.strip()]
    assert lines[-1].strip() == "data: [DONE]"
    text = "".join(json.loads(ln[5:])["choices"][0]["delta"].get("content", "")
                   for ln in lines if ln.startswith("data:") and ln[5:].strip() != "[DONE]")
    assert "好的，我记住了。" in text
    thinking = [ln for ln in lines if "reasoning_content" in ln]
    assert thinking, "压缩阶段应当往 think 区吐进度"


def test_stream_refusal_is_sse_not_http_error():
    fresh()
    ctl(fail_next=99)
    with httpx.stream("POST", f"{PROXY}/mm/v1/chat/completions", headers=HEADERS, timeout=60,
                      json={"model": "mock-chat", "messages": convo(40), "stream": True}) as r:
        assert r.status_code == 200, "流式场景不能返 HTTP 4xx/5xx"
        lines = [ln for ln in r.iter_lines() if ln.strip()]
    blob = "\n".join(lines)
    assert "compression_incomplete" in blob
    assert "已经保存" in blob
    assert lines[-1].strip() == "data: [DONE]"
    assert not chat_calls(), "压缩失败时不能转发给上游"


def test_auth_error_is_not_retried_blindly():
    fresh()
    ctl(fail_next=99, fail_status=402, fail_body={"error": {"message": "insufficient balance"}})
    r = post("mm", convo(40))
    assert r.status_code == 503
    msg = r.json()["error"]["message"]
    assert "余额" in msg or "insufficient" in msg.lower()
    # 402 不该被无差别重试：一批只调一次
    assert len(summary_calls()) == 1


def test_rate_limit_is_retried():
    fresh(main_max_attempts=3)
    ctl(fail_next=2, fail_status=429, fail_body={"error": {"message": "rate limited"}})
    r = post("mm", convo(30))
    assert r.status_code == 200, r.text
    assert len(summary_calls()) >= 3


FALLBACK = {"enabled": True, "base_url": f"{MOCK}/v1", "api_key": "sk-backup",
            "model": "mock-summary-backup", "max_attempts": 3}


def test_falls_back_to_backup_summary_model():
    """主模型打光重试次数后必须真的切到备用模型，而不是在端点字典上炸掉。"""
    fresh(main_max_attempts=1, fallback=FALLBACK)
    ctl(fail_next=99, fail_status=500, fail_body={"error": {"message": "boom"}})
    r = post("mm", convo(30))
    assert r.status_code == 200, r.text

    used = [c["model"] for c in summary_calls()]
    backup = used.count("mock-summary-backup")
    assert backup >= 1, f"没切到备用模型: {used}"
    # 每一批都是"主试 1 次失败 → 备用兜住"，所以两边次数相等
    assert used.count("mock-summary") == backup, used
    # 压缩确实完成了：上游收到的条数远少于发进来的
    assert chat_calls()[-1]["n_messages"] < len(convo(30))


def test_backup_summary_model_retries_its_own_max_attempts():
    """备用模型也会失败：max_attempts 必须被读到（漏传会 KeyError 而不是重试）。"""
    fresh(main_max_attempts=1,
          fallback={**FALLBACK, "model": "mock-summary", "max_attempts": 3})
    ctl(fail_next=99, fail_status=500, fail_body={"error": {"message": "boom"}})
    r = post("mm", convo(40))
    assert r.status_code == 503, r.text
    # 主 1 次 + 备 3 次 = 4；漏传 max_attempts 的话这里会是 500 KeyError
    assert len(summary_calls()) == 4, [c["model"] for c in summary_calls()]
    assert "KeyError" not in r.text


def test_fallback_endpoint_carries_all_required_fields():
    """端点字典的字段清单：调用链用到哪个就必须有哪个，缺一个就是运行时 KeyError。"""
    fresh(fallback=FALLBACK)
    from cproxy import config as _c
    eps = _c.summary_endpoints()
    assert [e["tag"] for e in eps] == ["primary", "fallback"]
    for ep in eps:
        assert set(ep) >= {"tag", "base_url", "api_key", "model", "summary_max_tokens",
                           "max_tokens_field", "timeout_seconds", "max_attempts",
                           "min_output_tokens", "extra_body"}, ep
        assert isinstance(ep["max_attempts"], int) and ep["max_attempts"] >= 1
    assert eps[1]["max_attempts"] == 3


def test_fallback_uses_its_own_request_options():
    fallback = {**FALLBACK, "summary_max_tokens": 321,
                "max_tokens_field": "max_completion_tokens", "timeout_seconds": 12,
                "min_output_tokens": 5, "extra_body": {"temperature": 0.2, "seed": 7}}
    fresh(main_max_attempts=1, fallback=fallback)
    ctl(fail_next=99, fail_status=500, fail_body={"error": {"message": "boom"}})
    assert post("mm", convo(30)).status_code == 200
    backup = next(c for c in summary_calls() if c["model"] == "mock-summary-backup")
    assert backup["extra"]["max_completion_tokens"] == 321
    assert backup["extra"]["temperature"] == 0.2
    assert backup["extra"]["seed"] == 7


def test_fallback_inherits_primary_extra_body():
    fresh(main_max_attempts=1, extra_body={"temperature": 0.5, "enable_thinking": False},
          fallback=FALLBACK)
    ctl(fail_next=99, fail_status=500, fail_body={"error": {"message": "boom"}})
    assert post("mm", convo(30)).status_code == 200
    backup = [c for c in summary_calls() if c["model"] == "mock-summary-backup"]
    assert backup, [c["model"] for c in summary_calls()]
    assert backup[0]["extra"]["temperature"] == 0.5
    assert backup[0]["extra"]["enable_thinking"] is False


def test_short_output_counts_as_failure():
    fresh(main_max_attempts=2)
    ctl(short_next=1)
    r = post("mm", convo(30))
    assert r.status_code == 200, r.text
    assert len(summary_calls()) >= 2, "过短输出应判失败并重试"


def test_legacy_sessions_table_is_migrated():
    _counter["n"] += 1
    db_rel = f"db/legacy{_counter['n']}.db"
    db_abs = TMP / db_rel
    db_abs.parent.mkdir(parents=True, exist_ok=True)

    history = convo(30)
    body = [m for m in history if m.get("role") != "system"]
    from cproxy import messages as M
    legacy_id = M.legacy_conv_id(body)
    upto = 20
    conn = sqlite3.connect(db_abs)
    conn.execute("CREATE TABLE sessions (conv_id TEXT PRIMARY KEY, summary TEXT, "
                 "compressed_upto INTEGER, boundary_fp TEXT, ts REAL)")
    conn.execute("INSERT INTO sessions VALUES (?,?,?,?,?)",
                 (legacy_id, "## 用户背景与偏好\n迁移过来的历史摘要内容。", upto,
                  M.legacy_boundary_fp(body[upto - 1]), time.time()))
    conn.commit()
    conn.close()

    write_config(db_rel)
    app_module.do_reload()
    httpx.post(f"{MOCK}/__reset", timeout=5)

    assert httpx.get(f"{PROXY}/health", timeout=5).json()["legacy_migrated"] == 1
    r = post("mm", history)
    assert r.status_code == 200, r.text
    s = sessions()[0]
    assert s["compressed_upto"] >= upto, "迁移过来的会话不该从 0 重压"


# ---- 近期原文硬下限：压缩过的会话，任何路径都不能让原文低于 keep_recent_tokens ----

def test_recent_verbatim_floor_survives_user_deleting_recent_messages():
    """用户删掉近期若干轮后，compressed_upto 逼近末尾，原文窗口必须回退。"""
    fresh()
    history = convo(30)
    assert post("mm", history).status_code == 200
    upto = sessions()[0]["compressed_upto"]
    assert upto > 0

    # 砍掉压缩位置之后的绝大部分原文，只留 1 条
    truncated = history[:1] + history[1:][:upto + 1]
    r = post("mm", truncated)
    assert r.status_code == 200, r.text
    call = chat_calls()[-1]
    # 摘要仍在，但原文窗口回退了：转发条数明显多于 "只剩 1 条"
    assert call["roles"][1] == "system"
    assert call["n_messages"] > 4, call["roles"]
    # 已压缩位置本身没有被改写（只是展示窗口回退）
    assert sessions()[0]["compressed_upto"] == upto


def test_recent_verbatim_floor_survives_raising_keep_recent():
    """运行中调大 keep_recent_tokens，存量 checkpoint 的切点按旧值定，必须回退补足。"""
    fresh(keep_recent_tokens=300)
    history = convo(30)
    assert post("mm", history).status_code == 200
    before = chat_calls()[-1]["n_messages"]
    upto = sessions()[0]["compressed_upto"]

    # 只调大下限，不动 DB
    write_config(f"db/t{_counter['n']}.db", keep_recent_tokens=900)
    app_module.do_reload()

    r = post("mm", history)
    assert r.status_code == 200, r.text
    after = chat_calls()[-1]["n_messages"]
    assert after > before, f"下限调大后原文应当变多: {before} -> {after}"
    assert sessions()[0]["compressed_upto"] == upto, "只回退展示窗口，不改写已压缩位置"


def test_recent_verbatim_floor_after_legacy_migration():
    """旧库迁移来的 compressed_upto 出自另一套阈值，同样受下限保护。"""
    _counter["n"] += 1
    db_rel = f"db/floor{_counter['n']}.db"
    db_abs = TMP / db_rel
    db_abs.parent.mkdir(parents=True, exist_ok=True)

    history = convo(30)
    body = [m for m in history if m.get("role") != "system"]
    from cproxy import messages as M
    upto = len(body) - 2          # 旧库把原文几乎全压掉了，只剩一轮
    conn = sqlite3.connect(db_abs)
    conn.execute("CREATE TABLE sessions (conv_id TEXT PRIMARY KEY, summary TEXT, "
                 "compressed_upto INTEGER, boundary_fp TEXT, ts REAL)")
    conn.execute("INSERT INTO sessions VALUES (?,?,?,?,?)",
                 (M.legacy_conv_id(body), "## 关键事实\n旧库摘要。", upto,
                  M.legacy_boundary_fp(body[upto - 1]), time.time()))
    conn.commit()
    conn.close()

    write_config(db_rel, keep_recent_tokens=1200)
    app_module.do_reload()
    httpx.post(f"{MOCK}/__reset", timeout=5)

    r = post("mm", history)
    assert r.status_code == 200, r.text
    call = chat_calls()[-1]
    assert call["n_messages"] > 4, f"迁移来的切点只剩 2 条原文，必须回退补足: {call['roles']}"


def test_floor_never_pushes_window_forward_in_steady_state():
    """稳态下不该有任何回退：原文窗口就等于已压缩位置，不产生重叠。"""
    fresh()
    history = convo(30)
    assert post("mm", history).status_code == 200
    upto = sessions()[0]["compressed_upto"]
    n_after_compress = chat_calls()[-1]["n_messages"]

    for k in range(3):
        history = history + convo(1, start=700 + k, head=False)
        assert post("mm", history).status_code == 200
    call = chat_calls()[-1]
    # 头部 system + 摘要 + (len(body) - upto) 条原文，一条不多一条不少
    body_len = len([m for m in history if m.get("role") != "system"])
    assert call["n_messages"] == 2 + (body_len - upto), call["roles"]
    assert call["n_messages"] == n_after_compress + 6


def test_keep_recent_is_capped_at_half_of_trigger():
    """配置写多大都没用：近期原文下限的有效值封顶在 trigger 的 50%。"""
    fresh(trigger_tokens=1200, keep_recent_tokens=5000)
    h = httpx.get(f"{PROXY}/health", timeout=5).json()
    assert h["keep_recent_tokens_configured"] == 5000
    assert h["keep_recent_tokens_effective"] == 600

    # 封顶之后下限不再和出口闸门打架，请求正常通过
    r = post("mm", convo(30))
    assert r.status_code == 200, r.text
    assert chat_calls()[-1]["n_messages"] < 60

    # 配置值小于一半时按配置来，不动它
    fresh(trigger_tokens=1200, keep_recent_tokens=300)
    h = httpx.get(f"{PROXY}/health", timeout=5).json()
    assert h["keep_recent_tokens_effective"] == 300


def test_tool_call_bloat_is_refused_with_actionable_message():
    """下限封顶后还顶穿闸门，只可能是最后一轮自己太大（最常见是工具调用）。
    这时直接报错，并明确告诉用户改消息、别让模型调工具。"""
    fresh(trigger_tokens=1200, keep_recent_tokens=400)
    history = convo(20)
    history += [
        {"role": "user", "content": "帮我查一下" + FILLER},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "search",
                                      "arguments": '{"q":"' + FILLER * 30 + '"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "工具返回：" + FILLER * 30},
    ]
    r = post("mm", history)
    assert r.status_code == 503, r.text
    d = r.json()["error"]["detail"]
    assert d["cause"] == "oversize_tail"
    assert d["tool_tokens"] > 0, "要能归因到工具调用占了多少"
    assert d["last_round_tokens"] > d["gate_tokens"] - d["keep_recent_floor"]
    msg = r.json()["error"]["message"]
    assert "工具调用" in msg and "编辑或缩短最后一条消息" in msg
    assert "避免在这一轮里让模型调用工具" in msg
    assert not chat_calls(), "绝不能把超标请求转发给上游"


def _legacy_state(db_rel, history, upto, **cfg):
    """造一个"已压缩到 upto、近期原文只剩几条"的存量会话（走旧库迁移那条路）。"""
    from cproxy import messages as M
    db_abs = TMP / db_rel
    db_abs.parent.mkdir(parents=True, exist_ok=True)
    body = [m for m in history if m.get("role") != "system"]
    conn = sqlite3.connect(db_abs)
    conn.execute("CREATE TABLE sessions (conv_id TEXT PRIMARY KEY, summary TEXT, "
                 "compressed_upto INTEGER, boundary_fp TEXT, ts REAL)")
    conn.execute("INSERT INTO sessions VALUES (?,?,?,?,?)",
                 (M.legacy_conv_id(body), "## 关键事实与专有名词\n旧库留下的累积摘要。", upto,
                  M.legacy_boundary_fp(body[upto - 1]), time.time()))
    conn.commit()
    conn.close()
    write_config(db_rel, **cfg)
    app_module.do_reload()
    httpx.post(f"{MOCK}/__reset", timeout=5)


def test_stuck_window_recovers_without_edit_and_survives_editing_last_message():
    """复现"近期原文只剩 3 条"的存量状态，验证三件事：
    1. 不需要用户做任何事，下一次请求窗口就自己回退补足；
    2. 编辑最后一条消息不会打乱定位，也不会触发全量重压或兜底；
    3. 对话继续增长后压缩正常推进，重叠自动消失（窗口不会永远停在回退状态）。
    """
    _counter["n"] += 1
    history = convo(40)
    body_len = len([m for m in history if m.get("role") != "system"])
    upto = body_len - 3                      # 只剩 3 条原文
    _legacy_state(f"db/stuck{_counter['n']}.db", history, upto,
                  trigger_tokens=1200, keep_recent_tokens=400)

    # 1) 什么都不改，直接发一次
    r = post("mm", history)
    assert r.status_code == 200, r.text
    first = chat_calls()[-1]
    assert first["n_messages"] > 3 + 2, f"窗口应当自动回退补足，实际只发了 {first['n_messages']} 条"
    assert httpx.get(f"{PROXY}/health", timeout=5).json()["fallback_activations"] == 0
    assert sessions()[0]["compressed_upto"] == upto, "只回退展示窗口，不改写已压缩位置"
    n_summary = len(summary_calls())

    # 迁移来的 checkpoint 已经补上指纹数组，从此能检测分叉
    detail = session_detail(sessions()[0]["conv_id"])
    assert detail["checkpoints"][0]["signature_len"] == body_len

    # 2) 编辑最后一条消息后重发
    edited = list(history)
    edited[-1] = {"role": "assistant", "content": "【改写后的最后一条回答】" + FILLER * 2}
    r = post("mm", edited)
    assert r.status_code == 200, r.text
    assert len(summary_calls()) == n_summary, "改最后一条不该触发重压"
    assert httpx.get(f"{PROXY}/health", timeout=5).json()["fallback_activations"] == 0
    assert sessions()[0]["compressed_upto"] == upto
    assert chat_calls()[-1]["n_messages"] == first["n_messages"], "窗口位置保持一致"

    # 3) 继续聊，直到攒够新内容触发压缩：压缩位置推进，重叠消失
    grown = edited
    for k in range(40):
        grown = grown + convo(1, start=900 + k, head=False)
        assert post("mm", grown).status_code == 200
        if sessions()[0]["compressed_upto"] > upto:
            break
    s = sessions()[0]
    assert s["compressed_upto"] > upto, "对话增长后压缩应当正常推进"
    grown_body = len([m for m in grown if m.get("role") != "system"])
    assert chat_calls()[-1]["n_messages"] == 2 + (grown_body - s["compressed_upto"]), \
        "推进之后窗口就等于已压缩位置，不再重叠"


# ---- 手工查看 / 修改摘要 ----

def _summary_api(conv_id, **kw):
    return httpx.get(f"{PROXY}/admin/session/{conv_id}/summary", headers=admin_h(),
                     params=kw, timeout=10)


def _put_summary(conv_id, text, base_seq=None):
    body = {"summary": text}
    if base_seq is not None:
        body["base_seq"] = base_seq
    return httpx.put(f"{PROXY}/admin/session/{conv_id}/summary", headers=admin_h(),
                     json=body, timeout=10)


def test_manual_summary_view_and_edit_takes_effect():
    fresh()
    history = convo(30)
    assert post("mm", history).status_code == 200
    cid = sessions()[0]["conv_id"]

    got = _summary_api(cid).json()
    assert got["summary"] and got["editable"] is True
    assert got["base_seq"] == 1 and got["total_rounds"] == 30
    # 纯文本视图，方便重定向到文件
    assert _summary_api(cid, format="text").text == got["summary"]

    hand = "## 用户背景与偏好\n用户叫小苦，讨厌被叫全名。\n## 关键事实与专有名词\n- 猫叫豆豆"
    r = _put_summary(cid, hand, base_seq=got["base_seq"])
    assert r.status_code == 200, r.text
    assert r.json()["new_seq"] == 2

    # 原 checkpoint 留着可回退，新的是 manual
    detail = session_detail(cid)
    kinds = {c["seq"]: c["kind"] for c in detail["checkpoints"]}
    assert kinds[2] == "manual" and kinds[1] == "incremental"

    # 下一次请求就用改过的摘要，且位置信息原样沿用
    assert post("mm", history).status_code == 200
    sysmsg = chat_calls()[-1]["system_preview"]
    assert _summary_api(cid).json()["summary"] == hand
    assert sessions()[0]["compressed_upto"] == detail["checkpoints"][0]["compressed_upto"]


def test_manual_summary_is_carried_forward_by_later_compression():
    fresh()
    history = convo(30)
    assert post("mm", history).status_code == 200
    cid = sessions()[0]["conv_id"]
    hand = "## 关键事实与专有名词\n- 这段是人写的，后续压缩必须保留它"
    assert _put_summary(cid, hand).status_code == 200

    grown = history + convo(30, start=400)
    assert post("mm", grown).status_code == 200
    now = _summary_api(cid).json()["summary"]
    assert now.startswith(hand), "后续压缩应当在手工摘要之后追加，而不是覆盖"


def test_manual_summary_rejects_stale_base_seq_and_open_event():
    fresh()
    assert post("mm", convo(30)).status_code == 200
    cid = sessions()[0]["conv_id"]
    seq = _summary_api(cid).json()["base_seq"]

    assert _put_summary(cid, "改动一", base_seq=seq).status_code == 200
    # 拿着旧的 base_seq 再存 -> 409，不会覆盖别人的更新
    r = _put_summary(cid, "改动二", base_seq=seq)
    assert r.status_code == 409 and "已被更新" in r.json()["error"]["message"]
    assert _summary_api(cid).json()["summary"] == "改动一"


def test_unfinished_compression_does_not_block_editing():
    """上次没压完（partial）是静止状态，不是"正在压缩"——不能因此永久锁住编辑。

    这正是用户遇到的现象：中转站报错/批次上限没压完之后，页面一直显示压缩中、改不了摘要。
    """
    fresh(max_batches_per_request=1)
    # 只压一批就返回：闸门会拦下这次请求（503），但已压的那批照样落盘，
    # 留下一条 partial —— 用户遇到的就是这个状态
    r = post("mm", convo(60))
    assert r.status_code == 503 and r.json()["error"]["detail"]["progress_saved"] is True
    cid = sessions()[0]["conv_id"]

    got = _summary_api(cid).json()
    assert got["status"] == "partial", "构造的就是没压完的状态"
    assert got["unfinished_event_seq"] is not None
    assert got["busy"] is False, "没有请求在跑，就不该说忙"
    assert got["editable"] is True, "没压完 ≠ 正在压缩，必须能改"

    r = _put_summary(cid, "## 关键事实\n手工写死的摘要", base_seq=got["base_seq"])
    assert r.status_code == 200, r.text
    after = _summary_api(cid).json()
    assert after["summary"].startswith("## 关键事实")
    # 半成品被作废了：不作废的话下次请求会绕回去接着压它，把手工摘要甩掉
    assert after["unfinished_event_seq"] is None
    stale = [c for c in session_detail(cid)["checkpoints"] if c["status"] == "stale"]
    assert stale, [c["status"] for c in session_detail(cid)["checkpoints"]]


def test_health_separates_running_from_unfinished():
    fresh(max_batches_per_request=1)
    assert post("mm", convo(60)).status_code == 503      # 没压完，进度已落盘
    h = httpx.get(f"{PROXY}/health", timeout=5).json()
    assert h["unfinished_compressions"] >= 1
    assert h["compressing_now"] == 0, "请求已经返回了，不该还算在「正在压缩」里"


def _activate(conv_id, seq):
    return httpx.post(f"{PROXY}/admin/session/{conv_id}/checkpoint/{seq}/activate",
                      headers=admin_h(), timeout=10)


def test_activate_old_checkpoint_becomes_the_live_summary():
    """把某条历史 checkpoint 设为当前：立刻用于发送，也用于后续继续压缩。"""
    fresh()
    assert post("mm", convo(30)).status_code == 200
    cid = sessions()[0]["conv_id"]
    first = _summary_api(cid).json()
    assert first["base_seq"] == 1

    # 制造第二条 checkpoint，让链上有多条可选
    assert _put_summary(cid, "## 关键事实\n第二版摘要", base_seq=first["base_seq"]).status_code == 200
    assert _summary_api(cid).json()["summary"].startswith("## 关键事实\n第二版")

    # 回到第 1 条
    r = _activate(cid, 1)
    assert r.status_code == 200, r.text
    assert r.json()["from_seq"] == 1 and r.json()["new_seq"] == 3
    live = _summary_api(cid).json()
    assert live["summary"] == first["summary"], "当前生效的应当是被指定的那条"
    assert live["base_seq"] == 3 and live["kind"] == "manual" and live["pinned"] == 2

    # 更晚的那条还在链上，可以再选回去
    seqs = {c["seq"] for c in session_detail(cid)["checkpoints"]}
    assert {1, 2, 3} <= seqs, seqs
    assert _activate(cid, 2).status_code == 200
    assert _summary_api(cid).json()["summary"].startswith("## 关键事实\n第二版")

    # 实际发出去的也换了：再发一条消息，上游收到的注入摘要就是它
    assert post("mm", convo(31)).status_code == 200
    assert "第二版摘要" in (chat_calls()[-1]["injected_summary"] or ""), \
        chat_calls()[-1]["injected_summary"]


def test_activated_checkpoint_survives_restart_and_further_compression():
    """长期生效：重启（重开库）之后仍然是它，后续压缩也在它之上追加。"""
    fresh(max_batches_per_request=2)
    assert post("mm", convo(40)).status_code in (200, 503)
    cid = sessions()[0]["conv_id"]

    hand = "## 关键事实与专有名词\n- 这条是人写的，必须一直生效"
    got = _summary_api(cid).json()
    assert _put_summary(cid, hand, base_seq=got["base_seq"]).status_code == 200
    pinned_seq = _summary_api(cid).json()["base_seq"]

    # 重开数据库 = 进程重启后的状态
    app_module.do_reload()
    after = _summary_api(cid).json()
    assert after["base_seq"] == pinned_seq and after["summary"] == hand
    assert after["unfinished_event_seq"] is None, "半成品已作废，不该再被续压"

    # 后续压缩在它之上追加，而不是绕回系统自动压的那条
    for _ in range(6):
        if post("mm", convo(40)).status_code == 200:
            break
    now = _summary_api(cid).json()["summary"]
    assert now.startswith(hand), now[:200]

    # 手工那条永远留在链上（不会被 checkpoint_keep 裁掉）
    cks = session_detail(cid)["checkpoints"]
    assert any(c["seq"] == pinned_seq for c in cks), [c["seq"] for c in cks]


def test_activate_rejects_unknown_or_empty_checkpoint():
    fresh()
    assert post("mm", convo(30)).status_code == 200
    cid = sessions()[0]["conv_id"]
    r = _activate(cid, 999)
    assert r.status_code == 404 and "没有 seq=999" in r.json()["error"]["message"]


def test_manual_summary_rejects_empty_and_over_cap():
    fresh(summary_total_cap_tokens=200)
    assert post("mm", convo(30)).status_code == 200
    cid = sessions()[0]["conv_id"]
    assert _put_summary(cid, "   ").status_code == 400
    r = _put_summary(cid, "太长了" * 500)
    assert r.status_code == 400 and "summary_total_cap_tokens" in r.json()["error"]["message"]


def test_ctl_sh_edit_round_trip():
    """真跑一遍 ./ctl.sh edit，用假编辑器改写文件。"""
    import subprocess
    fresh()
    assert post("mm", convo(30)).status_code == 200
    cid = sessions()[0]["conv_id"]

    fake_editor = TMP / "fake_editor.sh"
    fake_editor.write_text(
        "#!/bin/sh\nprintf '%s' '## 关键事实与专有名词\\n- 由 ctl.sh 写入' > \"$1\"\n",
        encoding="utf-8",
    )
    fake_editor.chmod(0o755)
    env = {**os.environ, "EDITOR": str(fake_editor), "PROXY_AUTH_TOKEN": TOKEN,
           "PROXY_PORT": str(PROXY_PORT)}

    out = subprocess.run(["bash", str(ROOT / "ctl.sh"), "summary", cid[:12]],
                         capture_output=True, text=True, encoding="utf-8", env=env, cwd=ROOT)
    assert out.returncode == 0 and "##" in out.stdout, out

    out = subprocess.run(["bash", str(ROOT / "ctl.sh"), "edit", cid[:12]],
                         capture_output=True, text=True, encoding="utf-8", env=env, cwd=ROOT)
    assert out.returncode == 0, out.stderr
    assert '"status": "updated"' in out.stdout, out.stdout
    assert "由 ctl.sh 写入" in _summary_api(cid).json()["summary"]


def test_ctl_sh_prefers_ui_token_over_auth_token():
    """配了 ui_token 后服务端不再认 auth_token，ctl.sh 必须自己切过去。"""
    import subprocess
    fresh(ui_token=UI_TOKEN)
    base = {**os.environ, "PROXY_PORT": str(PROXY_PORT), "PROXY_AUTH_TOKEN": TOKEN}
    only_auth = subprocess.run(["bash", str(ROOT / "ctl.sh"), "sessions"],
                               capture_output=True, text=True, encoding="utf-8", env=base, cwd=ROOT)
    assert "unauthorized" in only_auth.stdout, only_auth.stdout

    with_ui = subprocess.run(["bash", str(ROOT / "ctl.sh"), "sessions"], capture_output=True,
                             text=True, encoding="utf-8",
                             env={**base, "PROXY_UI_TOKEN": UI_TOKEN}, cwd=ROOT)
    assert with_ui.returncode == 0 and '"sessions"' in with_ui.stdout, with_ui.stdout


# ---- 可视化页面与鉴权 ----

def test_ui_disabled_without_token():
    fresh()
    r = httpx.get(f"{PROXY}/ui", timeout=5)
    assert r.status_code == 404
    assert "ui_token" in r.json()["error"]["message"]


def test_ui_page_served_when_token_set():
    fresh(ui_token=UI_TOKEN)
    r = httpx.get(f"{PROXY}/ui", timeout=5)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    body = r.text
    assert "context-proxy" in body and "ui_token" in body
    # 自包含：不引任何外部资源
    assert "http://" not in body.replace("http://127.0.0.1", "") or "cdn" not in body.lower()
    assert "<script src" not in body and "<link" not in body
    # 密钥绝不能出现在页面里
    assert UI_TOKEN not in body


def test_two_tokens_are_fully_isolated():
    """配了 ui_token 之后：ui_token 只能进后台，auth_token 只能走对话，互不越界。"""
    fresh(ui_token=UI_TOKEN)
    ui_h = {"Authorization": f"Bearer {UI_TOKEN}"}
    assert httpx.get(f"{PROXY}/admin/sessions", headers=ui_h, timeout=5).status_code == 200
    # ui_token 不能拿来调对话接口（不然等于把付费通道也交出去了）
    r = httpx.post(f"{PROXY}/mm/v1/chat/completions", headers=ui_h,
                   json={"model": "mock-chat", "messages": convo(2)}, timeout=10)
    assert r.status_code == 401
    # 反向也要挡住：auth_token 要填进 chatbox、跟着每个请求走，不能顺带是后台密码
    from cproxy import app as _a
    _a._FAIL.clear()
    assert httpx.get(f"{PROXY}/admin/sessions", headers=HEADERS, timeout=5).status_code == 401
    _a._FAIL.clear()
    assert post("mm", convo(2)).status_code == 200


def test_auth_token_still_opens_admin_when_no_ui_token():
    """没配 ui_token 时页面本来就是关的，此时 auth_token 得能进后台，否则 ctl.sh 全废。"""
    fresh()
    assert httpx.get(f"{PROXY}/admin/sessions", headers=HEADERS, timeout=5).status_code == 200
    assert httpx.post(f"{PROXY}/admin/reload", headers=HEADERS, timeout=10).status_code == 200


def test_wrong_key_is_rejected_then_throttled():
    fresh(ui_token=UI_TOKEN)
    bad = {"Authorization": "Bearer wrong-key-here"}
    codes = [httpx.get(f"{PROXY}/admin/sessions", headers=bad, timeout=5).status_code
             for _ in range(12)]
    assert codes[0] == 401
    assert 429 in codes, f"连续错误应当触发限流: {codes}"
    # 限流是按来源计的，正确密钥此时也会被挡（同一来源），清掉计数后恢复
    from cproxy import app as _a
    _a._FAIL.clear()
    assert httpx.get(f"{PROXY}/admin/sessions", headers=admin_h(), timeout=5).status_code == 200


def test_ui_summary_edit_flow_through_admin_api():
    """页面用的就是这几个接口，用 ui_token 完整走一遍。"""
    fresh(ui_token=UI_TOKEN)
    assert post("mm", convo(30)).status_code == 200
    ui_h = {"Authorization": f"Bearer {UI_TOKEN}"}
    cid = httpx.get(f"{PROXY}/admin/sessions", headers=ui_h, timeout=5).json()["sessions"][0]["conv_id"]
    got = httpx.get(f"{PROXY}/admin/session/{cid}/summary", headers=ui_h, timeout=5).json()
    r = httpx.put(f"{PROXY}/admin/session/{cid}/summary", headers=ui_h, timeout=5,
                  json={"summary": "## 关键事实\n页面改的", "base_seq": got["base_seq"]})
    assert r.status_code == 200 and r.json()["new_seq"] == got["base_seq"] + 1


# ---- 提示词读写 ----

def test_prompt_edit_writes_back_to_config_file():
    """页面上保存 = 直接写回 config.yaml，重启后依然生效。"""
    fresh(ui_token=UI_TOKEN)
    got = httpx.get(f"{PROXY}/admin/prompts", headers=admin_h(), timeout=5).json()["prompts"]
    names = {p["name"] for p in got}
    assert names == {"batch", "recompress", "injection", "fallback_notice"}
    assert all(p["overridden"] is False for p in got)

    mark = "【这是页面上改的批次摘要提示词】"
    r = httpx.put(f"{PROXY}/admin/prompts", headers=admin_h(), timeout=5,
                  json={"prompts": {"batch": mark + "请压缩下面的对话。\n\n以下是待压片段\n{{context}}"}})
    assert r.status_code == 200, r.text
    assert r.json()["written"] == ["batch"] and r.json()["target"] == "config.yaml"

    # 真的落到文件里了，而不是只在内存
    on_disk = yaml.safe_load(CFG_PATH.read_text(encoding="utf-8"))
    assert mark in on_disk["summary"]["prompts"]["batch"]
    assert (CFG_PATH.parent / (CFG_PATH.name + ".bak")).exists(), "改配置前要留一份备份"

    # 下一次压缩就用改过的提示词，而且整条走 user 角色
    assert post("mm", convo(30)).status_code == 200
    sc = summary_calls()
    assert sc and all(c["roles"] == ["user"] for c in sc), [c["roles"] for c in sc]
    assert all(c["prompt"].startswith(mark) for c in sc), sc[0]["prompt"][:120]
    assert all(CONTEXT_MARK in c["prompt"] and "【第" in c["prompt"] for c in sc)

    # 热重载后仍然是文件里的值（不再依赖内存覆盖项）
    app_module.do_reload()
    after = {p["name"]: p for p in
             httpx.get(f"{PROXY}/admin/prompts", headers=admin_h(), timeout=5).json()["prompts"]}
    assert after["batch"]["overridden"] is False, "写进文件之后就不该再挂内存覆盖项"
    assert mark in after["batch"]["from_file"] and mark in after["batch"]["effective"]

    # 提交空字符串 = 恢复内置默认值，同样写回文件
    httpx.put(f"{PROXY}/admin/prompts", headers=admin_h(), timeout=5,
              json={"prompts": {"batch": ""}})
    back = yaml.safe_load(CFG_PATH.read_text(encoding="utf-8"))["summary"]["prompts"]["batch"]
    assert mark not in back and "{{context}}" in back


def test_prompt_write_back_keeps_comments_and_other_keys():
    """只换提示词正文，文件里的注释和别的配置一字不动。"""
    fresh(ui_token=UI_TOKEN)
    raw = CFG_PATH.read_text(encoding="utf-8")
    CFG_PATH.write_text(raw.replace("summary:\n", "# 一条不能被冲掉的注释\nsummary:\n", 1),
                        encoding="utf-8")
    app_module.do_reload()

    r = httpx.put(f"{PROXY}/admin/prompts", headers=admin_h(), timeout=5,
                  json={"prompts": {"recompress": "精简一下\n\n以下是待压片段\n{{context}}"}})
    assert r.status_code == 200, r.text
    text = CFG_PATH.read_text(encoding="utf-8")
    assert "# 一条不能被冲掉的注释" in text
    cfg = yaml.safe_load(text)
    assert cfg["summary"]["prompts"]["recompress"].startswith("精简一下")
    assert "batch" not in cfg["summary"]["prompts"], "只写改的那条，其余仍走内置默认值"
    assert cfg["server"]["auth_token"] == TOKEN and cfg["providers"][0]["name"] == "mm"
    eff = {p["name"]: p for p in
           httpx.get(f"{PROXY}/admin/prompts", headers=admin_h(), timeout=5).json()["prompts"]}
    assert eff["batch"]["effective"] == config_mod.FALLBACK_PROMPTS["batch"]


def test_prompt_without_context_placeholder_is_rejected():
    fresh(ui_token=UI_TOKEN)
    r = httpx.put(f"{PROXY}/admin/prompts", headers=admin_h(), timeout=5,
                  json={"prompts": {"batch": "压缩一下这段对话"}})
    assert r.status_code == 400 and "{{context}}" in r.json()["error"]["message"]


def test_legacy_batch_system_prompt_is_migrated():
    """旧配置里的 batch_system（走 system 角色、没有占位符）要能自动升级。"""
    _counter["n"] += 1
    write_config(f"db/lg{_counter['n']}.db", ui_token=UI_TOKEN)
    cfg = yaml.safe_load(CFG_PATH.read_text(encoding="utf-8"))
    cfg["summary"]["prompts"] = {"batch_system": "老提示词：把对话压成要点"}
    CFG_PATH.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    _set_admin_token(UI_TOKEN)
    app_module.do_reload()
    httpx.post(f"{MOCK}/__reset", timeout=5)

    got = {p["name"]: p for p in
           httpx.get(f"{PROXY}/admin/prompts", headers=admin_h(), timeout=5).json()["prompts"]}
    assert "batch_system" not in got
    assert got["batch"]["effective"].startswith("老提示词")
    assert "{{context}}" in got["batch"]["effective"], "缺占位符要自动补上，否则正文会丢"

    assert post("mm", convo(30)).status_code == 200
    sc = summary_calls()
    assert sc and all(c["roles"] == ["user"] for c in sc)
    assert all(c["prompt"].startswith("老提示词") and "【第" in c["prompt"] for c in sc)


def test_injection_prompt_must_keep_placeholder():
    fresh(ui_token=UI_TOKEN)
    r = httpx.put(f"{PROXY}/admin/prompts", headers=admin_h(), timeout=5,
                  json={"prompts": {"injection": "忘了写占位符"}})
    assert r.status_code == 400 and "{summary}" in r.json()["error"]["message"]
    r = httpx.put(f"{PROXY}/admin/prompts", headers=admin_h(), timeout=5,
                  json={"prompts": {"不存在的提示词": "x"}})
    assert r.status_code == 400


# ---- 时间轴 ----

def test_timeline_disabled_by_default():
    fresh()
    assert post("mm", convo(30)).status_code == 200
    r = httpx.get(f"{PROXY}/admin/session/{sessions()[0]['conv_id']}/timeline",
                  headers=HEADERS, timeout=5)
    assert r.status_code == 404 and "capture_timeline" in r.json()["error"]["message"]


def test_timeline_marks_folded_rounds():
    _counter["n"] += 1
    write_config(f"db/tl{_counter['n']}.db", ui_token=UI_TOKEN)
    _set_admin_token(UI_TOKEN)
    cfg = yaml.safe_load(CFG_PATH.read_text(encoding="utf-8"))
    cfg["observability"] = {"capture_timeline": True, "preview_chars": 40}
    CFG_PATH.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    app_module.do_reload()
    httpx.post(f"{MOCK}/__reset", timeout=5)

    assert post("mm", convo(30)).status_code == 200
    cid = sessions()[0]["conv_id"]
    tl = httpx.get(f"{PROXY}/admin/session/{cid}/timeline", headers=admin_h(), timeout=5).json()

    assert tl["total_rounds"] == 30 and len(tl["rounds"]) == 30
    assert tl["in"]["messages"] == 60 and tl["out"]["messages"] < 60
    assert tl["out"]["tokens"] < tl["in"]["tokens"], "发出去的应当远小于进来的"

    folded = [r for r in tl["rounds"] if r["c"]]
    kept = [r for r in tl["rounds"] if not r["c"]]
    assert folded and kept, "应当既有被折叠的轮次也有逐字保留的"
    assert max(r["r"] for r in folded) < min(r["r"] for r in kept), "折叠的必须都在前面"
    assert all(len(r["p"]) <= 40 for r in tl["rounds"]), "预览按 preview_chars 截断"
    assert all("【第" in r["p"] for r in tl["rounds"])

    # 只留最新一份：再发一次请求会覆盖，不会越堆越多
    before = tl["at"]
    assert post("mm", convo(30) + convo(1, start=99, head=False)).status_code == 200
    tl2 = httpx.get(f"{PROXY}/admin/session/{cid}/timeline", headers=admin_h(), timeout=5).json()
    assert tl2["at"] > before and tl2["total_rounds"] == 31


# ---- body / header / query 透传与 extra_body ----

def test_body_params_pass_through_untouched():
    """messages 之外的字段一律原样透传，包括厂商私有字段。"""
    fresh()
    extras = {"temperature": 0.7, "top_p": 0.9, "seed": 42, "stop": ["\n\n"],
              "reasoning_effort": "xhigh", "thinking": {"type": "enabled", "budget_tokens": 8000},
              "enable_thinking": True, "thinking_budget": 4096,
              "tools": [{"type": "function", "function": {"name": "f"}}], "tool_choice": "auto",
              "response_format": {"type": "json_object"}, "user": "u-1",
              "厂商私有字段": "保留我"}
    assert post("mm", convo(2), **extras).status_code == 200
    got = chat_calls()[-1]["extra"]
    for k, v in extras.items():
        assert got.get(k) == v, f"{k} 没原样透传: {got.get(k)!r} != {v!r}"


def test_extra_body_overrides_client_and_protects_core_fields():
    fresh()
    cfg = yaml.safe_load(CFG_PATH.read_text(encoding="utf-8"))
    cfg["providers"] = [
        {"name": "mm", "base_url": f"{MOCK}/v1", "api_key": "sk-up", "multimodal": True,
         "extra_body": {"reasoning_effort": "xhigh", "temperature": 1.0,
                        "messages": [{"role": "user", "content": "偷天换日"}], "stream": True}},
        {"name": "text", "base_url": f"{MOCK}/v1", "api_key": "sk-up", "multimodal": False},
    ]
    CFG_PATH.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    app_module.do_reload()

    assert post("mm", convo(3), temperature=0.2, reasoning_effort="low").status_code == 200
    c = chat_calls()[-1]
    assert c["extra"]["reasoning_effort"] == "xhigh", "extra_body 应当覆盖客户端的 low"
    assert c["extra"]["temperature"] == 1.0
    # messages / stream 被保护：既没被换掉，也没被强行改成流式
    assert c["n_messages"] == len(convo(3)) and c["stream"] is False
    assert "偷天换日" not in json.dumps(c, ensure_ascii=False)


def test_summary_model_extra_body_applies():
    fresh(extra_body={"temperature": 0.5, "enable_thinking": False})
    assert post("mm", convo(30)).status_code == 200
    s = summary_calls()[0]
    assert s["extra"]["temperature"] == 0.5
    assert s["extra"]["enable_thinking"] is False
    # 骨架字段不受影响：提示词整条走 user，所以只有一条消息
    assert s["n_messages"] == 1 and s["roles"] == ["user"] and s["stream"] is False


def test_headers_are_not_forwarded_unless_whitelisted():
    fresh()
    h = {**HEADERS, "anthropic-beta": "interleaved-thinking", "X-Title": "my-app",
         "Cookie": "secret=1", "X-Forwarded-For": "10.0.0.9"}
    httpx.post(f"{PROXY}/mm/v1/chat/completions", headers=h, timeout=30,
               json={"model": "mock-chat", "messages": convo(2)})
    got = {k.lower(): v for k, v in chat_calls()[-1]["headers"].items()}
    assert "anthropic-beta" not in got and "x-title" not in got, "默认不该透传"
    assert got.get("authorization") == "Bearer sk-up", "鉴权头必须换成供应商的 key"

    cfg = yaml.safe_load(CFG_PATH.read_text(encoding="utf-8"))
    cfg["providers"][0]["forward_headers"] = ["anthropic-beta", "x-title",
                                              "authorization", "cookie"]   # 后两个应被拒
    CFG_PATH.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    app_module.do_reload()

    httpx.post(f"{PROXY}/mm/v1/chat/completions", headers=h, timeout=30,
               json={"model": "mock-chat", "messages": convo(2)})
    got = {k.lower(): v for k, v in chat_calls()[-1]["headers"].items()}
    assert got.get("anthropic-beta") == "interleaved-thinking"
    assert got.get("x-title") == "my-app"
    assert got.get("authorization") == "Bearer sk-up", "白名单里写 authorization 也不能生效"
    assert "cookie" not in got, "cookie 永远不透传"
    assert "x-forwarded-for" not in got


def test_query_string_forwarding_is_opt_in():
    fresh()
    httpx.post(f"{PROXY}/mm/v1/chat/completions?api-version=2024-08-01", headers=HEADERS,
               timeout=30, json={"model": "mock-chat", "messages": convo(2)})
    assert chat_calls()[-1]["query"] == "", "默认不透传查询串"

    cfg = yaml.safe_load(CFG_PATH.read_text(encoding="utf-8"))
    cfg["providers"][0]["forward_query"] = True
    CFG_PATH.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    app_module.do_reload()
    httpx.post(f"{PROXY}/mm/v1/chat/completions?api-version=2024-08-01", headers=HEADERS,
               timeout=30, json={"model": "mock-chat", "messages": convo(2)})
    assert chat_calls()[-1]["query"] == "api-version=2024-08-01"


# ---- body 字段探测工具 ----

def _run_probe(*args, env_extra=None):
    import subprocess
    # 显式钉住 PROXY_CONFIG：同一次 pytest 里别的测试模块也会改这个环境变量
    return subprocess.run([sys.executable, str(ROOT / "tools" / "probe_body.py"), *args,
                           "--retry-delay", "0"],
                          capture_output=True, text=True, cwd=ROOT,
                          env={**os.environ, "PROXY_CONFIG": str(CFG_PATH),
                               **(env_extra or {})})


def _probe_line(txt: str, prefix: str) -> str:
    """取报告结尾那几行汇总（"可用 (n)：..." / "被拒 (n)：..." / "无法判断 (n)：..."）。

    带上 " (" 一起匹配：结论段落里也有以"可用项"开头的句子，光按两个字前缀会撞上。
    """
    return next((l for l in txt.splitlines() if l.startswith(prefix + " (")), "")


def test_probe_stops_when_the_baseline_request_fails():
    """基线都发不出去时，后面每个字段都会"失败"，那份清单没有意义——必须停。"""
    fresh()
    ctl(chat_status=401)
    out = _run_probe("mm", "--model", "mock-chat", "--only", "temperature,reasoning_effort")
    assert out.returncode == 2, out.stdout
    assert "基线请求就没成功" in out.stdout and "探测中止" in out.stdout
    assert "api_key" in out.stdout, "要指出该去查什么"
    # 关键：一个探针都没发出去，没白烧钱也没生成假清单
    assert len(chat_calls()) == 1, [c["extra"] for c in chat_calls()]
    assert "可用" not in out.stdout and "会校验未知字段" not in out.stdout


def test_probe_refuses_to_call_a_429_control_strict():
    """对照组被限流 ≠ 这家会校验未知字段。这正是之前会误诊的那条路径。"""
    fresh()
    ctl(field_status={"__cproxy_probe_nonexistent__": 429})
    out = _run_probe("mm", "--model", "mock-chat", "--only", "temperature")
    assert out.returncode == 1, out.stdout          # 跑完了，但结论不确定
    assert "无法判断" in out.stdout
    assert "可按需写入 extra_body" not in out.stdout, "429 不能当成严格校验"
    assert "结论不确定" in out.stdout
    # 基线是好的，所以探针照跑，只是不下结论
    assert "temperature" in _probe_line(out.stdout, "可用")


def test_probe_refuses_to_conclude_on_a_500_or_network_error_control():
    fresh()
    for status in (500, 403):
        ctl(field_status={"__cproxy_probe_nonexistent__": status})
        out = _run_probe("mm", "--model", "mock-chat", "--only", "temperature")
        assert out.returncode == 1, (status, out.stdout)
        assert "可按需写入 extra_body" not in out.stdout, status
        assert "不校验未知字段" not in out.stdout, status


def test_probe_refuses_to_conclude_on_a_400_that_is_not_about_unknown_fields():
    """400 也不够——得看报错文本真的在说"这个字段我不认识"。"""
    fresh()
    ctl(strict_body=True, strict_message="You exceeded your current quota")
    out = _run_probe("mm", "--model", "mock-chat", "--only", "temperature")
    assert out.returncode == 1, out.stdout
    assert "看不出是不是在校验未知字段" in out.stdout
    assert "You exceeded your current quota" in out.stdout, "要把上游原文摆出来给人判断"


def test_probe_reports_rejected_fields_on_a_strict_upstream():
    """严格网关：基线通过 + 对照被 400 拒且文本指向未知字段 → 才敢下结论。"""
    fresh()
    ctl(strict_body=True, reasoning_for=[])
    out = _run_probe("mm", "--model", "mock-chat",
                     "--only", "temperature,top_p,reasoning_effort,enable_thinking")
    assert out.returncode == 0, out.stdout
    txt = out.stdout
    assert "会校验未知字段" in txt and "可按需写入 extra_body" in txt, txt
    assert "temperature" in _probe_line(txt, "可用")
    rejected = _probe_line(txt, "被拒")
    assert "reasoning_effort=minimal" in rejected and "enable_thinking=true" in rejected
    assert _probe_line(txt, "无法判断") == "", "没有存疑项时不该打这一行"
    # 每个探针都真发了一次最小请求（含基线），不会顺手把账单打爆
    probes = [c for c in chat_calls() if c["n_messages"] == 1]
    assert len(probes) >= 7 and all(c["chars"] < 200 for c in probes)


def test_probe_flags_a_lenient_upstream_as_inconclusive():
    """宽松网关：什么都收 2xx，报告必须说清楚"不代表生效"，并给出思考侧信号。"""
    fresh()
    ctl(strict_body=False, reasoning_for=["reasoning_effort", "thinking"])
    out = _run_probe("mm", "--model", "mock-chat", "--only", "reasoning_effort,temperature")
    assert out.returncode == 0, out.stdout
    txt = out.stdout
    assert "不校验未知字段" in txt, txt
    assert "仅确认不会报错" in txt and "不要仅凭本次结果修改 extra_body" in txt, txt
    assert "真的产生了思考内容的组合" in txt
    assert "reasoning_effort=high" in txt.split("真的产生了思考内容的组合")[1]
    # temperature 没被列进 reasoning_for，不该被误报成"开了思考"
    assert "temperature" not in txt.split("真的产生了思考内容的组合")[1]


def test_probe_retries_a_transient_failure_before_judging_a_field():
    """单个字段撞上一次限流，重试一次就好了——不该因此被记成"被拒"。"""
    fresh()
    ctl(strict_body=True, field_status={"temperature": 429}, field_status_once=True)
    out = _run_probe("mm", "--model", "mock-chat", "--only", "temperature,top_p")
    assert out.returncode == 0, out.stdout
    assert "(重试过)" in out.stdout
    assert "temperature" in _probe_line(out.stdout, "可用"), out.stdout
    assert "temperature" not in _probe_line(out.stdout, "被拒")


def test_probe_keeps_a_persistently_failing_field_out_of_the_rejected_list():
    """一直 429 的字段归入"无法判断"，并提示别照着改配置、单独重跑。"""
    fresh()
    ctl(strict_body=True, field_status={"temperature": 429})
    out = _run_probe("mm", "--model", "mock-chat", "--only", "temperature,top_p")
    assert out.returncode == 0, out.stdout      # 对照组本身是有结论的
    unsure = _probe_line(out.stdout, "无法判断")
    assert "temperature" in unsure and "top_p" not in unsure
    assert "temperature" not in _probe_line(out.stdout, "被拒")
    assert "别据此改 config.yaml" in out.stdout
    # 存疑时回头复查基线，确认不是端点整个挂了
    assert "基线复查" in out.stdout and "仍然正常" in out.stdout


def test_probe_reports_when_the_endpoint_died_midway():
    """探测中途端点挂了：复查基线也失败 → 整轮结果都不可信，必须收回结论。"""
    fresh()
    # 前 4 次正常（基线、对照、temperature ×2），之后全挂——最后那次复查基线就会失败
    ctl(field_status={"temperature": 503}, chat_status=503, chat_status_after=5)
    out = _run_probe("mm", "--model", "mock-chat", "--only", "temperature")
    assert out.returncode == 1, out.stdout
    assert "基线复查" in out.stdout and "也失败了" in out.stdout
    assert "整轮结果都不可信" in out.stdout
    assert "结论不确定" in out.stdout


def test_probe_accepts_custom_fields_and_reports_upstream_error_text():
    fresh()
    ctl(strict_body=True)
    out = _run_probe("mm", "--model", "mock-chat", "--only", "temperature",
                     "--extra", '{"厂商私有字段": 1}')
    assert out.returncode == 0, out.stdout
    assert "厂商私有字段(自定义)" in out.stdout
    # 上游的原始报错要照抄出来，不然用户不知道为什么被拒
    assert "Unrecognized request argument" in out.stdout


# ---- 摘要内容与压缩位置解耦 ----

def _two_checkpoints_apart():
    """造出两条压缩位置不同的 checkpoint：先压一段，再把对话续长触发第二次压缩。

    返回 (conv_id, 旧档, 当前生效档)。
    """
    fresh()
    assert post("mm", convo(30)).status_code == 200
    assert post("mm", convo(60)).status_code == 200          # 更长的历史 -> 第二次压缩事件
    cid = sessions()[0]["conv_id"]
    cks = sorted(session_detail(cid)["checkpoints"], key=lambda c: c["seq"])
    assert len(cks) >= 2 and cks[0]["round_upto"] < cks[-1]["round_upto"], \
        [(c["seq"], c["round_upto"]) for c in cks]
    return cid, cks[0], cks[-1]



def test_activate_keeps_compression_progress_by_default():
    """选一条旧摘要设为生效，**压缩进度不倒退**：内容换成它的，标记还停在原处。

    这是关键语义：压到第 1400 条、选了一条覆盖到第 800 条的旧摘要，
    用户要的是"这 1400 条对应的摘要换成这份"，不是让进度退回 800 去重压。
    """
    cid, old, live = _two_checkpoints_apart()
    r = _activate(cid, old["seq"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["rewind"] is False
    # 位置停在原处，内容来自旧档
    assert body["compressed_upto"] == live["compressed_upto"]
    assert body["round_upto"] == live["round_upto"]
    assert body["summary_covers_round"] == old["round_upto"]
    assert body["gap_rounds"] == live["round_upto"] - old["round_upto"] > 0
    assert "既不在这份摘要里" in body["note"], body["note"]

    now = _summary_api(cid).json()
    assert now["summary"] == _checkpoint_api(cid, old["seq"]).json()["summary"]
    assert now["compressed_upto"] == live["compressed_upto"], "进度不能倒退"

    # 后续压缩以这份摘要为基准往后追加，起点也还是原来的位置
    # （历史要接着 convo(60) 往下长，短历史会被当成另一段对话）
    assert post("mm", convo(70)).status_code in (200, 503)
    after = _summary_api(cid).json()
    assert after["summary"].startswith(old["summary_preview"][:40])
    assert after["compressed_upto"] >= live["compressed_upto"]


def test_activate_with_rewind_moves_the_marker_back():
    """显式要求时才回退进度：那段原文会被重新压一遍（贵，但没有空洞）。"""
    cid, old, live = _two_checkpoints_apart()
    r = httpx.post(f"{PROXY}/admin/session/{cid}/checkpoint/{old['seq']}/activate",
                   headers=admin_h(), json={"rewind": True}, timeout=10)
    assert r.status_code == 200, r.text
    assert r.json()["rewind"] is True and r.json()["gap_rounds"] == 0
    now = _summary_api(cid).json()
    assert now["compressed_upto"] == old["compressed_upto"] < live["compressed_upto"]


def _checkpoint_api(conv_id, seq):
    return httpx.get(f"{PROXY}/admin/session/{conv_id}/checkpoint/{seq}",
                     headers=admin_h(), timeout=10)


def test_overwrite_slot_does_not_change_which_one_is_live():
    """存档位语义：写回某个格子 ≠ 让它生效，这是两个动作。"""
    fresh()
    assert post("mm", convo(30)).status_code == 200
    cid = sessions()[0]["conv_id"]
    live = _summary_api(cid).json()
    assert _put_summary(cid, "## 关键事实\n第二版", base_seq=live["base_seq"]).status_code == 200
    live2 = _summary_api(cid).json()
    assert live2["base_seq"] == 2

    # 覆盖 1 号槽位，当前生效的仍是 2 号
    r = httpx.put(f"{PROXY}/admin/session/{cid}/checkpoint/1", headers=admin_h(), timeout=10,
                  json={"summary": "## 关键事实\n改写过的一号档"})
    assert r.status_code == 200 and r.json()["activated_seq"] is None
    assert _summary_api(cid).json()["summary"] == "## 关键事实\n第二版"
    assert "改写过的一号档" in _checkpoint_api(cid, 1).json()["summary"]
    # 链上没有多出新条目：存档是覆盖，不是每存一次加一条
    assert {c["seq"] for c in session_detail(cid)["checkpoints"]} == {1, 2}

    # 带 activate=true 才既存又生效
    r = httpx.put(f"{PROXY}/admin/session/{cid}/checkpoint/1", headers=admin_h(), timeout=10,
                  json={"summary": "## 关键事实\n一号档并生效", "activate": True})
    assert r.status_code == 200 and r.json()["activated_seq"] == 3
    assert _summary_api(cid).json()["summary"] == "## 关键事实\n一号档并生效"


def test_overwrite_refuses_a_partial_slot():
    fresh(max_batches_per_request=1)
    assert post("mm", convo(60)).status_code == 503
    cid = sessions()[0]["conv_id"]
    seq = _summary_api(cid).json()["base_seq"]
    r = httpx.put(f"{PROXY}/admin/session/{cid}/checkpoint/{seq}", headers=admin_h(), timeout=10,
                  json={"summary": "写进半成品里会被压缩覆盖掉"})
    assert r.status_code == 409 and "中间存档" in r.json()["error"]["message"]


def test_activate_refuses_a_summary_over_cap():
    fresh(summary_total_cap_tokens=100000)
    assert post("mm", convo(30)).status_code == 200
    cid = sessions()[0]["conv_id"]
    big = "很长的摘要内容。" * 400
    assert _put_summary(cid, big, base_seq=_summary_api(cid).json()["base_seq"]).status_code == 200
    seq = _summary_api(cid).json()["base_seq"]
    # 把 cap 调小，再去激活那条超长的：应当拦住，否则下次压缩会把它洗掉
    fresh_cfg = yaml.safe_load(CFG_PATH.read_text(encoding="utf-8"))
    fresh_cfg["summary"]["summary_total_cap_tokens"] = 50
    CFG_PATH.write_text(yaml.safe_dump(fresh_cfg, allow_unicode=True, sort_keys=False),
                        encoding="utf-8")
    app_module.do_reload()
    r = _activate(cid, seq)
    assert r.status_code == 400 and "summary_total_cap_tokens" in r.json()["error"]["message"]


# ---- 控制台：压缩任务 ----

def test_tasks_endpoint_lists_nothing_when_idle():
    fresh(max_batches_per_request=1)
    assert post("mm", convo(60)).status_code == 503
    d = httpx.get(f"{PROXY}/admin/tasks", headers=admin_h(), timeout=5).json()
    assert d["running"] == [], d
    assert d["unfinished_compressions"] >= 1


def test_cancel_stops_between_batches_and_keeps_progress():
    """中止在批与批之间生效：已落盘的批全保留，没压的下次接着压。"""
    import threading
    fresh(max_batches_per_request=20)
    ctl(delay_seconds=0.25)                 # 让每批慢下来，好在中途插进去
    cid_box, done = {}, threading.Event()

    def run():
        try:
            post("mm", convo(40), timeout=120)
        finally:
            done.set()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    # 等到任务真的开始跑
    cid = None
    for _ in range(200):
        d = httpx.get(f"{PROXY}/admin/tasks", headers=admin_h(), timeout=5).json()
        run_now = [x for x in d["running"] if x.get("conv_id")]
        if run_now and run_now[0]["batch"] >= 1:
            cid = run_now[0]["conv_id"]
            task = run_now[0]
            break
        time.sleep(0.05)
    assert cid, "没等到压缩任务跑起来"
    assert task["phase"] == "压缩中" and task["batches"] > 1
    assert task["rounds_total"] == 40

    r = httpx.post(f"{PROXY}/admin/session/{cid}/cancel", headers=admin_h(), timeout=5)
    assert r.status_code == 200 and r.json()["status"] == "cancelling"
    done.wait(120)

    detail = session_detail(cid)
    assert detail["checkpoints"], "已落盘的批次必须留着"
    upto = sessions()[0]["compressed_upto"]
    assert upto > 0, "中止不该把已完成的批次撕掉"
    # 中止后没有残留的运行态
    assert httpx.get(f"{PROXY}/admin/tasks", headers=admin_h(), timeout=5).json()["running"] == []

    # 下次请求接着压，不是从 0 重来
    ctl(delay_seconds=0)
    for _ in range(6):
        if post("mm", convo(40)).status_code == 200:
            break
    assert sessions()[0]["compressed_upto"] >= upto


def test_cancel_without_a_running_task_is_409():
    fresh()
    assert post("mm", convo(30)).status_code == 200
    cid = sessions()[0]["conv_id"]
    r = httpx.post(f"{PROXY}/admin/session/{cid}/cancel", headers=admin_h(), timeout=5)
    assert r.status_code == 409 and "没有正在执行的压缩" in r.json()["error"]["message"]


# ---- 控制台：模型与供应商配置 ----

def _models():
    return httpx.get(f"{PROXY}/admin/models", headers=admin_h(), timeout=10).json()


def _save_models(payload):
    return httpx.put(f"{PROXY}/admin/models", headers=admin_h(), json=payload, timeout=60)


def _test_model(payload):
    return httpx.post(f"{PROXY}/admin/models/test", headers=admin_h(), json=payload, timeout=60)


def _draft_from(d, **over):
    """把 GET 回来的配置整理成可提交的草稿（key 用掩码占位，表示不改）。"""
    provs = [{**p, "api_key": d["masked_placeholder"], "test_model": "mock-chat"}
             for p in d["providers"]]
    body = {"providers": provs,
            "summary": {**d["summary"], "api_key": d["masked_placeholder"]},
            "fallback": {**d["fallback"], "api_key": d["masked_placeholder"]}}
    body.update(over)
    return body


def test_models_endpoint_masks_keys():
    fresh()
    d = _models()
    assert [p["name"] for p in d["providers"]] == ["mm", "text"]
    for p in d["providers"]:
        assert p["has_key"] and "sk-up" not in json.dumps(p, ensure_ascii=False)
        assert "***" in p["api_key_masked"]
    assert d["summary"]["model"] == "mock-summary"
    assert "sk-summary" not in json.dumps(d, ensure_ascii=False), "真 key 一个字都不能回显"
    assert d["summary"]["max_tokens_field"] == "max_tokens"
    editable = {"base_url", "model", "summary_max_tokens", "max_tokens_field",
                "timeout_seconds", "max_attempts", "min_output_tokens", "extra_body"}
    assert editable <= d["summary"].keys()
    assert editable <= d["fallback"].keys()


def test_save_models_writes_config_and_keeps_masked_keys():
    fresh()
    d = _models()
    body = _draft_from(d)
    body["providers"][0]["extra_body"] = {"temperature": 0.3}
    body["summary"]["summary_max_tokens"] = 2048
    r = _save_models(body)
    assert r.status_code == 200, r.text
    assert all(c["verdict"] == "ok" for c in r.json()["checks"]), r.json()["checks"]

    cfg = yaml.safe_load(CFG_PATH.read_text(encoding="utf-8"))
    assert cfg["providers"][0]["extra_body"] == {"temperature": 0.3}
    assert cfg["providers"][0]["api_key"] == "sk-up", "提交掩码就该沿用旧 key，不能写成掩码"
    assert cfg["summary"]["summary_max_tokens"] == 2048
    # 热重载后立刻生效
    assert post("mm", convo(2)).status_code == 200
    assert chat_calls()[-1]["extra"]["temperature"] == 0.3


def test_save_models_rejects_a_broken_endpoint():
    """保存前真调一次，调不通就不写——写进去的下一秒对话就全挂了。"""
    fresh()
    d = _models()
    body = _draft_from(d)
    body["summary"]["model"] = "mock-summary"
    body["summary"]["extra_body"] = {"__cproxy_probe_nonexistent__": 1}
    ctl(strict_body=True)
    r = _save_models(body)
    assert r.status_code == 400, r.text
    assert "验证未通过" in r.json()["error"]["message"]
    failed = [c for c in r.json()["checks"] if c["verdict"] == "failed"]
    assert failed and "Unrecognized request argument" in failed[0]["raw"]
    # 没写进文件
    cfg = yaml.safe_load(CFG_PATH.read_text(encoding="utf-8"))
    assert not cfg["summary"].get("extra_body")

    # force 可以硬存
    body["force"] = True
    assert _save_models(body).status_code == 200
    cfg = yaml.safe_load(CFG_PATH.read_text(encoding="utf-8"))
    assert cfg["summary"]["extra_body"] == {"__cproxy_probe_nonexistent__": 1}


def test_test_endpoint_flags_a_gateway_error_delivered_as_200():
    """中转站把自己的报错当模型输出发回来：状态码是 200，必须标出来并给出原文。"""
    fresh()
    ctl(fake_success="池子中没有可用账号，请稍后再试")
    r = _test_model({"base_url": f"{MOCK}/v1", "api_key": "k", "model": "mock-chat"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] == 200 and d["ok"] is True
    assert d["verdict"] == "suspect", d
    assert "池子" in d["suspect"] or "没有可用" in d["suspect"], d["suspect"]
    assert "池子中没有可用账号" in d["content"], "模型输出要原样给用户看"
    assert "网关" in d["advice"]


def test_test_endpoint_flags_an_html_error_page():
    fresh()
    ctl(html_success=True)
    d = _test_model({"base_url": f"{MOCK}/v1", "api_key": "k", "model": "mock-chat"}).json()
    assert d["ok"] is False and d["verdict"] == "failed"
    assert "不是 JSON" in d["note"]
    assert "Cloudflare" in d["raw"]


def test_save_models_refuses_a_fake_success_only_with_the_warning():
    """可疑但 200：不拦保存（判断权在用户），但响应里必须带警告和原文。"""
    fresh()
    ctl(fake_success="upstream error: no available channel")
    d = _models()
    r = _save_models(_draft_from(d))
    assert r.status_code == 200, r.text
    assert r.json()["warning"], r.json()
    assert any(c["verdict"] == "suspect" for c in r.json()["checks"])
    assert any("no available channel" in c["content"] for c in r.json()["checks"])


def test_dialect_detection_does_not_look_at_the_url():
    """只看上游认不认，不看端点长什么样。"""
    fresh()
    ctl(strict_body=True)          # 严格网关：只认白名单里的字段
    d = _test_model({"base_url": f"{MOCK}/v1", "api_key": "k", "model": "mock-chat",
                     "detect_dialect": True}).json()
    assert d["mode"] == "dialect"
    names = [r["name"] for r in d["results"]]
    assert "baseline" in names and "thinking=disabled" in names
    # 这个假上游四种关思考的写法都不认
    assert d["dialect"] == "unknown", d["notes"]
    assert d["max_tokens_field"] == "max_tokens"


def test_dialect_detection_finds_max_completion_tokens_only_upstream():
    fresh()
    ctl(reject_unknown_max_tokens=True)
    d = _test_model({"base_url": f"{MOCK}/v1", "api_key": "k", "model": "mock-chat",
                     "detect_dialect": True}).json()
    base = next(r for r in d["results"] if r["name"] == "baseline")
    max_tokens = next(r for r in d["results"] if r["name"] == "max_tokens")
    max_completion = next(
        r for r in d["results"] if r["name"] == "max_completion_tokens")
    assert base["ok"] is True
    assert "max_tokens" not in base["request_body"]
    assert "max_completion_tokens" not in base["request_body"]
    assert max_tokens["ok"] is False
    assert max_tokens["request_body"]["max_tokens"] == 16
    assert "max_completion_tokens" not in max_tokens["request_body"]
    assert max_completion["ok"] is True
    assert max_completion["request_body"]["max_completion_tokens"] == 16
    assert "max_tokens" not in max_completion["request_body"]
    assert d["max_tokens_field"] == "max_completion_tokens"
    assert "探测中止" not in " ".join(d["notes"]), d["notes"]


def test_dialect_detection_reports_permissive_gateways():
    fresh()
    ctl(strict_body=False)         # 宽松网关：什么都收
    d = _test_model({"base_url": f"{MOCK}/v1", "api_key": "k", "model": "mock-chat",
                     "detect_dialect": True}).json()
    assert d["dialect"] == "permissive", d
    assert any("不校验未知字段" in n or "不等于" in n for n in d["notes"]), d["notes"]
    assert d["thinking_off"] is None
    assert d["suggested_extra_body"] is None
    assert d["thinking_off_evidence"] is None
    assert any("不自动填写" in n for n in d["notes"]), d["notes"]


def test_dialect_detection_autofills_the_value_that_really_disables_reasoning():
    fresh()
    ctl(strict_body=False,
        reasoning_for_values={"reasoning_effort": ["minimal"]})
    d = _test_model({"base_url": f"{MOCK}/v1", "api_key": "k", "model": "mock-chat",
                     "detect_dialect": True}).json()
    minimal = next(r for r in d["results"] if r["name"] == "reasoning_effort=minimal")
    none = next(r for r in d["results"] if r["name"] == "reasoning_effort=none")
    assert minimal["ok"] and minimal["has_reasoning"]
    assert none["ok"] and not none["has_reasoning"]
    assert d["dialect"] == "permissive"
    assert d["thinking_off"] == {"reasoning_effort": "none"}
    assert d["suggested_extra_body"] == {"reasoning_effort": "none"}
    assert d["thinking_off_evidence"] == "response_difference"
    assert any("实际输出差异" in n and "关闭思考参数生效" in n for n in d["notes"])


def test_probe_result_contains_readable_fields_and_nested_raw_payload_data():
    fresh()
    d = _test_model({"base_url": f"{MOCK}/v1", "api_key": "k", "model": "mock-chat",
                     "extra_body": {"temperature": 0.3}}).json()
    assert d["input"] == "回答一个字：好"
    assert d["request_body"]["temperature"] == 0.3
    assert "messages" not in d["request_body"]
    assert d["request_payload"]["messages"][0]["content"] == d["input"]
    assert d["raw"] and d["content"]


def test_save_validation_uses_each_endpoint_extra_body():
    fresh(fallback=FALLBACK)
    d = _models()
    body = _draft_from(d)
    body["providers"][0]["extra_body"] = {"temperature": 0.31, "seed": 11}
    body["summary"]["extra_body"] = {"temperature": 0.21, "top_p": 0.81}
    body["fallback"]["extra_body"] = {"temperature": 0.11, "seed": 22}
    r = _save_models(body)
    assert r.status_code == 200, r.text
    checks = {c["label"]: c for c in r.json()["checks"]}
    assert checks["provider:mm"]["request_body"]["temperature"] == 0.31
    assert checks["provider:mm"]["request_body"]["seed"] == 11
    assert checks["summary"]["request_body"]["temperature"] == 0.21
    assert checks["summary"]["request_body"]["top_p"] == 0.81
    assert checks["fallback"]["request_body"]["temperature"] == 0.11
    assert checks["fallback"]["request_body"]["seed"] == 22


def test_summary_uses_the_configured_max_tokens_field():
    fresh()
    d = _models()
    body = _draft_from(d)
    body["summary"]["max_tokens_field"] = "max_completion_tokens"
    body["force"] = True
    assert _save_models(body).status_code == 200
    assert post("mm", convo(30)).status_code == 200
    s = summary_calls()[0]
    assert "max_completion_tokens" in s["extra"], s["extra"]
    assert "max_tokens" not in s["extra"], "两个字段名不能同时发过去"


def test_save_models_validates_names_and_protected_keys():
    fresh()
    d = _models()
    bad = _draft_from(d)
    bad["providers"][0]["name"] = "有中文的名字"
    assert _save_models(bad).status_code == 400

    bad = _draft_from(d)
    bad["providers"][0]["extra_body"] = {"messages": [{"role": "user", "content": "x"}]}
    r = _save_models(bad)
    assert r.status_code == 400 and "messages" in r.json()["error"]["message"]

    bad = _draft_from(d)
    bad["fallback"] = {"enabled": True, "base_url": "", "model": "", "api_key": ""}
    r = _save_models(bad)
    assert r.status_code == 400 and "完整填写" in r.json()["error"]["message"]


def test_add_a_provider_from_the_console():
    fresh()
    d = _models()
    body = _draft_from(d)
    body["providers"].append({"name": "brand-new", "base_url": f"{MOCK}/v1",
                              "api_key": "sk-new", "multimodal": True,
                              "test_model": "mock-chat"})
    r = _save_models(body)
    assert r.status_code == 200, r.text
    assert "brand-new" in r.json()["providers"]
    assert post("brand-new", convo(2)).status_code == 200
    assert chat_calls()[-1]["headers"]["authorization"] == "Bearer sk-new"
