"""知识库存储层：SQLite（内置）+ FTS5 全文索引。

分三层存放，职责清晰：
- documents：抓取过的网页（去重键为 URL），记录来源、抓取时间、内容指纹；
- chunks   ：网页切块后的原文片段，是回答的「证据」；
- facts    ：由模型从证据中抽取的结构化知识条目，带置信度、版本与冲突状态，
             是「知识库」真正被维护的部分。

FTS5 不可用时自动降级为 LIKE 扫描（功能保留、速度下降），保证不会直接崩。
"""

from __future__ import annotations

import array
import json
import logging
import math
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import chunk as chunkmod
from . import curation
from . import paths
from . import versioning
from .chunk import simhash64

# 2：facts 表加 version / effective_from（版本化与时效，见 app/core/versioning.py）
# 3：facts 表加 date_kind（区分「生效于」还是「发布于」，避免把公告发布日说成生效日）
SCHEMA_VERSION = 3

# LIKE 兜底查询的分词上限。
# SQLite 的表达式树深度上限默认是 1000，而 LIKE 兜底是把每个分词拼成一项
# `text LIKE ?` 再用 OR 串起来——N 个分词就是 N 层深的 OR 链。中文提问按 bigram
# 切词，API 允许的 4000 字上限能切出 3900+ 个分词，于是
# `sqlite3.OperationalError: Expression tree is too large (maximum depth 1000)`
# 直接从 search_chunks / _like_facts 里抛出来，把 /chat 打成 500。
# 修法：只把前 _LIKE_MAX_TOKENS 个分词放进 SQL（长提问的关键词几乎总在前面），
# 并给两处兜底查询包上 except sqlite3.OperationalError（多一层保险）。
# 只影响 SQL 粗筛：_score_fact/_score_chunk 仍按完整 tokens 算覆盖率，
# 所以「长问题被截断」只体现在召回面，不体现在打分口径上。
_LIKE_MAX_TOKENS = 200

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
-- 本库是「读多写少 + 每次入库顺序写」的本地单机库：
-- synchronous=NORMAL 在 WAL 下依然安全（断电最多丢最近一个事务），写入快很多；
-- temp_store/cache_size 让 FTS 排序与 SimHash 扫描尽量在内存里做。
PRAGMA synchronous=NORMAL;
PRAGMA temp_store=MEMORY;
PRAGMA cache_size=-16000;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS documents (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    url           TEXT NOT NULL UNIQUE,
    title         TEXT NOT NULL DEFAULT '',
    site          TEXT NOT NULL DEFAULT '',
    source_type   TEXT NOT NULL DEFAULT 'community',
    lang          TEXT NOT NULL DEFAULT 'zh',
    content_hash  TEXT NOT NULL DEFAULT '',
    published_at  TEXT NOT NULL DEFAULT '',
    fetched_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    http_status   INTEGER NOT NULL DEFAULT 0,
    meta          TEXT NOT NULL DEFAULT '{}',
    status        TEXT NOT NULL DEFAULT 'active'
);
CREATE INDEX IF NOT EXISTS idx_documents_source ON documents(source_type, status);
CREATE INDEX IF NOT EXISTS idx_documents_updated ON documents(updated_at DESC);

CREATE TABLE IF NOT EXISTS chunks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id     INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    ord        INTEGER NOT NULL DEFAULT 0,
    text       TEXT NOT NULL,
    tokens     TEXT NOT NULL DEFAULT '',
    char_len   INTEGER NOT NULL DEFAULT 0,
    simhash    INTEGER NOT NULL DEFAULT 0,
    embedding  BLOB,
    status     TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);

CREATE TABLE IF NOT EXISTS facts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    topic         TEXT NOT NULL DEFAULT '',
    title         TEXT NOT NULL DEFAULT '',
    answer        TEXT NOT NULL DEFAULT '',
    tags          TEXT NOT NULL DEFAULT '',
    source_url    TEXT NOT NULL DEFAULT '',
    source_type   TEXT NOT NULL DEFAULT '',
    confidence    REAL NOT NULL DEFAULT 0.6,
    extraction    TEXT NOT NULL DEFAULT '',
    version       TEXT NOT NULL DEFAULT '',
    effective_from TEXT NOT NULL DEFAULT '',
    date_kind     TEXT NOT NULL DEFAULT '',
    simhash       INTEGER NOT NULL DEFAULT 0,
    sim_bucket    INTEGER NOT NULL DEFAULT -1,
    supersedes_id INTEGER,
    status        TEXT NOT NULL DEFAULT 'active',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_facts_topic ON facts(topic, status);
CREATE INDEX IF NOT EXISTS idx_facts_status ON facts(status);
-- idx_facts_title / idx_facts_bucket 不写在这里：它们依赖的列在**老库**里可能还不存在，
-- CREATE INDEX 会在 _migrate() 补列之前就报 "no such column"（真实踩过）。
-- 建索引放到 _migrate() 之后，见 _ensure_indexes()。

