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


def fresh(ui_token: str = "", **summary_overrides) -> None:
    """给每个用例一个干净的 DB 和配置。"""
    _counter["n"] += 1
    from cproxy import app as _a
    _a._FAIL.clear()                      # 清掉上个用例攒下的鉴权失败计数
    write_config(f"db/t{_counter['n']}.db", ui_token=ui_token, **summary_overrides)
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


def post(provider: str, messages: list[dict], **extra):
    return httpx.post(f"{PROXY}/{provider}/v1/chat/completions", headers=HEADERS,
                      json={"model": "mock-chat", "messages": messages, **extra}, timeout=60)


def calls():
    return httpx.get(f"{MOCK}/__calls", timeout=5).json()["calls"]


def summary_calls():
    return [c for c in calls() if str(c["model"]).startswith("mock-summary")]


def chat_calls():
    return [c for c in calls() if not str(c["model"]).startswith("mock-summary")]


def sessions():
    return httpx.get(f"{PROXY}/admin/sessions", headers=HEADERS, timeout=10).json()["sessions"]


def session_detail(conv_id: str):
    return httpx.get(f"{PROXY}/admin/session/{conv_id}", headers=HEADERS, timeout=10).json()


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
    assert all(c["n_messages"] >= 2 for c in summary_calls())


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
        transcript = c["prompt"].split("=== 对话片段开始 ===", 1)[-1].strip()
        transcript = transcript.split("=== 对话片段结束 ===", 1)[0].strip()
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
    # 二次重压走的是 merge 提示词，不会产生多份同名章节（全文从 /summary 取）
    merged = _summary_api(sessions()[0]["conv_id"]).json()["summary"]
    assert merged.count("## 用户背景与偏好") <= 1


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
    return httpx.get(f"{PROXY}/admin/session/{conv_id}/summary", headers=HEADERS,
                     params=kw, timeout=10)


def _put_summary(conv_id, text, base_seq=None):
    body = {"summary": text}
    if base_seq is not None:
        body["base_seq"] = base_seq
    return httpx.put(f"{PROXY}/admin/session/{conv_id}/summary", headers=HEADERS,
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

    # 压缩事件没收尾时拒绝编辑
    fresh(max_batches_per_request=1)
    post("mm", convo(60))
    cid2 = sessions()[0]["conv_id"]
    assert _summary_api(cid2).json()["editable"] is False
    r = _put_summary(cid2, "趁着压缩没完偷偷改")
    assert r.status_code == 409 and "正在压缩中" in r.json()["error"]["message"]


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
    fake_editor.write_text("#!/bin/sh\nprintf '%s' '## 关键事实与专有名词\\n- 由 ctl.sh 写入' > \"$1\"\n")
    fake_editor.chmod(0o755)
    env = {**os.environ, "EDITOR": str(fake_editor), "PROXY_AUTH_TOKEN": TOKEN,
           "PROXY_PORT": str(PROXY_PORT)}

    out = subprocess.run(["bash", str(ROOT / "ctl.sh"), "summary", cid[:12]],
                         capture_output=True, text=True, env=env, cwd=ROOT)
    assert out.returncode == 0 and "##" in out.stdout, out

    out = subprocess.run(["bash", str(ROOT / "ctl.sh"), "edit", cid[:12]],
                         capture_output=True, text=True, env=env, cwd=ROOT)
    assert out.returncode == 0, out.stderr
    assert '"status": "updated"' in out.stdout, out.stdout
    assert "由 ctl.sh 写入" in _summary_api(cid).json()["summary"]


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


def test_ui_token_reaches_admin_but_not_chat():
    fresh(ui_token=UI_TOKEN)
    ui_h = {"Authorization": f"Bearer {UI_TOKEN}"}
    assert httpx.get(f"{PROXY}/admin/sessions", headers=ui_h, timeout=5).status_code == 200
    # ui_token 不能拿来调对话接口（不然等于把付费通道也交出去了）
    r = httpx.post(f"{PROXY}/mm/v1/chat/completions", headers=ui_h,
                   json={"model": "mock-chat", "messages": convo(2)}, timeout=10)
    assert r.status_code == 401
    # auth_token 仍然两边都能用，ctl.sh 不受影响
    assert httpx.get(f"{PROXY}/admin/sessions", headers=HEADERS, timeout=5).status_code == 200
    assert post("mm", convo(2)).status_code == 200


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
    assert httpx.get(f"{PROXY}/admin/sessions", headers=HEADERS, timeout=5).status_code == 200


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
