import sqlite3
import threading
from pathlib import Path


DB_PATH = Path(__file__).resolve().parent / "arya.db"
_DB_LOCK = threading.RLock()


def _get_connection() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH)


def _initialize_database() -> None:
    create_table_sql = """
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
        content TEXT NOT NULL,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """
    try:
        with _DB_LOCK:
            with _get_connection() as conn:
                conn.execute(create_table_sql)
                conn.commit()
    except sqlite3.Error as exc:
        raise RuntimeError(f"Failed to initialize database: {exc}") from exc


def save_message(role: str, content: str) -> None:
    if role not in {"user", "assistant"}:
        raise RuntimeError("Invalid role. Expected 'user' or 'assistant'.")
    if not content:
        raise RuntimeError("Message content cannot be empty.")

    insert_sql = "INSERT INTO messages (role, content) VALUES (?, ?)"
    try:
        with _DB_LOCK:
            with _get_connection() as conn:
                conn.execute(insert_sql, (role, content))
                conn.commit()
    except sqlite3.Error as exc:
        raise RuntimeError(f"Failed to save message: {exc}") from exc


def get_history(limit: int = 20) -> list[dict]:
    if limit < 1:
        raise RuntimeError("History limit must be greater than zero.")

    query_sql = """
    SELECT role, content
    FROM (
        SELECT role, content, timestamp, id
        FROM messages
        ORDER BY timestamp DESC, id DESC
        LIMIT ?
    )
    ORDER BY timestamp ASC, id ASC
    """
    try:
        with _DB_LOCK:
            with _get_connection() as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(query_sql, (limit,)).fetchall()
    except sqlite3.Error as exc:
        raise RuntimeError(f"Failed to load history: {exc}") from exc

    return [{"role": row["role"], "content": row["content"]} for row in rows]


def clear_history() -> None:
    try:
        with _DB_LOCK:
            with _get_connection() as conn:
                conn.execute("DELETE FROM messages")
                conn.commit()
    except sqlite3.Error as exc:
        raise RuntimeError(f"Failed to clear history: {exc}") from exc


_initialize_database()