CREATE TABLE IF NOT EXISTS update_logs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger       TEXT NOT NULL DEFAULT 'manual',
    topic         TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'running',
    started_at    TEXT NOT NULL,
    finished_at   TEXT NOT NULL DEFAULT '',
    pages_fetched INTEGER NOT NULL DEFAULT 0,
    pages_failed  INTEGER NOT NULL DEFAULT 0,
    facts_added   INTEGER NOT NULL DEFAULT 0,
    facts_updated INTEGER NOT NULL DEFAULT 0,
    chunks_added  INTEGER NOT NULL DEFAULT 0,
    message       TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS topic_queue (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    topic      TEXT NOT NULL UNIQUE,
    origin     TEXT NOT NULL DEFAULT 'user',
    priority   INTEGER NOT NULL DEFAULT 5,
    hits       INTEGER NOT NULL DEFAULT 0,
    last_run   TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
"""

_FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(tokens);
CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(tokens);
"""


def _version_sort_key(value: Any) -> Tuple[int, ...]:
    """把版本号转成可比较的数字元组（实现已上移到 `versioning.version_sort_key`）。

    保留这个名字是因为它在本模块内用了很久，且 store / consistency 两处都必须
    用同一个实现：直接按字符串比版本会得到 `"1.10" < "1.9"`，把旧版本当新版本。
    """
    return versioning.version_sort_key(value)


def _prefer_newest_in_slot(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """同一「实体+字段」槽位出现多条（＝同一件事的不同版本）时，让版本/日期更新的排在前面。

    只调换同槽位条目之间的相对顺序，不会把任何条目挤出候选池，也不会隐藏旧版本——
    「用户问的就是 1.3 版本的活动时间」这种情况必须还能答出来，靠的是证据里的版本标签，
    而不是把旧条目删掉。没有日期/版本的条目保持原顺序（`sort` 稳定）。
    """
    positions: Dict[str, List[int]] = {}
    for index, row in enumerate(rows):
        slot = str(row.get("slot_key") or "")
        if slot:
            positions.setdefault(slot, []).append(index)
    for indexes in positions.values():
        if len(indexes) < 2:
            continue
        ranked = sorted(
            indexes,
            key=lambda i: (
                str(rows[i].get("effective_from") or ""),
                _version_sort_key(rows[i].get("version")),
            ),
            reverse=True,
        )
        if ranked == indexes:
            continue
        # 先取出再写回：就地赋值会让后面的 source 读到已被覆盖的行。
        picked = [rows[i] for i in ranked]
        for target, row in zip(indexes, picked):
            rows[target] = row
    return rows


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# SQLite 的 INTEGER 是有符号 64 位，而 SimHash 是无符号 64 位。
# 直接写入高位置 1 的指纹会抛 OverflowError，故在存储边界做一次两补码转换。
_MASK64 = (1 << 64) - 1
_SIGN64 = 1 << 63


def _to_db_int(value: int) -> int:
    value &= _MASK64
    return value - (1 << 64) if value >= _SIGN64 else value


def _from_db_int(value: Any) -> int:
    try:
        return int(value) & _MASK64
    except (TypeError, ValueError):
        return 0


def simhash_bucket(value: int) -> int:
    """simhash 高 16 位：近邻预筛用的桶号（见 find_similar_facts）。"""
    return (int(value) >> 48) & 0xFFFF


# 历史库回填时每批处理的行数（见 KnowledgeBase._backfill_sim_bucket）
_BACKFILL_BATCH = 20000


def embed_to_blob(vector: Sequence[float]) -> bytes:
    return array.array("f", [float(v) for v in vector]).tobytes()


def blob_to_embed(blob: Optional[bytes]) -> List[float]:
    if not blob:
        return []
    buf = array.array("f")
    try:
        buf.frombytes(blob)
    except Exception:
        return []
    return list(buf)


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = norm_a = norm_b = 0.0
    for x, y in zip(a, b):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a <= 0 or norm_b <= 0:
        return 0.0
    return float(dot / math.sqrt(norm_a * norm_b))


class KnowledgeBase:
    """线程安全的 SQLite 封装（FastAPI 多线程访问同一实例）。"""

    def __init__(self, db_file: Optional[Path] = None) -> None:
        self.path = Path(db_file) if db_file else paths.db_path()
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self.fts_enabled = False
        try:
            self._init_schema()
        except BaseException:
            # 初始化中途失败（最典型的是库里写着比本程序更高的 schema_version）时，
            # 构造函数没有返回值，调用方拿不到这个实例，也就没人能关掉这条连接：
            # 文件句柄会一直挂到进程退出，在 Windows 上还会锁住 .db——连删掉它所在
            # 的目录都会静默失败（自检残留的 .quality_check_round3 就是这样留下的）。
            self._conn.close()
            raise

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._check_schema_version()
            self._migrate()
            self._ensure_indexes()
            try:
                self._conn.executescript(_FTS_SCHEMA)
                self.fts_enabled = True
            except sqlite3.OperationalError:
                self.fts_enabled = False
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )
            self._conn.commit()

    def _check_schema_version(self) -> None:
        """打开数据库前先比一次库版本，比本程序新就明确拒绝。

        过去 `meta.schema_version` 只写不读：用旧版程序打开新版知识库时不会有
        任何提示，还会照常写入并**覆盖版本戳**，于是后续版本也再察觉不到这个库
        曾经被旧程序动过。数据目录默认在 `%APPDATA%\\NTE-RAG`，用户重装/降级
        时会真的撞上。
        """
        try:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
        except sqlite3.OperationalError:
            # 连 meta 表都没有：建库过程中，交给 _migrate() 正常走下去
            return
        if row is None:
            return
        try:
            stored = int(str(row["value"]).strip() or 0)
        except (TypeError, ValueError):
            return
        if stored > SCHEMA_VERSION:
            raise RuntimeError(
                f"这个知识库是用更新版本的 NTE-RAG 建的（库结构版本 {stored}，"
                f"本程序只支持到 {SCHEMA_VERSION}）。请升级程序后再打开；"
                "如果只是想看旧数据，可以先把它复制一份到别的数据目录。"
            )

    def _migrate(self) -> None:
        """给老库补新列（`CREATE TABLE IF NOT EXISTS` 不会改已存在的表）。"""
        additions = {
            "facts": {
                # 提取方式：api / table / llm / manual。可信度派生的输入之一，
                # 也用于事后统计「多少条数值是模型摘写的」。
                "extraction": "TEXT NOT NULL DEFAULT ''",
                # 版本与生效时间：官方公告里写明的「1.3版本」「2026年8月13日」，
                # 由 versioning.py 用正则在入库时确定性地取出（空串＝没写明或认不出）。
                "version": "TEXT NOT NULL DEFAULT ''",
                "effective_from": "TEXT NOT NULL DEFAULT ''",
                # 上面这个日期是「生效日」还是「发布日」：正文里带时间语境的算
                # effective，正文没写、从链接 /YYYYMMDD/ 退回的算 published。
                # 混为一谈会输出假信息（1.3 版本 8 月 13 日生效，公告发在 8 月 8 日）。
                "date_kind": "TEXT NOT NULL DEFAULT ''",
                # simhash 高 16 位（桶）：预筛近邻用，见 find_similar_facts()。
                # 默认 -1 表示「还没算过」，migrate 时会补齐。
                "sim_bucket": "INTEGER NOT NULL DEFAULT -1",
            }
        }
        for table, columns in additions.items():
            existing = {
                row["name"]
                for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            for name, ddl in columns.items():
                if name not in existing:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
        self._backfill_sim_bucket()

    def _ensure_indexes(self) -> None:
        """补建依赖新列的索引（必须在 _migrate() 补完列之后执行）。"""
        for ddl in (
            # find_facts_by_title / 标题精确比对走的是 title：
            # 没有这个索引时，每抽出一个候选就要扫一遍全表（入库热路径）。
            "CREATE INDEX IF NOT EXISTS idx_facts_title ON facts(title, status)",
            # find_similar_facts 的近邻预筛走 sim_bucket
            "CREATE INDEX IF NOT EXISTS idx_facts_bucket ON facts(sim_bucket, status)",
        ):
            try:
                self._conn.execute(ddl)
            except sqlite3.OperationalError:
                # 极端情况下（表结构被外部改坏）索引建不上也不该让整个数据库打不开
                pass

    def _backfill_sim_bucket(self) -> None:
        """给老库里的条目补 simhash 桶值（否则它们不会被近邻预筛选中）。

        过去只回填一批（`LIMIT 20000`）：超过两万条事实的老库里，剩下的条目会
        **永久**停在 `sim_bucket = -1`，永远进不了 `find_similar_facts` 的预筛，
        结果是静默漏合并（看起来像「没有重复」，其实是根本没查）。这里循环补完
        为止；`simhash_bucket()` 的返回值恒在 0..65535，所以每条记录只会被处理
        一次，不存在反复选中同一条的死循环。
        """
        total = 0
        while True:
            pending = self._conn.execute(
                "SELECT id, simhash FROM facts WHERE sim_bucket < 0 LIMIT ?",
                (_BACKFILL_BATCH,),
            ).fetchall()
            if not pending:
                break
            self._conn.executemany(
                "UPDATE facts SET sim_bucket=? WHERE id=?",
                [
                    (simhash_bucket(_from_db_int(row["simhash"])), int(row["id"]))
                    for row in pending
                ],
            )
            self._conn.commit()
            total += len(pending)
            if len(pending) < _BACKFILL_BATCH:
                break
        if total:
            logging.info("[store] 回填 simhash 桶：%d 条历史事实补齐了近邻预筛字段", total)

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.commit()
            finally:
                self._conn.close()

    # ------------------------------------------------------------------
    # meta
    # ------------------------------------------------------------------

    def get_meta(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # documents
    # ------------------------------------------------------------------

    def upsert_document(
        self,
        url: str,
        title: str,
        text_hash: str,
        site: str = "",
        source_type: str = "community",
        http_status: int = 200,
        published_at: str = "",
        meta: Optional[Dict[str, Any]] = None,
    ) -> Tuple[int, bool]:
        """写入/更新文档记录。返回 (doc_id, 内容是否有变化)。"""
        now = _now()
        with self._lock:
            row = self._conn.execute(
                "SELECT id, content_hash FROM documents WHERE url=?", (url,)
            ).fetchone()
            if row is None:
                cursor = self._conn.execute(
                    """INSERT INTO documents
                       (url, title, site, source_type, content_hash, published_at,
                        fetched_at, updated_at, http_status, meta, status)
                       VALUES (?,?,?,?,?,?,?,?,?,?, 'active')""",
                    (
                        url, title, site, source_type, text_hash, published_at,
                        now, now, http_status, json.dumps(meta or {}, ensure_ascii=False),
                    ),
                )
                self._conn.commit()
                return int(cursor.lastrowid), True
            doc_id = int(row["id"])
            changed = row["content_hash"] != text_hash
            self._conn.execute(
                """UPDATE documents SET title=?, site=?, source_type=?, content_hash=?,
                       published_at=?, fetched_at=?, updated_at=?, http_status=?, meta=?, status='active'
                   WHERE id=?""",
                (
                    title, site, source_type, text_hash, published_at, now, now,
                    http_status, json.dumps(meta or {}, ensure_ascii=False), doc_id,
                ),
            )
            self._conn.commit()
            return doc_id, changed

    def get_document(self, doc_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
        return dict(row) if row else None

    def list_documents(
        self, limit: int = 50, offset: int = 0, keyword: str = "", source_type: str = ""
    ) -> List[Dict[str, Any]]:
        sql = ["SELECT d.*, (SELECT COUNT(1) FROM chunks c WHERE c.doc_id=d.id) AS chunk_count FROM documents d WHERE 1=1"]
        params: List[Any] = []
        if keyword:
            sql.append("AND (d.title LIKE ? OR d.url LIKE ?)")
            params.extend([f"%{keyword}%", f"%{keyword}%"])
        if source_type:
            sql.append("AND d.source_type=?")
            params.append(source_type)
        sql.append("ORDER BY d.updated_at DESC LIMIT ? OFFSET ?")
        params.extend([int(limit), int(offset)])
        with self._lock:
            rows = self._conn.execute(" ".join(sql), params).fetchall()
        return [dict(r) for r in rows]

    def delete_document(self, doc_id: int) -> None:
        with self._lock:
            if self.fts_enabled:
                self._conn.execute(
                    "DELETE FROM chunks_fts WHERE rowid IN (SELECT id FROM chunks WHERE doc_id=?)",
                    (doc_id,),
                )
            self._conn.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
            self._conn.execute("DELETE FROM documents WHERE id=?", (doc_id,))
            self._conn.commit()

    def revoke_source(self, url: str) -> Dict[str, int]:
        """按来源 URL 软撤回它的正文、切片与条目（保留审计痕迹）。

        用于「已证伪页面」（见 app/core/curation.py）：硬删会让复核结论无处可查，
        置为 `revoked` 则检索层（只取 active/conflict）看不见它，而数据还在。
        返回各表影响的行数；调用方拿它写日志/报告。
        """
        target = curation.canonical_url(url)
        counts = {"documents": 0, "chunks": 0, "facts": 0}
        if not target:
            return counts
        now = _now()
        with self._lock:
            # URL 在库里有编码/未编码两种写法，比较前统一归一化，否则会漏。
            doc_ids = [
                int(row["id"])
                for row in self._conn.execute("SELECT id, url FROM documents").fetchall()
                if curation.canonical_url(row["url"]) == target
            ]
            for doc_id in doc_ids:
                cursor = self._conn.execute(
                    "UPDATE chunks SET status='revoked', updated_at=? WHERE doc_id=? AND status<>'revoked'",
                    (now, doc_id),
                )
                counts["chunks"] += int(cursor.rowcount or 0)
            if doc_ids:
                placeholders = ",".join("?" * len(doc_ids))
                cursor = self._conn.execute(
                    f"UPDATE documents SET status='revoked', updated_at=? WHERE id IN ({placeholders}) AND status<>'revoked'",
                    (now, *doc_ids),
                )
                counts["documents"] = int(cursor.rowcount or 0)
            for row in self._conn.execute("SELECT id, source_url FROM facts").fetchall():
                if curation.canonical_url(row["source_url"]) != target:
                    continue
                cursor = self._conn.execute(
                    "UPDATE facts SET status='revoked', updated_at=? WHERE id=? AND status<>'revoked'",
                    (now, int(row["id"])),
                )
                counts["facts"] += int(cursor.rowcount or 0)
            self._conn.commit()
        return counts

    # ------------------------------------------------------------------
    # chunks
    # ------------------------------------------------------------------

    def replace_chunks(self, doc_id: int, texts: Sequence[str], embeddings: Optional[Sequence[Sequence[float]]] = None) -> int:
        """用新切块替换该文档的旧切块。返回写入的块数。"""
        now = _now()
        with self._lock:
            if self.fts_enabled:
                self._conn.execute(
                    "DELETE FROM chunks_fts WHERE rowid IN (SELECT id FROM chunks WHERE doc_id=?)",
                    (doc_id,),
                )
            self._conn.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
            count = 0
            for index, text in enumerate(texts):
                tokens = chunkmod.tokens_to_index(chunkmod.tokenize(text))
                blob = None
                if embeddings and index < len(embeddings) and embeddings[index]:
                    blob = embed_to_blob(embeddings[index])
                cursor = self._conn.execute(
                    """INSERT INTO chunks
                       (doc_id, ord, text, tokens, char_len, simhash, embedding, status, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?, 'active', ?, ?)""",
                    (
                        doc_id, index, text, tokens, len(text), _to_db_int(simhash64(text)),
                        blob, now, now,
                    ),
                )
                if self.fts_enabled:
                    self._conn.execute(
                        "INSERT INTO chunks_fts(rowid, tokens) VALUES (?, ?)",
                        (int(cursor.lastrowid), tokens),
                    )
                count += 1
            self._conn.commit()
        return count

    def get_chunk(self, chunk_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                """SELECT c.*, d.url, d.title, d.source_type, d.site, d.updated_at AS doc_updated
                   FROM chunks c JOIN documents d ON d.id=c.doc_id WHERE c.id=?""",
                (chunk_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_chunks(self, doc_id: int) -> List[Dict[str, Any]]:
        """取某个文档的全部切片（按顺序），用于导出与诊断。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, ord, text, char_len FROM chunks WHERE doc_id=? ORDER BY ord",
                (doc_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # facts
    # ------------------------------------------------------------------

    def add_fact(
        self,
        title: str,
        answer: str,
        topic: str = "",
        tags: str = "",
        source_url: str = "",
        source_type: str = "",
        confidence: float = 0.6,
        extraction: str = "",
        version: str = "",
        effective_from: str = "",
        date_kind: str = "",
        supersedes_id: Optional[int] = None,
        status: str = "active",
    ) -> int:
        now = _now()
        fingerprint = simhash64(f"{title} {answer}")
        with self._lock:
            cursor = self._conn.execute(
                """INSERT INTO facts
                   (topic, title, answer, tags, source_url, source_type, confidence, extraction,
                    version, effective_from, date_kind, simhash, sim_bucket, supersedes_id, status,
                    created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    topic, title, answer, tags, source_url, source_type,
                    float(confidence), extraction or "",
                    str(version or ""), str(effective_from or ""), str(date_kind or ""),
                    _to_db_int(fingerprint), simhash_bucket(fingerprint),
                    supersedes_id, status, now, now,
                ),
            )
            fact_id = int(cursor.lastrowid)
            if self.fts_enabled:
                tokens = chunkmod.tokens_to_index(chunkmod.tokenize(f"{title} {topic} {tags} {answer}"))
                self._conn.execute(
                    "INSERT INTO facts_fts(rowid, tokens) VALUES (?, ?)", (fact_id, tokens)
                )
            self._conn.commit()
        return fact_id

    def update_fact(self, fact_id: int, **fields: Any) -> None:
        allowed = {"title", "answer", "topic", "tags", "source_url", "source_type",
                   "confidence", "extraction", "version", "effective_from", "date_kind", "status"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        updates["updated_at"] = _now()
        with self._lock:
            assignments = ", ".join(f"{k}=?" for k in updates)
            self._conn.execute(
                f"UPDATE facts SET {assignments} WHERE id=?", (*updates.values(), fact_id)
            )
            if self.fts_enabled:
                row = self._conn.execute(
                    "SELECT topic, title, tags, answer FROM facts WHERE id=?", (fact_id,)
                ).fetchone()
                if row:
                    # 必须先 tokenize 再 tokens_to_index：tokens_to_index 只负责把「已经切好的
                    # token 列表」拼成空格串，直接喂原文会把整段文字按单字切开——那样这条
                    # 条目就再也匹配不上任何双字查询了（实测：改过一次的条目从检索结果里消失）。
                    tokens = chunkmod.tokens_to_index(
                        chunkmod.tokenize(
                            f"{row['title']} {row['topic']} {row['tags']} {row['answer']}"
                        )
                    )
                    self._conn.execute("DELETE FROM facts_fts WHERE rowid=?", (fact_id,))
                    self._conn.execute(
                        "INSERT INTO facts_fts(rowid, tokens) VALUES (?, ?)", (fact_id, tokens)
                    )
            if "title" in updates or "answer" in updates:
                # 指纹随内容变化：桶不同步的话，这条条目就再也参与不了近邻预筛
                row = self._conn.execute(
                    "SELECT title, answer FROM facts WHERE id=?", (fact_id,)
                ).fetchone()
                if row:
                    fingerprint = simhash64(f"{row['title']} {row['answer']}")
                    self._conn.execute(
                        "UPDATE facts SET simhash=?, sim_bucket=? WHERE id=?",
                        (_to_db_int(fingerprint), simhash_bucket(fingerprint), fact_id),
                    )
            self._conn.commit()

    def supersede_fact(self, old_id: int, new_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE facts SET status='superseded', updated_at=? WHERE id=?", (_now(), old_id)
            )
            self._conn.execute("UPDATE facts SET supersedes_id=? WHERE id=?", (old_id, new_id))
            self._conn.commit()

    def get_fact(self, fact_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM facts WHERE id=?", (fact_id,)).fetchone()
        return dict(row) if row else None

    def find_similar_facts(self, text: str, limit: int = 5, threshold: int = 4) -> List[Dict[str, Any]]:
        """按 SimHash 近邻找出可能重复/冲突的既有条目。

        过去是 LIMIT 5000 把整表拉进 Python 逐条 popcount，而它位于入库热循环里，
        总代价是 O(新增条数 × 全表条数)。改用 simhash 高 16 位当桶预筛：
        汉明距离 <= 4 的两条记录，高 16 位最多差 4 位，所以除了自身桶还要看邻居桶。
        为控制 SQL 参数数量，只枚举高 16 位翻转 <= 2 位的组合（137 个桶）；
        高 16 位翻转 3-4 位却仍然距离 <= 4 的情况存在，但极罕见，
        最终判定仍用完整汉明距离，所以只会漏掉极少数候选、不会误报。

        已知取舍（cap 参数）：137 个桶一次性取回后按 updated_at 倒序截断在 cap 行，
        所以某个桶里积累超过 cap 行时，更老的行不会再进入候选，重复项可能漏检。
        与「误报」不同，这里漏的是「没合并成一条」——代价是知识库里多一条近似重复，
        而不是把不该合并的条目合并掉（后者会丢信息，更糟）。所以保留这个上限。
        要彻底解决得改成按桶分页遍历（多次小查询），当前数据规模（几百到几千条）用不到。
        """
        target = simhash64(text)
        pool = self._simhash_bucket_candidates(target)
        scored: List[Tuple[int, Dict[str, Any]]] = []
        for row in pool:
            distance = bin(_from_db_int(row["simhash"]) ^ target).count("1")
            if distance <= threshold:
                scored.append((distance, dict(row)))
        scored.sort(key=lambda item: item[0])
        return [item[1] for item in scored[:limit]]

    def _simhash_bucket_candidates(self, target: int, cap: int = 5000) -> List[sqlite3.Row]:
        """取出 simhash 高 16 位落在目标桶或邻居桶的候选行。"""
        top = (target >> 48) & 0xFFFF
        buckets = {top}
        for a in range(16):
            buckets.add(top ^ (1 << a))
        for a in range(16):
            for b in range(a + 1, 16):
                buckets.add(top ^ (1 << a) ^ (1 << b))
        placeholders = ",".join("?" * len(buckets))
        with self._lock:
            rows = self._conn.execute(
                f"""SELECT * FROM facts
                    WHERE status IN ('active','conflict') AND sim_bucket IN ({placeholders})
                    ORDER BY updated_at DESC LIMIT ?""",
                (*sorted(buckets), int(cap)),
            ).fetchall()
        return rows

    def find_facts_by_title(self, title: str, limit: int = 5) -> List[Dict[str, Any]]:
        """按标题精确取出既有条目（近似重复合并用）。

        SimHash 对短文本太紧（实测「动作角色扮演游戏」vs「开放世界动作角色扮演游戏」
        距离 >4，抓不住），所以同一标题下的近似重复要单独查一次再逐条比对。
        """
        if not title:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM facts WHERE status IN ('active','conflict') AND title=? "
                "ORDER BY confidence DESC, updated_at DESC LIMIT ?",
                (title, int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_facts(
        self, limit: int = 50, offset: int = 0, keyword: str = "", status: str = "", topic: str = ""
    ) -> List[Dict[str, Any]]:
        sql = ["SELECT * FROM facts WHERE 1=1"]
        params: List[Any] = []
        if topic:
            sql.append("AND topic=?")
            params.append(topic)
        if keyword:
            sql.append("AND (title LIKE ? OR answer LIKE ? OR tags LIKE ?)")
            params.extend([f"%{keyword}%"] * 3)
        if status:
            sql.append("AND status=?")
            params.append(status)
        sql.append("ORDER BY updated_at DESC LIMIT ? OFFSET ?")
        params.extend([int(limit), int(offset)])
        with self._lock:
            rows = self._conn.execute(" ".join(sql), params).fetchall()
        return [dict(r) for r in rows]

    def sample_facts(
        self, limit: int = 60, min_title: int = 4, max_title: int = 30, status: str = "active"
    ) -> List[Dict[str, Any]]:
        """随机抽一批条目（用于「推荐问题」这类需要每次都不一样的场景）。

        过滤掉过短/过长的标题：过短的（如「薄荷」）套不出像样的问句，
        过长的多半是从表格里拼出来的句子，直接当问题很别扭。
        """
        sql = ["SELECT * FROM facts WHERE 1=1"]
        params: List[Any] = []
        if status:
            sql.append("AND status=?")
            params.append(status)
        sql.append("AND length(title) BETWEEN ? AND ?")
        params.extend([int(min_title), int(max_title)])
        sql.append("ORDER BY RANDOM() LIMIT ?")
        params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(" ".join(sql), params).fetchall()
        return [dict(r) for r in rows]

    def delete_fact(self, fact_id: int) -> None:
        with self._lock:
            if self.fts_enabled:
                self._conn.execute("DELETE FROM facts_fts WHERE rowid=?", (fact_id,))
            self._conn.execute("DELETE FROM facts WHERE id=?", (fact_id,))
            self._conn.commit()

    def delete_facts_by_topic(self, topic: str) -> int:
        """删掉某个主题下的全部条目，返回删除条数。

        给自检用的兜底清扫：自检条目正常路径末尾会按 id 删掉，但进程中途崩溃
        （正好是自检要覆盖的场景）就会在用户知识库里留下一条不会被清理的残留条目。
        开跑之前先按主题清一遍，重复运行也不会累积。
        """
        topic = (topic or "").strip()
        if not topic:
            return 0
        with self._lock:
            rows = self._conn.execute("SELECT id FROM facts WHERE topic=?", (topic,)).fetchall()
            ids = [int(row["id"]) for row in rows]
            for fact_id in ids:
                if self.fts_enabled:
                    self._conn.execute("DELETE FROM facts_fts WHERE rowid=?", (fact_id,))
                self._conn.execute("DELETE FROM facts WHERE id=?", (fact_id,))
            if ids:
                self._conn.commit()
        return len(ids)

    # ------------------------------------------------------------------
    # 检索
    # ------------------------------------------------------------------

    def search_facts(self, query: str, limit: int = 8) -> List[Dict[str, Any]]:
        tokens = chunkmod.tokenize_query(query)
        if not tokens:
            return []
        # 先多取一批候选再自己排序：bm25 只反映词频，无法表达「问题点名了哪个实体」；
        # 而事实条数很少（top_k//2 通常是 4 条），窗口太窄会让该进证据的条目根本进不来。
        pool = max(int(limit), 1) * 4
        with self._lock:
            if self.fts_enabled:
                expression = chunkmod.build_fts_query(tokens)
                try:
                    rows = self._conn.execute(
                        """SELECT f.*, bm25(facts_fts) AS bm
                           FROM facts_fts JOIN facts f ON f.id = facts_fts.rowid
                           WHERE facts_fts MATCH ? AND f.status IN ('active','conflict')
                           ORDER BY bm LIMIT ?""",
                        (expression, int(pool)),
                    ).fetchall()
                except sqlite3.OperationalError as error:
                    # 不再静默吞掉：畸形 FTS 表达式以前在日志里完全看不见
                    logging.warning("[store] 事实 FTS 查询失败，退回 LIKE 兜底：%s", error)
                    rows = self._like_facts(tokens, pool)
                if not rows:
                    # FTS 正常返回零行时也要兜底（分块路径一直是这么做的）：
                    # 分词差异会让分块检索有结果而事实检索为空，助手于是误判
                    # 「资料不足」转去联网抓取——尽管库里本来就有匹配的事实。
                    rows = self._like_facts(tokens, pool)
            else:
                rows = self._like_facts(tokens, pool)
        query_norm = chunkmod.normalize(query)
        scored = [self._score_fact(dict(r), tokens, query_norm) for r in rows]
        # 排序依据（从强到弱）：
        # ① entity_hit——问题里点名了这个条目所属的实体（标题 `X·字段` 的 X 出现在问题里），
        #    这种条目几乎一定是用户要的那条，必须排在前面；
        # ② rank_raw——覆盖率/标题命中/可信度的复合分（**不封顶**）。不能用封顶的 `rank`：
        #    两条同时封到 1.0 就再也区分不出可信度高低，可信度等于没参与排序；
        # ③ bm25——FTS 词频相关度，只用来打破完全相同的 rank_raw（越小越相关）。
        # 实测：「弧盘「我们。」的效果描述里提到了哪些数值？」这类提问下，
        # 只按 bm25 排序时前 12 条全是其它弧盘的「·效果」，真正的「我们。」·描述根本进不来。
        scored.sort(
            key=lambda item: (
                -float(item.get("entity_hit") or 0.0),
                -float(item.get("rank_raw") or 0.0),
                round(float(item.get("bm") or 0.0), 4),
            )
        )
        # 排完序再做一次「同槽位取新版」的调整（见 _prefer_newest_in_slot）：
        # 这件事必须放在候选池（4×limit）上做，否则新版条目可能刚好落在窗口外。
        return _prefer_newest_in_slot(scored)[: int(limit)]

    def _like_facts(self, tokens: Sequence[str], limit: int) -> List[sqlite3.Row]:
        """LIKE 兜底召回（无 FTS5 / FTS 报错时走这里）。

        分词要先截断：见 _LIKE_MAX_TOKENS 的说明——超长提问会撞上 SQLite 的
        表达式树深度上限。异常也一并兜住，兜底查询再失败时退化成「没有候选」，
        而不是把 500 抛给用户。
        """
        safe_tokens = list(tokens)[:_LIKE_MAX_TOKENS]
        if not safe_tokens:
            return []
        clause = " OR ".join(["title LIKE ? OR answer LIKE ? OR tags LIKE ?"] * len(safe_tokens))
        params: List[Any] = []
        for token in safe_tokens:
            params.extend([f"%{token}%"] * 3)
        try:
            return self._conn.execute(
                f"""SELECT *, 0.0 AS bm FROM facts
                    WHERE status IN ('active','conflict') AND ({clause})
                    ORDER BY confidence DESC, updated_at DESC LIMIT ?""",
                (*params, int(limit)),
            ).fetchall()
        except sqlite3.OperationalError as error:
            logging.warning("[store] LIKE 兜底召回也失败了，本次视为没有候选：%s", error)
            return []

    @staticmethod
    def entity_of(title: Any) -> str:
        """条目标题里的实体名：`「我们。」·描述` → `我们。`（去字段后缀与引号）。"""
        head = str(title or "").split("·")[0]
        head = re.sub(r"[「」『』“”\"'《》【】()（）\s]", "", head)
        return chunkmod.normalize(head)

    @classmethod
    def _score_fact(
        cls, row: Dict[str, Any], tokens: Sequence[str], query_norm: str = ""
    ) -> Dict[str, Any]:
        title = chunkmod.normalize(str(row.get("title") or ""))
        haystack = chunkmod.normalize(
            f"{row.get('title','')} {row.get('tags','')} {row.get('answer','')}"
        )
        hit = sum(1 for t in tokens if t in haystack)
        coverage = hit / max(1, len(tokens))
        title_hit = sum(1 for t in tokens if t in title)
        # 分母取 min(len(tokens), 3)：短问题（「薄荷的声优是谁」）不该因为标题只命中一个词就被判低分
        title_coverage = min(1.0, title_hit / max(1, min(len(tokens), 3)))
        entity = cls.entity_of(row.get("title"))
        # 实体必须在问题里**原样出现**才算命中：只按词命中会把「异环」「角色」这类泛词算进来
        entity_hit = 1.0 if len(entity) >= 2 and entity in query_norm else 0.0
        row["coverage"] = round(coverage, 4)
        # relevance 的算法**保持不变**：它同时被「本地资料够不够」的阈值
        # （answer.web_trigger_score）使用，改权重会让联网回退的触发频率整体漂移。
        row["relevance"] = round(coverage * 0.75 + float(row.get("confidence") or 0) * 0.25, 4)
        row["title_coverage"] = round(title_coverage, 4)
        row["entity_hit"] = entity_hit
        # 同槽位键：同一实体的同一字段。版本新旧只在同槽位内比较（见 _prefer_newest_in_slot）。
        row["slot_key"] = versioning.slot_key(entity, row.get("title"))
        # 排序用未封顶的原始分：封顶会把「可信度高」的信息抹掉（两条都是 1.0 时无法再区分，
        # 可信度就不再参与排序了）。对外展示用封顶后的 rank。
        row["rank_raw"] = round(row["relevance"] + 0.35 * title_coverage + 0.5 * entity_hit, 6)
        row["rank"] = round(min(1.0, row["rank_raw"]), 4)
        return row

    def search_chunks(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        tokens = chunkmod.tokenize_query(query)
        if not tokens:
            return []
        # 候选池要比最终返回条数大：bm25 的排序在 SQL 里，但 relevance 还叠加了
        # 覆盖率/标题命中/来源加成，只在 LIMIT 内的几行里比较等于白算——
        # 一篇最佳片段排在第 11 位的文档会永远看不到。多捞几倍再在 Python 里重排。
        pool = max(1, int(limit)) * 4
        with self._lock:
            rows: List[sqlite3.Row] = []
            if self.fts_enabled:
                expression = chunkmod.build_fts_query(tokens)
                try:
                    rows = self._conn.execute(
                        """SELECT c.id, c.doc_id, c.text, c.ord, c.simhash, c.embedding,
                                  d.url, d.title, d.source_type, d.site, d.updated_at AS doc_updated,
                                  bm25(chunks_fts) AS bm
                           FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid
                           JOIN documents d ON d.id = c.doc_id
                           WHERE chunks_fts MATCH ? AND c.status='active' AND d.status='active'
                           ORDER BY bm LIMIT ?""",
                        (expression, pool),
                    ).fetchall()
                except sqlite3.OperationalError:
                    rows = []
            if not rows:
                # LIKE 兜底分支过去没有 ORDER BY：返回顺序是 rowid 顺序，
                # 与相关度无关，重排后仍要有一个稳定的基础顺序。
                # 分词上限见 _LIKE_MAX_TOKENS；查询自身失败时退化成空候选池。
                safe_tokens = tokens[:_LIKE_MAX_TOKENS]
                clause = " OR ".join(["c.text LIKE ?"] * len(safe_tokens))
                try:
                    rows = self._conn.execute(
                        f"""SELECT c.id, c.doc_id, c.text, c.ord, c.simhash, c.embedding,
                                   d.url, d.title, d.source_type, d.site, d.updated_at AS doc_updated,
                                   0.0 AS bm
                            FROM chunks c JOIN documents d ON d.id=c.doc_id
                            WHERE c.status='active' AND d.status='active' AND ({clause})
                            ORDER BY c.ord LIMIT ?""",
                        (*[f"%{t}%" for t in safe_tokens], pool),
                    ).fetchall()
                except sqlite3.OperationalError:
                    rows = []
        scored = [self._score_chunk(dict(r), tokens) for r in rows]
        scored.sort(key=lambda item: float(item.get("relevance") or 0.0), reverse=True)
        return scored[: int(limit)]

    @staticmethod
    def _score_chunk(row: Dict[str, Any], tokens: Sequence[str]) -> Dict[str, Any]:
        text = chunkmod.normalize(row.get("text", ""))
        title = chunkmod.normalize(row.get("title", ""))
        hits = sum(1 for t in tokens if t in text)
        title_hits = sum(1 for t in tokens if t in title)
        coverage = hits / max(1, len(tokens))
        title_bonus = min(0.2, title_hits / max(1, len(tokens)) * 0.2)
        source = row.get("source_type", "")
        source_bonus = {"official": 0.12, "wiki": 0.08, "seed": 0.06}.get(source, 0.0)
        bm = float(row.get("bm") or 0.0)
        # FTS5 的 bm25() 返回**负值，越负越相关**（SQL 里 ORDER BY bm 升序是对的）。
        # 过去这里取了 abs()，等于把符号抹掉：匹配越强、bm_score 反而越低，
        # relevance 被系统性算反。这里只取负值的绝对值，正值（无匹配）记 0 分。
        bm_score = 1.0 / (1.0 + max(0.0, -bm)) if bm else 0.0
        # bm25 用**乘法**参与，而不是加在覆盖率的后面：
        # coverage 与各种 bonus 相加很容易顶到 1.0（封顶后完全一样），
        # 再准的 bm25 也拉不开差距——「同一段文字、越相关反而分越低」的怪现象就是这么来的。
        # base 先封顶到 1.0，再乘上 [0.5, 1.0] 的 bm25 系数。
        # 系数是 1 - 0.5×bm_score：bm_score 越小代表 bm25 越负、匹配越强，
        # 所以减号不能写反（写反就又变成「越相关分越低」）。
        base = min(1.0, coverage * 0.62 + title_bonus + source_bonus)
        relevance = min(1.0, base * (1.0 - 0.5 * bm_score)) if bm else base
        row["coverage"] = round(coverage, 4)
        row["relevance"] = round(max(0.0, min(1.0, relevance)), 4)
        row.pop("embedding", None)  # 不把二进制向量丢给上层
        return row

    def semantic_chunks(self, query_vector: Sequence[float], limit: int = 10) -> List[Dict[str, Any]]:
        """可选的向量召回：对已存向量的片段做暴力余弦（知识库规模有限，够用）。

        未接线：当前**没有任何调用方**（rag.retrieve 只走 FTS/LIKE 关键词召回）。
        这是刻意保留的已实现能力——config 的 `embedding` 段、`kb.use_embeddings`、
        llm.EmbeddingClient 与 POST /api/config/test-embedding 都在位，只是检索路径
        还没接上。要启用它，需要：让 ingest 在 `kb.use_embeddings` 为真时算向量并传给
        `replace_chunks(..., embeddings=...)`，再在 `retrieve()` 里用 query 向量调用本方法
        与关键词结果融合。若要删除，先确认确实不打算启用向量召回。
        """
        if not query_vector:
            return []
        with self._lock:
            rows = self._conn.execute(
                """SELECT c.id, c.doc_id, c.text, c.ord, c.embedding, d.url, d.title,
                          d.source_type, d.site, d.updated_at AS doc_updated
                   FROM chunks c JOIN documents d ON d.id=c.doc_id
                   WHERE c.embedding IS NOT NULL AND c.status='active' AND d.status='active'"""
            ).fetchall()
        scored: List[Tuple[float, Dict[str, Any]]] = []
        for row in rows:
            data = dict(row)
            score = cosine(query_vector, blob_to_embed(data.get("embedding")))
            if score > 0:
                data.pop("embedding", None)
                data["relevance"] = round(score, 4)
                data["coverage"] = round(score, 4)
                scored.append((score, data))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [item[1] for item in scored[:limit]]

    # ------------------------------------------------------------------
    # 更新日志与主题队列
    # ------------------------------------------------------------------

    def start_update_log(self, trigger: str, topic: str = "") -> int:
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO update_logs(trigger, topic, status, started_at) VALUES (?,?, 'running', ?)",
                (trigger, topic, _now()),
            )
            self._conn.commit()
        return int(cursor.lastrowid)

    def finish_update_log(self, log_id: int, status: str, message: str = "", **counters: int) -> None:
        fields = {
            "pages_fetched": int(counters.get("pages_fetched", 0)),
            "pages_failed": int(counters.get("pages_failed", 0)),
            "facts_added": int(counters.get("facts_added", 0)),
            "facts_updated": int(counters.get("facts_updated", 0)),
            "chunks_added": int(counters.get("chunks_added", 0)),
        }
        with self._lock:
            self._conn.execute(
                """UPDATE update_logs SET status=?, finished_at=?, message=?,
                       pages_fetched=?, pages_failed=?, facts_added=?, facts_updated=?, chunks_added=?
                   WHERE id=?""",
                (status, _now(), message[:2000], *fields.values(), log_id),
            )
            self._conn.commit()

    def list_update_logs(self, limit: int = 30) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM update_logs ORDER BY id DESC LIMIT ?", (int(limit),)
            ).fetchall()
        return [dict(r) for r in rows]

    def enqueue_topic(self, topic: str, origin: str = "auto", priority: int = 5) -> None:
        topic = topic.strip()
        if not topic:
            return
        with self._lock:
            self._conn.execute(
                """INSERT INTO topic_queue(topic, origin, priority, created_at)
                   VALUES (?,?,?,?)
                   ON CONFLICT(topic) DO UPDATE SET priority=MIN(priority, excluded.priority)""",
                (topic, origin, int(priority), _now()),
            )
            self._conn.commit()

    def dequeue_topics(self, limit: int = 5) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM topic_queue
                   WHERE last_run='' OR last_run IS NULL
                   ORDER BY priority ASC, hits DESC, id ASC LIMIT ?""",
                (int(limit),),
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_topic_run(self, topic: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE topic_queue SET last_run=?, hits=hits+1 WHERE topic=?", (_now(), topic)
            )
            self._conn.commit()

    def list_topics(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM topic_queue ORDER BY priority ASC, id ASC"
            ).fetchall()
        return [dict(r) for r in rows]

    def remove_topic(self, topic: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM topic_queue WHERE topic=?", (topic,))
            self._conn.commit()

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            documents = self._conn.execute("SELECT COUNT(1) FROM documents WHERE status='active'").fetchone()[0]
            chunks = self._conn.execute("SELECT COUNT(1) FROM chunks WHERE status='active'").fetchone()[0]
            facts = self._conn.execute("SELECT COUNT(1) FROM facts WHERE status='active'").fetchone()[0]
            conflicts = self._conn.execute("SELECT COUNT(1) FROM facts WHERE status='conflict'").fetchone()[0]
            superseded = self._conn.execute("SELECT COUNT(1) FROM facts WHERE status='superseded'").fetchone()[0]
            last_log = self._conn.execute(
                "SELECT * FROM update_logs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            by_source = self._conn.execute(
                "SELECT source_type, COUNT(1) AS n FROM documents WHERE status='active' GROUP BY source_type"
            ).fetchall()
        return {
            "documents": documents,
            "chunks": chunks,
            "facts": facts,
            "conflicts": conflicts,
            "superseded": superseded,
            "by_source": {row["source_type"]: row["n"] for row in by_source},
            "fts_enabled": self.fts_enabled,
            "db_path": str(self.path),
            "db_size_kb": round(self.path.stat().st_size / 1024, 1) if self.path.exists() else 0,
            "last_update": dict(last_log) if last_log else None,
        }
