import sqlite3
import threading
from pathlib import Path

import numpy as np


DB_PATH = Path(__file__).resolve().parent / "arya.db"
_DB_LOCK = threading.RLock()

_MAX_MEMORY_ROWS = 10_000
_EMBED_DIM = 384
_BLOB_SIZE = _EMBED_DIM * 4          # 384 float32 = 1 536 bytes per vector
_MIN_SCORE  = 0.40                   # cosine similarity threshold for injection


# ── Schema ───────────────────────────────────────────────────────────────────

def _get_connection() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH)


def _initialize_database() -> None:
    with _DB_LOCK:
        with _get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    role      TEXT    NOT NULL CHECK(role IN ('user', 'assistant')),
                    content   TEXT    NOT NULL,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memory_vectors (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_kind TEXT    NOT NULL CHECK(source_kind IN ('message', 'note')),
                    source_id   INTEGER NOT NULL,
                    content     TEXT    NOT NULL,
                    embedding   BLOB    NOT NULL,
                    timestamp   DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_mv_timestamp "
                "ON memory_vectors (timestamp DESC)"
            )
            conn.commit()


# ── Messages ─────────────────────────────────────────────────────────────────

def save_message(role: str, content: str) -> int:
    """Insert a message and return its row-id."""
    if role not in {"user", "assistant"}:
        raise RuntimeError("Invalid role. Expected 'user' or 'assistant'.")
    if not content:
        raise RuntimeError("Message content cannot be empty.")

    with _DB_LOCK:
        with _get_connection() as conn:
            cursor = conn.execute(
                "INSERT INTO messages (role, content) VALUES (?, ?)", (role, content)
            )
            conn.commit()
            return cursor.lastrowid


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
    with _DB_LOCK:
        with _get_connection() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(query_sql, (limit,)).fetchall()

    return [{"role": row["role"], "content": row["content"]} for row in rows]


def clear_history() -> None:
    with _DB_LOCK:
        with _get_connection() as conn:
            conn.execute("DELETE FROM messages")
            conn.execute("DELETE FROM memory_vectors")
            conn.commit()


# ── Semantic memory ───────────────────────────────────────────────────────────

def save_memory_vector(
    source_kind: str,
    source_id: int,
    content: str,
    embedding: np.ndarray,
) -> None:
    """Store a vector; prune oldest entries when the table exceeds _MAX_MEMORY_ROWS."""
    blob = embedding.astype(np.float32).tobytes()
    with _DB_LOCK:
        with _get_connection() as conn:
            conn.execute(
                "INSERT INTO memory_vectors "
                "(source_kind, source_id, content, embedding) VALUES (?,?,?,?)",
                (source_kind, source_id, content[:300], blob),
            )
            conn.commit()
    _prune_memory_vectors()


def _prune_memory_vectors() -> None:
    """Delete the oldest rows when count exceeds _MAX_MEMORY_ROWS."""
    with _DB_LOCK:
        with _get_connection() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM memory_vectors"
            ).fetchone()[0]
            if count > _MAX_MEMORY_ROWS:
                excess = count - _MAX_MEMORY_ROWS
                conn.execute(
                    "DELETE FROM memory_vectors WHERE id IN "
                    "(SELECT id FROM memory_vectors ORDER BY id ASC LIMIT ?)",
                    (excess,),
                )
                conn.commit()


def semantic_search(
    query_embedding: np.ndarray,
    top_k: int = 3,
    min_score: float = _MIN_SCORE,
    exclude_content: set[str] | None = None,
) -> list[str]:
    """
    Return up to top_k content strings whose stored vector is most similar
    to query_embedding (dot-product == cosine similarity for unit-normalised vecs).
    Only returns hits with score >= min_score.
    Strings already in exclude_content are skipped (deduplication vs recent history).
    """
    with _DB_LOCK:
        with _get_connection() as conn:
            # Cap at 5 000 most recent to keep numpy overhead < 50 ms
            rows = conn.execute(
                "SELECT content, embedding FROM memory_vectors "
                "ORDER BY id DESC LIMIT 5000"
            ).fetchall()

    if not rows:
        return []

    # Filter out any blobs with unexpected size (guards against corruption)
    valid = [(c, b) for c, b in rows if len(b) == _BLOB_SIZE]
    if not valid:
        return []

    contents = [c for c, _ in valid]
    blobs    = [b for _, b in valid]

    # Stack into matrix and compute all similarities in one operation
    matrix = np.frombuffer(b"".join(blobs), dtype=np.float32).reshape(len(blobs), _EMBED_DIM)
    scores = matrix @ query_embedding          # shape (N,)

    top_indices = np.argsort(scores)[::-1]

    results: list[str] = []
    for idx in top_indices:
        if float(scores[idx]) < min_score:
            break                              # sorted descending — nothing better follows
        content = contents[idx]
        if exclude_content and content in exclude_content:
            continue
        if content not in results:
            results.append(content)
        if len(results) >= top_k:
            break

    return results


_initialize_database()
