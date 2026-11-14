import asyncio
import os
import sqlite3
import threading
from pathlib import Path


# ARYA_DB_PATH points tests (or a second profile) at another database.
DB_PATH = Path(os.getenv("ARYA_DB_PATH") or Path(__file__).resolve().parents[1] / "memory" / "arya.db")
_DB_LOCK = threading.RLock()


def _get_connection() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH)


def _initialize_notes_table() -> None:
    create_table_sql = """
    CREATE TABLE IF NOT EXISTS research_notes (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        content   TEXT    NOT NULL,
        tag       TEXT    DEFAULT 'general',
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """
    try:
        with _DB_LOCK:
            with _get_connection() as conn:
                conn.execute(create_table_sql)
                conn.commit()
    except sqlite3.Error as exc:
        raise RuntimeError(f"Failed to initialize notes table: {exc}") from exc


def _format_rows(rows: list[sqlite3.Row]) -> str:
    if not rows:
        return ""
    return "\n".join(
        f"[{row['timestamp']}] ({row['tag']}): {row['content']}"
        for row in rows
    )


# ── Embedding helper (fire-and-forget) ───────────────────────────────────────

async def _embed_and_store_note(note_id: int, content: str, tag: str) -> None:
    """Runs in the background after save_note returns."""
    try:
        from arya.core.embedder import embed
        from arya.memory.database import save_memory_vector
        labelled = f"[note:{tag}]: {content}"
        vec = await embed(labelled)
        save_memory_vector("note", note_id, labelled, vec)
    except Exception as exc:
        print(f"Embedder: failed to store note vector (id={note_id}): {exc}")


# ── CRUD ─────────────────────────────────────────────────────────────────────

async def save_note(content: str, tag: str = "general") -> str:
    insert_sql = "INSERT INTO research_notes (content, tag) VALUES (?, ?)"
    try:
        with _DB_LOCK:
            with _get_connection() as conn:
                cursor = conn.execute(insert_sql, (content, tag or "general"))
                conn.commit()
                note_id = cursor.lastrowid
    except sqlite3.Error as exc:
        raise RuntimeError(f"Failed to save note: {exc}") from exc

    # Embed in background — doesn't block the response
    asyncio.create_task(_embed_and_store_note(note_id, content, tag or "general"))

    return f"Note saved: {content[:50]}..."


async def get_notes(tag: str = None, limit: int = 10) -> str:
    if limit < 1:
        return "No notes found."

    if tag is None:
        query_sql = """
        SELECT id, content, tag, timestamp
        FROM research_notes
        ORDER BY timestamp DESC, id DESC
        LIMIT ?
        """
        params = (limit,)
    else:
        query_sql = """
        SELECT id, content, tag, timestamp
        FROM research_notes
        WHERE tag = ?
        ORDER BY timestamp DESC, id DESC
        LIMIT ?
        """
        params = (tag, limit)

    try:
        with _DB_LOCK:
            with _get_connection() as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(query_sql, params).fetchall()
    except sqlite3.Error as exc:
        raise RuntimeError(f"Failed to load notes: {exc}") from exc

    if not rows:
        return "No notes found."
    return _format_rows(rows)


async def delete_note(note_id: int) -> str:
    try:
        with _DB_LOCK:
            with _get_connection() as conn:
                cursor = conn.execute(
                    "DELETE FROM research_notes WHERE id = ?", (note_id,)
                )
                conn.commit()
    except sqlite3.Error as exc:
        raise RuntimeError(f"Failed to delete note: {exc}") from exc

    if cursor.rowcount == 0:
        return "Note not found."
    return f"Note {note_id} deleted."


async def search_notes(keyword: str) -> str:
    query_sql = """
    SELECT id, content, tag, timestamp
    FROM research_notes
    WHERE content LIKE ?
    ORDER BY timestamp DESC, id DESC
    """
    try:
        with _DB_LOCK:
            with _get_connection() as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(query_sql, (f"%{keyword}%",)).fetchall()
    except sqlite3.Error as exc:
        raise RuntimeError(f"Failed to search notes: {exc}") from exc

    if not rows:
        return f"No notes matching: {keyword}"
    return _format_rows(rows)


_initialize_notes_table()
