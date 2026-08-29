"""会话 / checkpoint 持久化。

设计要点：

* **滑动 checkpoint 链**替代单点状态。每个 checkpoint 存「当时的累积摘要全文 + 压缩位置 +
  轮次 + 当时整条消息列表的逐条指纹数组」，几千条消息也就几十 KB。
* 每个会话保留最近 ``checkpoint_keep`` 个 checkpoint，**外加一条永久置顶的最早 checkpoint**
  （第一次触发压缩时那条），用于定位彻底失败时的兜底。
* 配额单位是「一次压缩事件」而不是「一个批次」：一次事件对应一条 ``partial`` 记录，
  期间逐批 UPDATE 同一行，事件结束才 ``sealed`` 并开新的。否则一次全量重压的几十次
  逐批落盘会把窗口里的历史 checkpoint 冲干净。
* 所有 sqlite 调用都由 :class:`Store` 的 async 方法包进 ``asyncio.to_thread``，
  不占事件循环。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import threading
import time
from typing import Any

log = logging.getLogger("proxy.store")

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    conv_id        TEXT PRIMARY KEY,
    conv_key       TEXT,
    legacy_conv_id TEXT,
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL,
    event_seq      INTEGER NOT NULL DEFAULT 0,
    indexed_upto   INTEGER NOT NULL DEFAULT 0,
    fallback_count INTEGER NOT NULL DEFAULT 0,
    last_mode      TEXT
);
CREATE INDEX IF NOT EXISTS idx_conv_key    ON conversations(conv_key);
CREATE INDEX IF NOT EXISTS idx_conv_legacy ON conversations(legacy_conv_id);

CREATE TABLE IF NOT EXISTS checkpoints (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conv_id         TEXT NOT NULL,
    seq             INTEGER NOT NULL,
    status          TEXT NOT NULL,          -- partial | sealed
    kind            TEXT NOT NULL,          -- incremental | recompress | fallback | migrated
    pinned          INTEGER NOT NULL DEFAULT 0,
    summary         TEXT NOT NULL DEFAULT '',
    compressed_upto INTEGER NOT NULL DEFAULT 0,
    round_upto      INTEGER NOT NULL DEFAULT 0,
    total_rounds    INTEGER NOT NULL DEFAULT 0,
    msg_count       INTEGER NOT NULL DEFAULT 0,
    signature       TEXT,                   -- JSON 数组；旧库迁移来的为 NULL
    legacy_fp       TEXT,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ckpt_conv ON checkpoints(conv_id, seq DESC);

-- 锚点倒排：只存合格消息（≥20 字）的指纹，用于宽松会话匹配
CREATE TABLE IF NOT EXISTS fp_index (
    fp      TEXT NOT NULL,
    conv_id TEXT NOT NULL,
    PRIMARY KEY (fp, conv_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_fp_conv ON fp_index(conv_id);

CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);

-- 每个会话只留**最新一份**请求快照（覆盖写），供页面画时间轴。
-- 只存结构与预览，不存原文；默认不开，见 observability.capture_timeline。
CREATE TABLE IF NOT EXISTS timelines (
    conv_id    TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    updated_at REAL NOT NULL
) WITHOUT ROWID;
"""


def _row_to_ckpt(row: sqlite3.Row) -> dict[str, Any]:
    """指纹数组按需解析：一次请求通常只会用到最新那条 checkpoint 的 signature，
    几千条消息的 JSON 数组每条都解一遍纯属浪费。"""
    d = dict(row)
    d["signature_json"] = d.pop("signature", None)
    return d


def checkpoint_signature(ck: dict[str, Any]) -> list[str] | None:
    if "signature" not in ck:
        raw = ck.get("signature_json")
        ck["signature"] = json.loads(raw) if raw else None
    return ck["signature"]


