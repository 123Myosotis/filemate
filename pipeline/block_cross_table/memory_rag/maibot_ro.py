# -*- coding: utf-8 -*-
"""
Read-only MaiBot main DB access (jargons table).

Hard rule: open with SQLite URI mode=ro; never execute INSERT/UPDATE/DELETE/DDL.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence

from pipeline.block_cross_table.memory_rag.errors import LOCUS_MAIBOT_RO, MemoryRagError

# Candidate relative filenames under a MaiBot data/instance root (dynamic discovery order).
_DEFAULT_DB_CANDIDATES: tuple[str, ...] = (
    "MaiBot.db",
    "data/MaiBot.db",
    "maibot.db",
)


@dataclass(frozen=True)
class MaiBotJargonRow:
    content: str
    meaning: str
    count: int
    is_jargon: bool | None
    is_global: bool
    session_id_dict: str


def discover_maibot_db_path(data_dir: str | Path) -> Path | None:
    """
    Resolve MaiBot.db from a configured directory or direct .db path.
    Returns None if not found (caller treats as 'no MaiBot link').
    """
    raw = Path(str(data_dir)).expanduser()
    if not str(data_dir).strip():
        return None
    if raw.is_file():
        return raw.resolve()
    if not raw.exists():
        return None
    for rel in _DEFAULT_DB_CANDIDATES:
        candidate = (raw / rel).resolve()
        if candidate.is_file():
            return candidate
    # Shallow scan for *.db containing a jargons table would require open;
    # prefer explicit names. Optional: first *.db in root.
    for child in sorted(raw.glob("*.db")):
        if child.is_file():
            return child.resolve()
    return None


def _assert_read_only_pragma(conn: sqlite3.Connection) -> None:
    # query_only is advisory; URI mode=ro is the hard gate.
    conn.execute("PRAGMA query_only = ON")


@contextmanager
def open_maibot_ro(data_dir: str | Path) -> Iterator[sqlite3.Connection | None]:
    """
    Yield a read-only connection, or None if path unset / DB missing.
    Raises MemoryRagError if path set but file unreadable.
    """
    if not str(data_dir or "").strip():
        yield None
        return
    db_path = discover_maibot_db_path(data_dir)
    if db_path is None:
        raise MemoryRagError(
            "MaiBot database not found under configured data dir",
            fail_locus=LOCUS_MAIBOT_RO,
            subreason="db_not_found",
            detail={"maibot_data_dir": str(data_dir)},
        )
    # mode=ro + immutable-ish URI; Absolute path for Windows.
    uri = db_path.as_uri() + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=10.0, check_same_thread=False)
    except sqlite3.Error as exc:
        raise MemoryRagError(
            "failed to open MaiBot DB read-only",
            fail_locus=LOCUS_MAIBOT_RO,
            subreason="open_failed",
            detail={"path": str(db_path), "error": str(exc)},
        ) from exc
    conn.row_factory = sqlite3.Row
    try:
        _assert_read_only_pragma(conn)
        yield conn
    finally:
        conn.close()


def fetch_jargon_by_content(
    conn: sqlite3.Connection,
    content: str,
) -> list[MaiBotJargonRow]:
    """Lookup MaiBot jargons by exact content (case-sensitive as stored)."""
    term = str(content or "").strip()
    if not term:
        return []
    try:
        rows = conn.execute(
            """
            SELECT content, meaning, count, is_jargon, is_global, session_id_dict
            FROM jargons
            WHERE content = ?
            """,
            (term,),
        ).fetchall()
    except sqlite3.Error as exc:
        raise MemoryRagError(
            "MaiBot jargons read failed",
            fail_locus=LOCUS_MAIBOT_RO,
            subreason="query_failed",
            detail={"error": str(exc)},
        ) from exc
    out: list[MaiBotJargonRow] = []
    for r in rows:
        out.append(
            MaiBotJargonRow(
                content=str(r["content"] or ""),
                meaning=str(r["meaning"] or ""),
                count=int(r["count"] or 0),
                is_jargon=None if r["is_jargon"] is None else bool(r["is_jargon"]),
                is_global=bool(r["is_global"]),
                session_id_dict=str(r["session_id_dict"] or "{}"),
            )
        )
    return out


def list_jargons_for_merge(
    conn: sqlite3.Connection,
    *,
    limit: int,
    contents: Sequence[str] | None = None,
) -> list[MaiBotJargonRow]:
    """Bulk read for hybrid merge (optional content filter)."""
    lim = max(1, int(limit))
    try:
        if contents:
            uniq = [str(c).strip() for c in contents if str(c).strip()]
            if not uniq:
                return []
            placeholders = ",".join("?" for _ in uniq)
            rows = conn.execute(
                f"""
                SELECT content, meaning, count, is_jargon, is_global, session_id_dict
                FROM jargons
                WHERE content IN ({placeholders})
                LIMIT ?
                """,
                (*uniq, lim),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT content, meaning, count, is_jargon, is_global, session_id_dict
                FROM jargons
                ORDER BY count DESC, id DESC
                LIMIT ?
                """,
                (lim,),
            ).fetchall()
    except sqlite3.Error as exc:
        raise MemoryRagError(
            "MaiBot jargons list failed",
            fail_locus=LOCUS_MAIBOT_RO,
            subreason="query_failed",
            detail={"error": str(exc)},
        ) from exc
    return [
        MaiBotJargonRow(
            content=str(r["content"] or ""),
            meaning=str(r["meaning"] or ""),
            count=int(r["count"] or 0),
            is_jargon=None if r["is_jargon"] is None else bool(r["is_jargon"]),
            is_global=bool(r["is_global"]),
            session_id_dict=str(r["session_id_dict"] or "{}"),
        )
        for r in rows
    ]
