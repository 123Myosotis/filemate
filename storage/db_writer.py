# storage/db_writer.py
"""
存储层：统一 JSON → SQLite 8 张表（与业务库共用 db/filemate.db）。
document / chapter / chunk / entity / fact / table_row / relation / export_mapping
按 content_hash 幂等写入：同 hash 重新整理时先清旧行再插入，不产生脏数据。
"""
import json  # aliases/source_chunks/row_data 以 JSON 文本落库
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "db" / "filemate.db"


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_tables():
    """建 8 张业务表（幂等）"""
    conn = _conn()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS document(
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        filename     TEXT NOT NULL,
        doc_type     TEXT NOT NULL,
        content_hash TEXT UNIQUE NOT NULL,       -- 幂等写入的依据
        processed_at TEXT,
        total_chunks INTEGER,
        failed_chunks INTEGER,
        degraded_chunks INTEGER
    );
    CREATE TABLE IF NOT EXISTS chapter(
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        document_id  INTEGER NOT NULL REFERENCES document(id),
        section_path TEXT NOT NULL,
        seq          INTEGER NOT NULL,           -- 章节顺序
        status       TEXT DEFAULT 'ok'
    );
    CREATE TABLE IF NOT EXISTS chunk(
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        document_id  INTEGER NOT NULL REFERENCES document(id),
        chapter_id   INTEGER REFERENCES chapter(id),
        chunk_id     TEXT NOT NULL,
        start_offset INTEGER,
        end_offset   INTEGER,
        content      TEXT NOT NULL,
        status       TEXT DEFAULT 'pending'
    );
    CREATE TABLE IF NOT EXISTS entity(
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        document_id   INTEGER NOT NULL REFERENCES document(id),
        name          TEXT NOT NULL,
        type          TEXT NOT NULL,
        aliases       TEXT,                      -- JSON 数组
        source_chunks TEXT,                      -- JSON 数组
        UNIQUE(document_id, name, type)
    );
    CREATE TABLE IF NOT EXISTS fact(
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        document_id INTEGER NOT NULL REFERENCES document(id),
        subject     TEXT NOT NULL,
        predicate   TEXT NOT NULL,
        object      TEXT,
        confidence  REAL,
        chunk_id    TEXT
    );
    CREATE TABLE IF NOT EXISTS table_row(
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        document_id INTEGER NOT NULL REFERENCES document(id),
        table_name  TEXT NOT NULL,
        row_data    TEXT NOT NULL                -- JSON 对象（一行的 列:值）
    );
    CREATE TABLE IF NOT EXISTS relation(
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        document_id INTEGER NOT NULL REFERENCES document(id),
        subject     TEXT NOT NULL,
        predicate   TEXT NOT NULL,
        object      TEXT
    );
    CREATE TABLE IF NOT EXISTS export_mapping(
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        document_id   INTEGER NOT NULL REFERENCES document(id),
        doc_type      TEXT NOT NULL,
        template_name TEXT NOT NULL,
        field_map     TEXT NOT NULL              -- JSON：Word 占位符 → 数据来源
    );
    """)
    conn.commit()
    conn.close()


def write_document(unified: dict) -> int:
    """
    统一 JSON → 落库。同 content_hash 覆盖旧记录（先删后插）。
    返回 document_id。
    """
    init_tables()
    info = unified["document_info"]
    sc = unified["structured_content"]

    conn = _conn()
    try:
        row = conn.execute("SELECT id FROM document WHERE content_hash=?",
                           (info["content_hash"],)).fetchone()
        if row:  # 旧记录：级联清理子表
            old_id = row[0]
            for tbl in ("chapter", "chunk", "entity", "fact", "table_row", "relation",
                        "export_mapping"):
                conn.execute(f"DELETE FROM {tbl} WHERE document_id=?", (old_id,))
            conn.execute("DELETE FROM document WHERE id=?", (old_id,))

        cur = conn.execute(
            "INSERT INTO document(filename, doc_type, content_hash, processed_at,"
            " total_chunks, failed_chunks, degraded_chunks) VALUES(?,?,?,?,?,?,?)",
            (info["filename"], info["type"], info["content_hash"], info["processed_at"],
             info["total_chunks"], info["failed_chunks"], info["degraded_chunks"]),
        )
        doc_id = cur.lastrowid

        # 章节与块
        for seq, ch in enumerate(sc.get("chapters", [])):
            ccur = conn.execute(
                "INSERT INTO chapter(document_id, section_path, seq, status) VALUES(?,?,?,?)",
                (doc_id, ch["section_path"], seq, ch.get("status", "ok")),
            )
            ch["_pk"] = ccur.lastrowid  # 临时记录章节主键，供 chunk 关联

        # 块明细（由调用方把 chunk_results 一并放进 unified["_chunk_results"]）
        for r in unified.get("_chunk_results", []):
            chapter_pk = next((ch["_pk"] for ch in sc.get("chapters", [])
                               if ch["section_path"] == r["section_path"]), None)
            conn.execute(
                "INSERT INTO chunk(document_id, chapter_id, chunk_id, start_offset,"
                " end_offset, content, status) VALUES(?,?,?,?,?,?,?)",
                (doc_id, chapter_pk, r["chunk_id"], r.get("start_offset", 0),
                 r.get("end_offset", 0), r.get("content", ""), r["status"]),
            )

        # 实体
        for e in sc.get("global_entities", []):
            conn.execute(
                "INSERT OR IGNORE INTO entity(document_id, name, type, aliases, source_chunks)"
                " VALUES(?,?,?,?,?)",
                (doc_id, e["name"], e["type"],
                 json.dumps(e.get("aliases", []), ensure_ascii=False),
                 json.dumps(e.get("source_chunks", []), ensure_ascii=False)),
            )

        # 事实 + 关系（fact 是带置信度的关系，relation 存纯三元组）
        for f in sc.get("facts", []):
            conn.execute(
                "INSERT INTO fact(document_id, subject, predicate, object, confidence, chunk_id)"
                " VALUES(?,?,?,?,?,?)",
                (doc_id, f["subject"], f["predicate"], f.get("object", ""),
                 f.get("confidence", 0.8), f.get("chunk_id", "")),
            )
            conn.execute(
                "INSERT INTO relation(document_id, subject, predicate, object) VALUES(?,?,?,?)",
                (doc_id, f["subject"], f["predicate"], f.get("object", "")),
            )

        # 表格行
        for t in sc.get("tables", []):
            for r in t.get("rows", []):
                conn.execute(
                    "INSERT INTO table_row(document_id, table_name, row_data) VALUES(?,?,?)",
                    (doc_id, t.get("name", "表格"), json.dumps(r, ensure_ascii=False)),
                )
        # 外部表格（CSV/XLSX 原表）也落 table_row：unified["_external_tables"]
        for t in unified.get("_external_tables", []):
            for r in t.get("rows", []):
                conn.execute(
                    "INSERT INTO table_row(document_id, table_name, row_data) VALUES(?,?,?)",
                    (doc_id, t.get("name", "表格"), json.dumps(r, ensure_ascii=False)),
                )

        # 导出映射（模板占位符 → 数据路径）
        conn.execute(
            "INSERT INTO export_mapping(document_id, doc_type, template_name, field_map)"
            " VALUES(?,?,?,?)",
            (doc_id, info["type"], f"{info['type']}_report",
             json.dumps({"{{title}}": "metadata.title",
                         "{{table}}": "structured_content.tables"}, ensure_ascii=False)),
        )
        conn.commit()
        return doc_id
    finally:
        # 清掉写入过程中挂在 unified 上的临时字段
        for ch in sc.get("chapters", []):
            ch.pop("_pk", None)
        unified.pop("_chunk_results", None)
        unified.pop("_external_tables", None)
        conn.close()


def list_documents() -> list:
    """已整理文档清单（新→旧）"""
    conn = _conn()
    rows = conn.execute(
        "SELECT id, filename, doc_type, processed_at, total_chunks, failed_chunks,"
        " degraded_chunks FROM document ORDER BY id DESC"
    ).fetchall()
    conn.close()
    return rows