class Store:
    """线程安全的同步实现 + async 包装。"""

    def __init__(self, path: str | None):
        self.path = path
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self.migrated_count = 0
        if path:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            self._connect()

    # ---------- 连接与建表 ----------
    def _connect(self) -> None:
        conn = sqlite3.connect(self.path, timeout=15, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=15000")
        conn.executescript(SCHEMA)
        conn.commit()
        self._conn = conn

    @property
    def enabled(self) -> bool:
        return self._conn is not None

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # ---------- 同步实现 ----------
    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            assert self._conn is not None
            return self._conn.execute(sql, args).fetchall()

    def _x(self, sql: str, args: tuple = ()) -> None:
        with self._lock:
            assert self._conn is not None
            self._conn.execute(sql, args)
            self._conn.commit()

    def _find_by_key_sync(self, conv_key: str) -> str | None:
        rows = self._q("SELECT conv_id FROM conversations WHERE conv_key = ? "
                       "ORDER BY updated_at DESC LIMIT 1", (conv_key,))
        return rows[0]["conv_id"] if rows else None

    def _find_by_legacy_sync(self, legacy_id: str) -> str | None:
        rows = self._q("SELECT conv_id FROM conversations WHERE legacy_conv_id = ? "
                       "ORDER BY updated_at DESC LIMIT 1", (legacy_id,))
        return rows[0]["conv_id"] if rows else None

    def _match_anchors_sync(self, anchors: list[str]) -> list[tuple[str, int, float]]:
        if not anchors:
            return []
        marks = ",".join("?" * len(anchors))
        rows = self._q(
            f"SELECT f.conv_id AS conv_id, COUNT(DISTINCT f.fp) AS score, c.updated_at AS updated_at "
            f"FROM fp_index f JOIN conversations c ON c.conv_id = f.conv_id "
            f"WHERE f.fp IN ({marks}) GROUP BY f.conv_id "
            f"ORDER BY score DESC, c.updated_at DESC", tuple(anchors))
        return [(r["conv_id"], r["score"], r["updated_at"]) for r in rows]

    def _load_checkpoints_sync(self, conv_id: str, limit: int) -> list[dict]:
        rows = self._q("SELECT * FROM checkpoints WHERE conv_id = ? ORDER BY seq DESC, id DESC LIMIT ?",
                       (conv_id, limit))
        return [_row_to_ckpt(r) for r in rows]

    def _load_pinned_sync(self, conv_id: str) -> dict | None:
        rows = self._q("SELECT * FROM checkpoints WHERE conv_id = ? AND pinned = 1 "
                       "ORDER BY seq ASC LIMIT 1", (conv_id,))
        return _row_to_ckpt(rows[0]) if rows else None

    def _get_conv_sync(self, conv_id: str) -> dict | None:
        rows = self._q("SELECT * FROM conversations WHERE conv_id = ?", (conv_id,))
        return dict(rows[0]) if rows else None

    def _create_conv_sync(self, conv_id: str, conv_key: str, legacy_id: str | None) -> None:
        now = time.time()
        self._x("INSERT OR IGNORE INTO conversations "
                "(conv_id, conv_key, legacy_conv_id, created_at, updated_at) VALUES (?,?,?,?,?)",
                (conv_id, conv_key, legacy_id, now, now))

    def _touch_conv_sync(self, conv_id: str, conv_key: str | None, mode: str | None) -> None:
        with self._lock:
            assert self._conn is not None
            self._conn.execute(
                "UPDATE conversations SET updated_at = ?, "
                "conv_key = COALESCE(?, conv_key), last_mode = COALESCE(?, last_mode) "
                "WHERE conv_id = ?", (time.time(), conv_key, mode, conv_id))
            self._conn.commit()

    def _open_event_sync(self, conv_id: str, kind: str, summary: str, upto: int,
                         round_upto: int, total_rounds: int, msg_count: int,
                         signature: list[str] | None) -> dict:
        """开启一次压缩事件：分配 seq 并写入一条 partial checkpoint。"""
        now = time.time()
        with self._lock:
            assert self._conn is not None
            cur = self._conn.execute(
                "SELECT event_seq FROM conversations WHERE conv_id = ?", (conv_id,)).fetchone()
            seq = (cur["event_seq"] if cur else 0) + 1
            has_pinned = self._conn.execute(
                "SELECT 1 FROM checkpoints WHERE conv_id = ? AND pinned = 1 LIMIT 1",
                (conv_id,)).fetchone() is not None
            cid = self._conn.execute(
                "INSERT INTO checkpoints (conv_id, seq, status, kind, pinned, summary, "
                "compressed_upto, round_upto, total_rounds, msg_count, signature, created_at, updated_at) "
                "VALUES (?,?,'partial',?,?,?,?,?,?,?,?,?,?)",
                (conv_id, seq, kind, 0 if has_pinned else 1, summary, upto, round_upto,
                 total_rounds, msg_count,
                 json.dumps(signature, ensure_ascii=False) if signature is not None else None,
                 now, now)).lastrowid
            self._conn.execute("UPDATE conversations SET event_seq = ?, updated_at = ? WHERE conv_id = ?",
                               (seq, now, conv_id))
            self._conn.commit()
        return {"id": cid, "seq": seq, "pinned": 0 if has_pinned else 1}

    def _resume_event_sync(self, ckpt_id: int, upto: int, round_upto: int, total_rounds: int,
                           msg_count: int, signature: list[str] | None) -> None:
        """续压一条 partial 记录：把它的坐标系刷新到本次请求的消息列表上。

        上次事件中断后消息列表可能已经增删过，存着的下标/指纹数组是旧坐标，
        续压前必须先对齐过来，否则后面逐批落盘写进去的下标会和 signature 对不上。
        """
        now = time.time()
        with self._lock:
            assert self._conn is not None
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute(
                    "UPDATE checkpoints SET compressed_upto=?, round_upto=?, total_rounds=?, "
                    "msg_count=?, signature=COALESCE(?, signature), updated_at=? WHERE id=?",
                    (upto, round_upto, total_rounds, msg_count,
                     json.dumps(signature, ensure_ascii=False) if signature is not None else None,
                     now, ckpt_id))
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def _flush_sync(self, ckpt_id: int, summary: str, upto: int, round_upto: int) -> None:
        """逐批落盘：摘要与压缩位置必须在**同一个事务**里更新。

        否则崩在两条语句中间会出现"下标推进了但摘要没写"，那段原文就永久丢了。
        """
        now = time.time()
        with self._lock:
            assert self._conn is not None
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute(
                    "UPDATE checkpoints SET summary = ?, compressed_upto = ?, round_upto = ?, "
                    "updated_at = ? WHERE id = ?", (summary, upto, round_upto, now, ckpt_id))
                self._conn.execute(
                    "UPDATE conversations SET updated_at = ? WHERE conv_id = "
                    "(SELECT conv_id FROM checkpoints WHERE id = ?)", (now, ckpt_id))
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def _seal_sync(self, ckpt_id: int, signature: list[str] | None,
                   msg_count: int, total_rounds: int, keep: int) -> None:
        now = time.time()
        with self._lock:
            assert self._conn is not None
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute(
                    "UPDATE checkpoints SET status='sealed', updated_at=?, msg_count=?, total_rounds=?, "
                    "signature = COALESCE(?, signature) WHERE id = ?",
                    (now, msg_count, total_rounds,
                     json.dumps(signature, ensure_ascii=False) if signature is not None else None,
                     ckpt_id))
                row = self._conn.execute("SELECT conv_id FROM checkpoints WHERE id = ?",
                                         (ckpt_id,)).fetchone()
                if row:
                    self._prune(row["conv_id"], keep)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def _get_meta_sync(self, k: str) -> str | None:
        rows = self._q("SELECT v FROM meta WHERE k = ?", (k,))
        return rows[0]["v"] if rows else None

    def _set_meta_sync(self, k: str, v: str) -> None:
        self._x("INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)", (k, v))

    def _save_timeline_sync(self, conv_id: str, payload: str) -> None:
        self._x("INSERT OR REPLACE INTO timelines (conv_id, payload, updated_at) VALUES (?,?,?)",
                (conv_id, payload, time.time()))

    def _load_timeline_sync(self, conv_id: str) -> dict | None:
        rows = self._q("SELECT payload, updated_at FROM timelines WHERE conv_id = ?", (conv_id,))
        if not rows:
            return None
        d = json.loads(rows[0]["payload"])
        d["updated_at"] = rows[0]["updated_at"]
        return d

    def _prune(self, conv_id: str, keep: int) -> None:
        """保留：置顶的最早一条 + 最近 keep 个压缩事件（按事件计，不按批次）。调用方负责事务。"""
        assert self._conn is not None
        self._conn.execute(
            "DELETE FROM checkpoints WHERE conv_id = ? AND pinned = 0 AND seq NOT IN "
            "(SELECT seq FROM checkpoints WHERE conv_id = ? ORDER BY seq DESC LIMIT ?)",
            (conv_id, conv_id, keep))

    def _latest_ckpt_sync(self, conv_id: str) -> dict | None:
        rows = self._q("SELECT * FROM checkpoints WHERE conv_id = ? ORDER BY seq DESC, id DESC LIMIT 1",
                       (conv_id,))
        return _row_to_ckpt(rows[0]) if rows else None

    def _open_event_ckpt_sync(self, conv_id: str) -> dict | None:
        rows = self._q("SELECT * FROM checkpoints WHERE conv_id = ? AND status = 'partial' "
                       "ORDER BY seq DESC LIMIT 1", (conv_id,))
        return _row_to_ckpt(rows[0]) if rows else None

    def _add_manual_sync(self, conv_id: str, base_id: int, summary: str, keep: int) -> int:
        """把手工编辑的摘要写成**新的** checkpoint，不覆盖原来那条。

        位置信息（compressed_upto / round_upto / signature / …）整套从被编辑的那条复制过来——
        用户只改了摘要文字，历史对齐关系没有变。原checkpoint 留在链上，改坏了可以回退。
        """
        now = time.time()
        with self._lock:
            assert self._conn is not None
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                cur = self._conn.execute(
                    "SELECT event_seq FROM conversations WHERE conv_id = ?", (conv_id,)).fetchone()
                seq = (cur["event_seq"] if cur else 0) + 1
                self._conn.execute(
                    "INSERT INTO checkpoints (conv_id, seq, status, kind, pinned, summary, "
                    "compressed_upto, round_upto, total_rounds, msg_count, signature, legacy_fp, "
                    "created_at, updated_at) "
                    "SELECT conv_id, ?, 'sealed', 'manual', 0, ?, compressed_upto, round_upto, "
                    "total_rounds, msg_count, signature, legacy_fp, ?, ? "
                    "FROM checkpoints WHERE id = ?", (seq, summary, now, now, base_id))
                self._conn.execute(
                    "UPDATE conversations SET event_seq = ?, updated_at = ?, last_mode = 'manual' "
                    "WHERE conv_id = ?", (seq, now, conv_id))
                self._prune(conv_id, keep)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return seq

    def _index_fps_sync(self, conv_id: str, fps: list[str], indexed_upto: int) -> None:
        if not fps:
            return
        with self._lock:
            assert self._conn is not None
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.executemany("INSERT OR IGNORE INTO fp_index (fp, conv_id) VALUES (?,?)",
                                       [(fp, conv_id) for fp in fps])
                self._conn.execute("UPDATE conversations SET indexed_upto = ? WHERE conv_id = ?",
                                   (indexed_upto, conv_id))
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def _bump_fallback_sync(self, conv_id: str) -> int:
        with self._lock:
            assert self._conn is not None
            self._conn.execute(
                "UPDATE conversations SET fallback_count = fallback_count + 1 WHERE conv_id = ?",
                (conv_id,))
            self._conn.commit()
            row = self._conn.execute("SELECT fallback_count FROM conversations WHERE conv_id = ?",
                                     (conv_id,)).fetchone()
            return int(row["fallback_count"]) if row else 0

    def _stats_sync(self) -> dict:
        rows = self._q("SELECT COUNT(*) AS n, COALESCE(SUM(fallback_count),0) AS fb FROM conversations")
        ck = self._q("SELECT COUNT(*) AS n, SUM(status='partial') AS p FROM checkpoints")
        return {"conversations": rows[0]["n"], "fallback_activations": rows[0]["fb"],
                "checkpoints": ck[0]["n"], "open_events": ck[0]["p"] or 0}

    def _list_sync(self, limit: int) -> list[dict]:
        rows = self._q(
            "SELECT c.conv_id, c.updated_at, c.fallback_count, c.last_mode, c.event_seq, "
            "(SELECT compressed_upto FROM checkpoints k WHERE k.conv_id=c.conv_id "
            " ORDER BY k.seq DESC, k.id DESC LIMIT 1) AS compressed_upto, "
            "(SELECT round_upto FROM checkpoints k WHERE k.conv_id=c.conv_id "
            " ORDER BY k.seq DESC, k.id DESC LIMIT 1) AS round_upto, "
            "(SELECT status FROM checkpoints k WHERE k.conv_id=c.conv_id "
            " ORDER BY k.seq DESC, k.id DESC LIMIT 1) AS status, "
            "(SELECT length(summary) FROM checkpoints k WHERE k.conv_id=c.conv_id "
            " ORDER BY k.seq DESC, k.id DESC LIMIT 1) AS summary_chars, "
            "(SELECT COUNT(*) FROM checkpoints k WHERE k.conv_id=c.conv_id) AS checkpoints "
            "FROM conversations c ORDER BY c.updated_at DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    def _delete_sync(self, conv_ids: list[str]) -> None:
        with self._lock:
            assert self._conn is not None
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                for cid in conv_ids:
                    self._conn.execute("DELETE FROM checkpoints WHERE conv_id = ?", (cid,))
                    self._conn.execute("DELETE FROM fp_index WHERE conv_id = ?", (cid,))
                    self._conn.execute("DELETE FROM timelines WHERE conv_id = ?", (cid,))
                    self._conn.execute("DELETE FROM conversations WHERE conv_id = ?", (cid,))
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def _match_prefix_sync(self, prefix: str) -> list[str]:
        rows = self._q("SELECT conv_id FROM conversations WHERE conv_id LIKE ?", (prefix + "%",))
        return [r["conv_id"] for r in rows]

    # ---------- 旧库迁移 ----------
    def migrate_legacy(self) -> int:
        """把旧的 ``sessions`` 表导入 checkpoint 结构。幂等，跑过一次就跳过。"""
        if not self.enabled:
            return 0
        with self._lock:
            assert self._conn is not None
            done = self._conn.execute("SELECT v FROM meta WHERE k='legacy_migrated'").fetchone()
            if done:
                return 0
            has_old = self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sessions'").fetchone()
            if not has_old:
                self._conn.execute("INSERT OR REPLACE INTO meta (k,v) VALUES ('legacy_migrated','1')")
                self._conn.commit()
                return 0
            rows = self._conn.execute(
                "SELECT conv_id, summary, compressed_upto, boundary_fp, ts FROM sessions").fetchall()
            n = 0
            now = time.time()
            for r in rows:
                legacy_id = r["conv_id"]
                summary = r["summary"] or ""
                upto = int(r["compressed_upto"] or 0)
                ts = float(r["ts"] or now)
                if not summary or upto <= 0:
                    continue
                conv_id = "legacy-" + legacy_id[:24]
                self._conn.execute(
                    "INSERT OR IGNORE INTO conversations "
                    "(conv_id, conv_key, legacy_conv_id, created_at, updated_at, event_seq, last_mode) "
                    "VALUES (?,NULL,?,?,?,1,'migrated')", (conv_id, legacy_id, ts, ts))
                # 迁移来的 checkpoint 没有指纹数组，标记 pinned 兼作兜底锚
                self._conn.execute(
                    "INSERT INTO checkpoints (conv_id, seq, status, kind, pinned, summary, "
                    "compressed_upto, round_upto, total_rounds, msg_count, signature, legacy_fp, "
                    "created_at, updated_at) VALUES (?,1,'sealed','migrated',1,?,?,0,0,?,NULL,?,?,?)",
                    (conv_id, summary, upto, upto, r["boundary_fp"], ts, ts))
                n += 1
            self._conn.execute("INSERT OR REPLACE INTO meta (k,v) VALUES ('legacy_migrated','1')")
            self._conn.commit()
        self.migrated_count = n
        if n:
            log.info("旧 sessions 表迁移完成：导入 %d 个会话为 migrated checkpoint", n)
        return n

    # ---------- async 包装 ----------
    async def _call(self, fn, *args):
        if not self.enabled:
            return None
        return await asyncio.to_thread(fn, *args)

    async def find_by_key(self, conv_key: str) -> str | None:
        return await self._call(self._find_by_key_sync, conv_key)

    async def find_by_legacy(self, legacy_id: str) -> str | None:
        return await self._call(self._find_by_legacy_sync, legacy_id)

    async def match_anchors(self, anchors: list[str]) -> list[tuple[str, int, float]]:
        return await self._call(self._match_anchors_sync, anchors) or []

    async def load_checkpoints(self, conv_id: str, limit: int = 12) -> list[dict]:
        return await self._call(self._load_checkpoints_sync, conv_id, limit) or []

    async def load_pinned(self, conv_id: str) -> dict | None:
        return await self._call(self._load_pinned_sync, conv_id)

    async def get_conversation(self, conv_id: str) -> dict | None:
        return await self._call(self._get_conv_sync, conv_id)

    async def create_conversation(self, conv_id: str, conv_key: str, legacy_id: str | None) -> None:
        await self._call(self._create_conv_sync, conv_id, conv_key, legacy_id)

    async def touch(self, conv_id: str, conv_key: str | None = None, mode: str | None = None) -> None:
        await self._call(self._touch_conv_sync, conv_id, conv_key, mode)

    async def open_event(self, conv_id: str, kind: str, summary: str, upto: int, round_upto: int,
                         total_rounds: int, msg_count: int, signature: list[str] | None) -> dict | None:
        return await self._call(self._open_event_sync, conv_id, kind, summary, upto,
                                round_upto, total_rounds, msg_count, signature)

    async def resume_event(self, ckpt_id: int, upto: int, round_upto: int, total_rounds: int,
                           msg_count: int, signature: list[str] | None) -> None:
        await self._call(self._resume_event_sync, ckpt_id, upto, round_upto,
                         total_rounds, msg_count, signature)

    async def flush(self, ckpt_id: int, summary: str, upto: int, round_upto: int) -> None:
        await self._call(self._flush_sync, ckpt_id, summary, upto, round_upto)

    async def seal(self, ckpt_id: int, signature: list[str] | None, msg_count: int,
                   total_rounds: int, keep: int) -> None:
        await self._call(self._seal_sync, ckpt_id, signature, msg_count, total_rounds, keep)

    async def latest_checkpoint(self, conv_id: str) -> dict | None:
        return await self._call(self._latest_ckpt_sync, conv_id)

    async def open_event_checkpoint(self, conv_id: str) -> dict | None:
        return await self._call(self._open_event_ckpt_sync, conv_id)

    async def add_manual_checkpoint(self, conv_id: str, base_id: int, summary: str,
                                    keep: int) -> int | None:
        return await self._call(self._add_manual_sync, conv_id, base_id, summary, keep)

    async def index_fps(self, conv_id: str, fps: list[str], indexed_upto: int) -> None:
        await self._call(self._index_fps_sync, conv_id, fps, indexed_upto)

    async def bump_fallback(self, conv_id: str) -> int:
        return await self._call(self._bump_fallback_sync, conv_id) or 0

    async def get_meta(self, k: str) -> str | None:
        return await self._call(self._get_meta_sync, k)

    async def set_meta(self, k: str, v: str) -> None:
        await self._call(self._set_meta_sync, k, v)

    async def save_timeline(self, conv_id: str, payload: str) -> None:
        await self._call(self._save_timeline_sync, conv_id, payload)

    async def load_timeline(self, conv_id: str) -> dict | None:
        return await self._call(self._load_timeline_sync, conv_id)

    async def stats(self) -> dict:
        return await self._call(self._stats_sync) or {}

    async def list_conversations(self, limit: int = 100) -> list[dict]:
        return await self._call(self._list_sync, limit) or []

    async def delete(self, conv_ids: list[str]) -> None:
        await self._call(self._delete_sync, conv_ids)

    async def match_prefix(self, prefix: str) -> list[str]:
        return await self._call(self._match_prefix_sync, prefix) or []


# ===== 模块级单例 =====
_STORE: Store | None = None


def init(path: str | None) -> Store:
    global _STORE
    if _STORE is not None:
        if _STORE.path == path:
            return _STORE
        _STORE.close()
    _STORE = Store(path)
    if _STORE.enabled:
        _STORE.migrate_legacy()
        log.info("会话持久化已启用：%s", path)
    else:
        log.warning("会话持久化未启用（persist_db 为空），重启会丢失全部压缩状态")
    return _STORE


def get() -> Store:
    assert _STORE is not None, "store 未初始化"
    return _STORE
