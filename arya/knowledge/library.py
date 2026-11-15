"""
ARYA's knowledge library — your course material, searchable.

  * Indexes PDFs, Word, PowerPoint, text/Markdown files where they are —
    read-only: files are never copied, moved or modified.
  * Splits each document into passages that remember their page number.
  * Hybrid search: meaning (the same MiniLM embedder ARYA uses for memory)
    + exact keywords (SQLite FTS5 / BM25), fused by reciprocal rank.
  * Renders a source page as an image with the matching passage highlighted
    (in memory only — the PDF on disk is never written).

The index lives in %LOCALAPPDATA%\\ARYA\\knowledge.db (your course text is
private and never goes into the repo). Re-indexing is incremental: only new or
changed files are processed; files that disappeared are dropped from the index.
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

DATA_DIR = Path(os.getenv("ARYA_DATA_DIR") or Path(os.getenv("LOCALAPPDATA") or Path.home()) / "ARYA")
DB_PATH = Path(os.getenv("ARYA_KNOWLEDGE_DB") or DATA_DIR / "knowledge.db")
CORE_NOTES = Path(__file__).resolve().parent / "core_notes.md"
# What the user told ARYA was wrong ("that's wrong — ζ = 0 is undamped"); outranks everything.
CORRECTIONS = DATA_DIR / "corrections.md"
CORRECTIONS_TITLE = "Your corrections"
# Handwritten solution pages read by the cloud vision model (after asking): kept and searchable.
TRANSCRIPTIONS = DATA_DIR / "transcriptions.md"
TRANSCRIPTIONS_TITLE = "Handwritten solutions"
# Text of photos you added to ARYA and asked to keep ("add it to my library").
PHOTO_NOTES = DATA_DIR / "photo_notes.md"
PHOTO_NOTES_TITLE = "Photo notes"
EXTENSIONS = {".pdf", ".docx", ".pptx", ".txt", ".md"}
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "site-packages"}

CHUNK_CHARS = 750
CHUNK_OVERLAP = 120
MIN_CHUNK_CHARS = 60
SCANNED_PAGE_CHARS = 40          # a PDF page with less text than this is probably an image scan

_lock = threading.RLock()


def default_roots() -> list[Path]:
    """Folders to index: ARYA_KNOWLEDGE_DIRS (os.pathsep-separated) or the course folders on the Desktop."""
    env = os.getenv("ARYA_KNOWLEDGE_DIRS", "").strip()
    if env:
        return [Path(p).expanduser() for p in env.split(os.pathsep) if p.strip()]
    from arya.core.policy import _known_folder

    desktop = _known_folder("Desktop") or Path.home() / "Desktop"
    return [desktop / name for name in ("Video Lectures", "Dynamics", "Labs") if (desktop / name).exists()]


# ── Storage ───────────────────────────────────────────────────────────────────

def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _init() -> None:
    with _lock, _connect() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY, path TEXT UNIQUE, title TEXT, collection TEXT,
                kind TEXT, mtime REAL, size INTEGER, fingerprint TEXT, pages INTEGER,
                chunks INTEGER, empty_pages INTEGER, status TEXT, indexed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS chunks (
                id INTEGER PRIMARY KEY, doc_id INTEGER, page INTEGER, text TEXT, embedding BLOB
            );
            CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                text, content='chunks', content_rowid='id', tokenize='porter unicode61'
            );
        """)


_init()


# ── Text extraction ───────────────────────────────────────────────────────────

def _clean(text: str) -> str:
    text = text.replace("­", "").replace("\x00", " ")
    # Drop runs of 1–2 character lines (figure labels like "R / a / b / ϕ") — noise for the model.
    lines = text.split("\n")
    short = [len(line.strip()) <= 2 for line in lines]
    text = "\n".join(line for i, line in enumerate(lines)
                     if not (short[i] and ((i > 0 and short[i - 1]) or (i + 1 < len(lines) and short[i + 1]))))
    text = re.sub(r"-\n(?=[a-z])", "", text)            # re-join hyphenated line breaks
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _pages(path: Path) -> list[tuple[int, str]]:
    """[(page number starting at 1, text)] — "page" is a slide for PowerPoint,
    a section for Markdown, and a ~3000-character block for Word/text."""
    ext = path.suffix.lower()
    if ext in _media_extensions():
        from arya.knowledge.lectures import pages
        return pages(path)                          # a lecture recording: two-minute blocks of its transcript
    if ext == ".pdf":
        import pymupdf

        with pymupdf.open(path) as doc:
            out = []
            for i, page in enumerate(doc):
                text = _clean(page.get_text("text"))
                if len(text) < SCANNED_PAGE_CHARS and ocr_available():
                    text = _ocr_page(page) or text            # scanned page: read the printed text off the image
                out.append((i + 1, text))
            return out
    if ext == ".pptx":
        from pptx import Presentation

        out = []
        for i, slide in enumerate(Presentation(str(path)).slides, start=1):
            parts = [shape.text_frame.text for shape in slide.shapes if getattr(shape, "has_text_frame", False)]
            out.append((i, _clean("\n".join(parts))))
        return out
    if ext == ".docx":
        from docx import Document

        text = "\n".join(p.text for p in Document(str(path)).paragraphs)
        return _blocks(_clean(text))
    text = path.read_text(encoding="utf-8", errors="replace")
    if ext == ".md":
        sections = re.split(r"(?m)^(?=#{1,3} )", text)
        return [(i, _clean(s)) for i, s in enumerate((s for s in sections if s.strip()), start=1)]
    return _blocks(_clean(text))


# ── OCR for scanned PDFs ──────────────────────────────────────────────────────
# Uses the OCR engine built into Windows (offline, nothing to download; via the
# small winrt-* packages). It reads printed text well — problem statements,
# exercise numbers, headings — so scanned pages become searchable and ARYA can
# show the page. Handwriting is mostly skipped rather than turned into noise.
# ARYA_OCR=0 turns it off.

OCR_DPI = 200
_ocr_engine = None
_ocr_checked = False


def ocr_available() -> bool:
    global _ocr_engine, _ocr_checked
    if not _ocr_checked:
        _ocr_checked = True
        if os.getenv("ARYA_OCR", "1") != "0":
            try:
                from winrt.windows.globalization import Language
                from winrt.windows.media.ocr import OcrEngine

                _ocr_engine = (OcrEngine.try_create_from_user_profile_languages()
                               or OcrEngine.try_create_from_language(Language("en-US")))
            except Exception as exc:                  # not Windows / packages missing
                print(f"Knowledge: OCR unavailable ({exc})")
        if _ocr_engine is not None:
            print(f"Knowledge: OCR ready (Windows OCR, {_ocr_engine.recognizer_language.language_tag})")
    return _ocr_engine is not None


def _ocr_page(page) -> str:
    """Printed text of one PDF page image; '' if nothing readable."""
    from winrt.windows.media.ocr import OcrEngine

    longest = max(page.rect.width, page.rect.height) / 72
    dpi = min(OCR_DPI, int(OcrEngine.max_image_dimension / max(longest, 1)))
    pix = page.get_pixmap(dpi=dpi, alpha=True)                    # RGBA, opaque
    try:
        return ocr_rgba(pix.width, pix.height, bytes(pix.samples))
    except Exception as exc:
        print(f"Knowledge: OCR failed on page {page.number + 1}: {exc}")
        return ""


def ocr_rgba(width: int, height: int, rgba: bytes) -> str:
    """Printed text in an RGBA image (a PDF page, a screenshot, a photo). Call from a worker thread."""
    import asyncio

    from winrt.windows.graphics.imaging import BitmapAlphaMode, BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.storage.streams import DataWriter

    if not ocr_available():
        return ""
    writer = DataWriter()
    writer.write_bytes(rgba)
    bitmap = SoftwareBitmap.create_copy_from_buffer(
        writer.detach_buffer(), BitmapPixelFormat.RGBA8, width, height, BitmapAlphaMode.PREMULTIPLIED)
    result = asyncio.run(_ocr_engine.recognize_async(bitmap))
    # Keep lines with at least one real word; drops scribbles read as 'ie', '=', 'Vr'.
    lines = [ln.text for ln in result.lines if re.search(r"[A-Za-zÄÖÜäöüß]{3,}|\d+\s*[-–.]\s*\d+\)", ln.text)]
    return _clean("\n".join(lines))


_INDEX_PAGE = re.compile(r"^\s*(?:\d+\s*)?(?:index|contents|table of contents)\b", re.IGNORECASE)


def _is_index_page(text: str) -> bool:
    """Book index / table-of-contents pages match every query — skip them."""
    if _INDEX_PAGE.search(text[:60]):
        return True
    digits = sum(ch.isdigit() for ch in text)
    return len(text) > 400 and digits / len(text) > 0.18 and text.count(",") > 25


def _blocks(text: str, size: int = 3000) -> list[tuple[int, str]]:
    return [(i + 1, text[j:j + size]) for i, j in enumerate(range(0, max(1, len(text)), size))]


def _chunk(text: str) -> list[str]:
    """~750-character passages, cut at sentence/line ends where possible, overlapping a little."""
    if len(text) <= CHUNK_CHARS:
        return [text] if len(text) >= MIN_CHUNK_CHARS else []
    chunks, start = [], 0
    while start < len(text):
        end = min(len(text), start + CHUNK_CHARS)
        if end < len(text):
            cut = max(text.rfind(". ", start + CHUNK_CHARS // 2, end), text.rfind("\n", start + CHUNK_CHARS // 2, end))
            if cut > start:
                end = cut + 1
        piece = text[start:end].strip()
        if len(piece) >= MIN_CHUNK_CHARS:
            chunks.append(piece)
        if end >= len(text):
            break
        start = max(end - CHUNK_OVERLAP, start + 1)
    return chunks


def _fingerprint(path: Path) -> str:
    h = hashlib.sha1(str(path.stat().st_size).encode())
    with open(path, "rb") as f:
        h.update(f.read(1 << 20))
    return h.hexdigest()


_COPY_NAME = re.compile(
    r"^(?:copy|kopie)(?:\s*(?:of|von))?[\s_-]+|[\s_-]+(?:-\s*)?(?:copy|kopie)\s*\d*$|\s*\(\d+\)$", re.IGNORECASE)


def _copy_score(path: Path | str) -> int:
    """How much a file name looks like a copy ('copy of x', 'x - Copy', 'x (1)'): 0 = original.
    Among identical files the lowest score is indexed and cited; the others are duplicates."""
    stem, score = Path(path).stem.strip(), 0
    while (m := _COPY_NAME.search(stem)) and m.group(0) != stem:
        stem, score = (stem[:m.start()] + stem[m.end():]).strip(), score + 1
    return score


def _title_and_collection(path: Path, roots: list[Path]) -> tuple[str, str]:
    title = re.sub(r"[_]+", " ", path.stem).strip()
    if path.suffix.lower() in _media_extensions() and not re.search(r"[A-Za-z]{3}", title):
        # Recordings named "_ (11).mp4": call them after their folder ("CS1 Lecture Videos, recording 11").
        n = re.findall(r"\d+", title)
        title = f"{path.parent.name.strip()}, recording {n[-1] if n else 1}"
    if path == CORE_NOTES:
        return "ARYA core notes", "ARYA core notes"
    if path == CORRECTIONS:
        return CORRECTIONS_TITLE, CORRECTIONS_TITLE
    if path == TRANSCRIPTIONS:
        return TRANSCRIPTIONS_TITLE, TRANSCRIPTIONS_TITLE
    if path == PHOTO_NOTES:
        return PHOTO_NOTES_TITLE, PHOTO_NOTES_TITLE
    for root in roots:
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        # The course is the first meaningful folder ("Video Lectures/Video Lectures/Dynamics/..." -> Dynamics)
        parts = [p for p in rel.parts[:-1] if p.lower() != root.name.lower()]
        return title, (parts[0] if parts else root.name)
    return title, path.parent.name


# ── Indexing ──────────────────────────────────────────────────────────────────

@dataclass
class IndexStatus:
    running: bool = False
    done: int = 0
    total: int = 0
    current: str = ""
    finished_at: float = 0.0


status = IndexStatus()
_matrix_cache: dict = {}


def _media_extensions() -> set[str]:
    from arya.knowledge.lectures import MEDIA_EXTENSIONS
    return MEDIA_EXTENSIONS


def _lecture_ready(path: Path) -> bool:
    from arya.knowledge.lectures import transcript_ready
    return transcript_ready(path)


def _files(roots: list[Path]) -> list[Path]:
    from arya.core.policy import PolicyError, check_path

    files = []
    for root in roots:
        try:
            check_path(root)                       # never index outside the laptop-access sandbox
        except PolicyError as exc:
            print(f"Knowledge: skipping {root} ({exc})")
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
            for name in filenames:
                p = Path(dirpath) / name
                if p.suffix.lower() in EXTENSIONS and not name.startswith("~$"):
                    files.append(p)
                elif p.suffix.lower() in _media_extensions() and _lecture_ready(p):
                    files.append(p)                 # a lecture recording, once its transcript is complete
    if CORE_NOTES.exists():
        files.append(CORE_NOTES)
    if CORRECTIONS.exists():
        files.append(CORRECTIONS)
    if TRANSCRIPTIONS.exists():
        files.append(TRANSCRIPTIONS)
    if PHOTO_NOTES.exists():
        files.append(PHOTO_NOTES)
    return files


def index(roots: list[Path] | None = None, progress: Callable[[IndexStatus], None] | None = None) -> dict:
    """Bring the index up to date. Safe to call repeatedly (incremental)."""
    from arya.core.embedder import load_embedder

    roots = roots if roots is not None else default_roots()
    files = sorted(_files(roots), key=_copy_score)      # originals before 'copy of …' (stable sort)
    status.running, status.done, status.total, status.current = True, 0, len(files), ""
    model = load_embedder()
    added = updated = skipped = removed = 0
    seen_fingerprints: dict[str, str] = {}
    try:
        with _lock, _connect() as c:
            known = {row["path"]: row for row in c.execute("SELECT * FROM documents")}
            present = {str(f) for f in files}
            for path_str, row in known.items():          # files that disappeared
                if path_str not in present:
                    _delete_doc(c, row["id"])
                    removed += 1
            for row in c.execute("SELECT path, fingerprint FROM documents WHERE status = 'indexed'"):
                seen_fingerprints.setdefault(row["fingerprint"], row["path"])

        for path in files:
            status.done += 1
            status.current = path.name
            if progress:
                progress(status)
            try:
                st = path.stat()
            except OSError:
                continue
            row = known.get(str(path))
            if row and row["mtime"] == st.st_mtime and row["size"] == st.st_size:
                rival = seen_fingerprints.get(row["fingerprint"])
                # An unchanged file is skipped — unless it's an original that an older
                # index filed as a duplicate of its own copy; then it takes over.
                takeover = (row["status"].startswith("duplicate") and rival and rival != str(path)
                            and _copy_score(path) < _copy_score(rival))
                ocr_now = row["status"] == "needs OCR" and ocr_available()   # scanned earlier, readable now
                if not (takeover or ocr_now):
                    skipped += 1
                    continue
            fp = _fingerprint(path)
            title, collection = _title_and_collection(path, roots)
            dup_of = seen_fingerprints.get(fp)
            demote = dup_of if dup_of and dup_of != str(path) and _copy_score(path) < _copy_score(dup_of) else None
            if dup_of and dup_of != str(path) and not demote:
                _save_doc(path, title, collection, st, fp, 0, [], 0, f"duplicate of {Path(dup_of).name}", row)
                skipped += 1
                continue
            try:
                pages = _pages(path)
            except Exception as exc:
                print(f"Knowledge: couldn't read {path.name}: {exc}")
                _save_doc(path, title, collection, st, fp, 0, [], 0, "unreadable", row)
                continue
            empty = sum(1 for _, t in pages if len(t) < SCANNED_PAGE_CHARS) if path.suffix.lower() == ".pdf" else 0
            items = [(page, piece) for page, text in pages if not _is_index_page(text) for piece in _chunk(text)]
            vectors = (model.encode([f"{title}: {p}" for _, p in items], batch_size=48,
                                    normalize_embeddings=True, convert_to_numpy=True, show_progress_bar=False)
                       if items else np.zeros((0, 384), np.float32))
            mostly_empty = pages and empty >= 0.6 * len(pages)
            # After OCR, a still-empty scan is handwriting/pictures: index what there is, don't retry each run.
            doc_status = ("indexed" if items else "no readable text") if mostly_empty and ocr_available() \
                else "needs OCR" if mostly_empty else "indexed"
            _save_doc(path, title, collection, st, fp, len(pages), list(zip(items, vectors)), empty, doc_status, row)
            if demote and doc_status == "indexed":
                _demote_to_duplicate(Path(demote), fp, path.name, roots)
                seen_fingerprints[fp] = str(path)
            seen_fingerprints.setdefault(fp, str(path))
            added += row is None
            updated += row is not None
    finally:
        _matrix_cache.clear()
        try:
            _matrix()                      # warm the search matrix now, not on the first question
        except Exception:
            pass
        status.running = False
        status.finished_at = time.time()
        if progress:
            progress(status)
    summary = {"added": added, "updated": updated, "unchanged": skipped, "removed": removed}
    print(f"Knowledge: index up to date — {summary}")
    return summary


def _delete_doc(c: sqlite3.Connection, doc_id: int) -> None:
    for row in c.execute("SELECT id, text FROM chunks WHERE doc_id = ?", (doc_id,)).fetchall():
        c.execute("INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES('delete', ?, ?)", (row["id"], row["text"]))
    c.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
    c.execute("DELETE FROM documents WHERE id = ?", (doc_id,))


def _save_doc(path, title, collection, st, fp, n_pages, chunks, empty, doc_status, old_row) -> None:
    with _lock, _connect() as c:
        if old_row is not None:
            _delete_doc(c, old_row["id"])
        cur = c.execute(
            "INSERT INTO documents(path, title, collection, kind, mtime, size, fingerprint, pages, chunks,"
            " empty_pages, status, indexed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (str(path), title, collection, path.suffix.lower().lstrip("."), st.st_mtime, st.st_size, fp,
             n_pages, len(chunks), empty, doc_status, time.strftime("%Y-%m-%d %H:%M")))
        doc_id = cur.lastrowid
        for (page, text), vec in chunks:
            cid = c.execute("INSERT INTO chunks(doc_id, page, text, embedding) VALUES (?,?,?,?)",
                            (doc_id, page, text, np.asarray(vec, np.float16).tobytes())).lastrowid
            c.execute("INSERT INTO chunks_fts(rowid, text) VALUES (?, ?)", (cid, text))


def _demote_to_duplicate(path: Path, fp: str, original_name: str, roots: list[Path]) -> None:
    """The copy that was indexed before its original turned up: drop its passages, keep it listed as a duplicate."""
    with _lock, _connect() as c:
        old_row = c.execute("SELECT * FROM documents WHERE path = ?", (str(path),)).fetchone()
    try:
        st = path.stat()
    except OSError:
        return
    title, collection = _title_and_collection(path, roots)
    _save_doc(path, title, collection, st, fp, 0, [], 0, f"duplicate of {original_name}", old_row)


def start_background_indexing(progress: Callable[[IndexStatus], None] | None = None) -> threading.Thread | None:
    if status.running:
        return None
    t = threading.Thread(target=lambda: index(progress=progress), name="knowledge-index", daemon=True)
    t.start()
    return t


# ── Search ────────────────────────────────────────────────────────────────────

@dataclass
class Passage:
    chunk_id: int
    doc_id: int
    title: str
    collection: str
    kind: str
    page: int
    text: str
    similarity: float
    keyword_hit: bool
    note: str = ""          # e.g. "exercise 1-13" when found by its number

    def label(self) -> str:
        if "." + self.kind in _media_extensions():
            from arya.knowledge.lectures import BLOCK_S, clock
            return f"{self.title}, at {clock((self.page - 1) * BLOCK_S)}"     # "Dynamics lecture 5, at 24:00"
        unit = {"pptx": "slide", "md": "section"}.get(self.kind, "p.")
        return f"{self.title}, {unit} {self.page}" if unit != "p." else f"{self.title}, p. {self.page}"

    def as_dict(self) -> dict:
        return {"doc": self.doc_id, "page": self.page, "title": self.title, "collection": self.collection,
                "kind": self.kind, "label": self.label(), "snippet": self.text[:240], "chunk": self.chunk_id}


def _matrix() -> tuple[np.ndarray, np.ndarray]:
    if "m" not in _matrix_cache:
        with _lock, _connect() as c:
            rows = c.execute("SELECT id, embedding FROM chunks").fetchall()
        ids = np.array([r["id"] for r in rows], dtype=np.int64)
        m = (np.vstack([np.frombuffer(r["embedding"], np.float16) for r in rows]).astype(np.float32)
             if rows else np.zeros((0, 384), np.float32))
        _matrix_cache["m"], _matrix_cache["ids"] = m, ids
    return _matrix_cache["m"], _matrix_cache["ids"]


CORE_NOTES_BOOST = float(os.getenv("ARYA_KB_CORE_BOOST", "1.3"))
SHORT_CHUNK_CHARS = 200
SHORT_CHUNK_PENALTY = 0.6

_STOP = set("""a an the of to in on for and or is are was were be been what whats how why when where which who
whom does do did can could would should explain tell me about please give show find with from by as at it its
this that these those i you my your our formula define definition mean means""".split())


def _fts_query(query: str) -> str:
    words = [w for w in re.findall(r"[a-zA-Z][a-zA-Z0-9]{2,}", query.lower()) if w not in _STOP]
    return " OR ".join(f'"{w}"' for w in words[:12])


def search(query: str, k: int = 5) -> list[Passage]:
    """Hybrid search; best first."""
    from arya.core.embedder import embed_sync

    m, ids = _matrix()
    if not len(ids):
        return []
    vec = embed_sync(query)
    sims = m @ vec
    vec_rank = np.argsort(-sims)[:40]
    scores: dict[int, float] = {}
    sim_of = {int(ids[i]): float(sims[i]) for i in vec_rank}
    for rank, i in enumerate(vec_rank):
        scores[int(ids[i])] = scores.get(int(ids[i]), 0) + 1 / (60 + rank)
    keyword_ids: set[int] = set()
    q = _fts_query(query)
    if q:
        with _lock, _connect() as c:
            rows = c.execute("SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY bm25(chunks_fts) LIMIT 40",
                             (q,)).fetchall()
        for rank, r in enumerate(rows):
            cid = int(r["rowid"])
            keyword_ids.add(cid)
            scores[cid] = scores.get(cid, 0) + 1 / (60 + rank)
    if not scores:
        return []
    with _lock, _connect() as c:
        ids_ = list(scores)
        rows = {r["id"]: r for r in c.execute(
            f"SELECT chunks.id, doc_id, page, text, title, collection, kind, path FROM chunks "
            f"JOIN documents ON documents.id = chunks.doc_id WHERE chunks.id IN ({','.join('?' * len(ids_))})", ids_)}
    core, corrections = str(CORE_NOTES), str(CORRECTIONS)
    top_sim = max(sim_of.values()) if sim_of else 0.0
    for cid, r in rows.items():
        # Boosts only between near-equal matches: a loosely related core-notes section must not
        # outrank a much better match elsewhere (a photo note at 0.77 vs core notes at 0.37).
        close = sim_of.get(cid, 0.0) >= top_sim - 0.15
        if r["path"] == core and close:
            scores[cid] *= CORE_NOTES_BOOST       # checked fundamentals beat a lab report that shares keywords
        elif r["path"] == corrections and close:
            scores[cid] *= CORE_NOTES_BOOST * 1.3  # your own corrections beat everything
        if len(r["text"]) < SHORT_CHUNK_CHARS:
            scores[cid] *= SHORT_CHUNK_PENALTY     # near-empty pages (titles, figure captions) match too easily
    best = sorted(scores, key=scores.get, reverse=True)[: k * 3]
    out: list[Passage] = []
    seen_pages = set()
    for cid in best:
        r = rows.get(cid)
        if r is None or (r["doc_id"], r["page"]) in seen_pages:
            continue                                   # one passage per page is enough
        seen_pages.add((r["doc_id"], r["page"]))
        sim = sim_of.get(cid)
        if sim is None:
            sim = float(np.frombuffer(_embedding(cid), np.float16).astype(np.float32) @ vec)
        out.append(Passage(cid, r["doc_id"], r["title"], r["collection"], r["kind"], r["page"], r["text"],
                           sim, cid in keyword_ids))
        if len(out) >= k:
            break
    return out


def _embedding(chunk_id: int) -> bytes:
    with _lock, _connect() as c:
        return c.execute("SELECT embedding FROM chunks WHERE id = ?", (chunk_id,)).fetchone()["embedding"]


RELEVANT_SIMILARITY = float(os.getenv("ARYA_KB_MIN_SIMILARITY", "0.42"))


def relevant(query: str, k: int = 4) -> list[Passage]:
    """Passages good enough to ground an answer on — empty when the material doesn't cover it."""
    hits = search(query, k=k + 6)
    if not hits:
        return []
    top = max(h.similarity for h in hits)
    if top < RELEVANT_SIMILARITY:
        return []
    keep = [h for h in hits if h.similarity >= max(RELEVANT_SIMILARITY - 0.08, top - 0.15)][:k]
    # The checked core notes are the most reliable source: if one is relevant but was
    # crowded out by keyword-heavy pages (lab reports), it goes in first.
    core = [h for h in hits if h.title == "ARYA core notes" and h.similarity >= RELEVANT_SIMILARITY - 0.05]
    if core and not any(h.title == "ARYA core notes" for h in keep):
        keep = [core[0]] + keep[:k - 1]
    # A correction you gave earlier on this topic always goes first.
    fixes = [h for h in hits if h.title == CORRECTIONS_TITLE and h.similarity >= RELEVANT_SIMILARITY - 0.05]
    if fixes:
        keep = [fixes[0]] + [h for h in keep if h.chunk_id != fixes[0].chunk_id][:k - 1]
    return keep


def index_file(path: Path) -> None:
    """(Re)index one file right away — e.g. a new correction — without a full library pass."""
    from arya.core.embedder import load_embedder

    path = Path(path)
    model = load_embedder()
    with _lock, _connect() as c:
        row = c.execute("SELECT * FROM documents WHERE path = ?", (str(path),)).fetchone()
    if not path.exists():
        if row is not None:
            with _lock, _connect() as c:
                _delete_doc(c, row["id"])
        _matrix_cache.clear()
        return
    # Lecture recordings take their course from the folder they sit in under your course roots.
    title, collection = _title_and_collection(path, default_roots() if path.suffix.lower() in _media_extensions() else [])
    pages = _pages(path)
    items = [(page, piece) for page, text in pages for piece in _chunk(text)]
    vectors = (model.encode([f"{title}: {p}" for _, p in items], normalize_embeddings=True,
                            convert_to_numpy=True, show_progress_bar=False)
               if items else np.zeros((0, 384), np.float32))
    _save_doc(path, title, collection, path.stat(), _fingerprint(path), len(pages), list(zip(items, vectors)),
              0, "indexed", row)
    _matrix_cache.clear()


# ── Exercises by number ───────────────────────────────────────────────────────
# Course sheets label exercises "1-13)" (chapter 1, exercise 13), and the
# answers section repeats the label. Meaning-based search can't match a number,
# so "exercise 113" / "chapter one question 13" / "A 1-13" are looked up here.

_NUMBER_WORDS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
    "seventeen eighteen nineteen twenty".split())}
_EX_WORD = (r"(?:exercises?|excercises?|questions?|problems?|tasks?|homeworks?|assignments?|aufgaben?|"
            r"übung(?:en)?|uebung(?:en)?|ex\.?|nr\.?|no\.?|number|a)")


def exercise_refs(question: str) -> list[str]:
    """'exercise 113' -> ['1-13', '11-3']; 'chapter one, question 30' -> ['1-30'];
    'A 1-13' / 'exercise 1.13' -> ['1-13']. [] when no exercise number is asked for."""
    t = question.lower()
    t = re.sub(r"\b(" + "|".join(_NUMBER_WORDS) + r")\b", lambda m: str(_NUMBER_WORDS[m.group(1)]), t)
    m = re.search(r"\bchapter\s*(\d{1,2})\b.{0,40}?\b" + _EX_WORD + r"\s*(?:no\.?|number|nr\.?)?\s*(\d{1,2})\b", t)
    if m:
        return [f"{int(m.group(1))}-{int(m.group(2))}"]
    m = re.search(r"\b" + _EX_WORD + r"\s*(?:no\.?|number|nr\.?)?\s*(\d{1,2})\s*[-–./]\s*(\d{1,2})\b", t) \
        or re.search(r"\b" + _EX_WORD + r"\s*(?:no\.?|number|nr\.?)?\s*(\d{1,2})\s+(\d{1,2})\b", t)
    if m:
        return [f"{int(m.group(1))}-{int(m.group(2))}"]
    m = re.search(r"\b" + _EX_WORD + r"\s*(?:no\.?|number|nr\.?)?\s*(\d)(\d)(\d)\b", t)   # spoken "one thirteen"
    if m:
        a, b, c = m.groups()
        return [f"{int(a)}-{int(b + c)}", f"{int(a + b)}-{int(c)}"]
    return []


def _label_pattern(ref: str) -> re.Pattern:
    a, b = ref.split("-")
    return re.compile(rf"(?<![\d.,(/])(?:A\s*)?{a}\s*[-–]\s*{b}(?!\d)\s*\)")


def _page_text(c: sqlite3.Connection, doc_id: int, page: int) -> tuple[str, int]:
    """The page's text stitched back from its overlapping chunks, and its first chunk id."""
    rows = c.execute("SELECT id, text FROM chunks WHERE doc_id = ? AND page = ? ORDER BY id", (doc_id, page)).fetchall()
    text = ""
    for r in rows:
        piece = r["text"]
        if text:
            head = piece[:40]
            at = text.rfind(head, max(0, len(text) - CHUNK_OVERLAP - 80))
            piece = piece[len(text) - at:] if at >= 0 else "\n" + piece
        text += piece
    return text, (rows[0]["id"] if rows else 0)


def find_exercise(refs: list[str], question: str = "", k: int = 4) -> list[Passage]:
    """Passages for an exercise number: its statement and its answers, wherever they are labelled."""
    q = question.lower()
    found: list[Passage] = []
    with _lock, _connect() as c:
        collections = [r["collection"] for r in c.execute("SELECT DISTINCT collection FROM documents")]
        wanted = [col for col in collections if col and col.lower() in q]      # "dynamics exercise 113"
        for ref in refs:
            a, b = ref.split("-")
            label = _label_pattern(ref)
            rows = c.execute(
                "SELECT DISTINCT chunks.doc_id, chunks.page, title, collection, kind FROM chunks_fts "
                "JOIN chunks ON chunks.id = chunks_fts.rowid JOIN documents ON documents.id = chunks.doc_id "
                "WHERE chunks_fts MATCH ? ORDER BY title, chunks.page", (f'"{a} {b}"',)).fetchall()
            for r in rows:
                if wanted and r["collection"] not in wanted:
                    continue
                text, first_id = _page_text(c, r["doc_id"], r["page"])
                m = label.search(text)
                if not m:
                    continue
                nxt = re.search(r"(?<![\d.,(/])(?:A\s*)?\d{1,2}\s*[-–]\s*\d{1,2}\s*\)", text[m.end():])
                segment = text[m.start(): m.end() + (nxt.start() if nxt else 1500)][:1800].strip()
                found.append(Passage(first_id, r["doc_id"], r["title"], r["collection"], r["kind"], r["page"],
                                     segment, 1.0, True, note=f"exercise {ref}"))
            if found:                       # the first reading of the number that exists wins
                break
        if found:                           # your handwritten solution, if it was read before (transcriptions.md)
            ref = found[0].note.replace("exercise ", "")
            row = c.execute(
                "SELECT chunks.doc_id, chunks.page FROM chunks JOIN documents ON documents.id = chunks.doc_id "
                "WHERE documents.path = ? AND chunks.text LIKE ? LIMIT 1",
                (str(TRANSCRIPTIONS), f"%## Exercise {ref} —%")).fetchone()
            if row:
                text, first_id = _page_text(c, row["doc_id"], row["page"])
                found.insert(min(1, len(found)), Passage(
                    first_id, row["doc_id"], TRANSCRIPTIONS_TITLE, TRANSCRIPTIONS_TITLE, "md", row["page"],
                    text[:2500].strip(), 1.0, True, note=f"exercise {ref}"))
    return found[:k]


# ── Where is it in my material? ───────────────────────────────────────────────

_WHERE = re.compile(
    r"\b(?:which|what) (?:page|pages|section|chapter|slide|slides)\b"
    r"|\bwhere (?:in|do|does|did|can i find|is|are)\b.{0,60}\b(?:my|the) (?:book|notes|script|slides|lecture|"
    r"material|course|pdf|documents?|exercises?)\b"
    r"|\bfind\b.{0,40}\bin my (?:notes|book|material|script|slides|lectures?)\b", re.IGNORECASE)
_WHERE_FILLER = re.compile(
    r"\b(?:which|what|page|pages|section|chapter|slides?|of|in|on|my|the|a|an|book|notes|script|lecture|lectures|"
    r"material|course|pdf|documents?|explains?|explained|covers?|covered|discuss(?:es|ed)?|talks?|about|where|is|are|"
    r"does|do|did|can|i|find|me|show|it|there)\b", re.IGNORECASE)


def locate(question: str, k: int = 3) -> list[Passage] | None:
    """'Which page of my dynamics book explains the Coriolis acceleration?' -> the pages, from the
    search itself (the model guessed page numbers). None if this isn't a where-is question."""
    if not _WHERE.search(question):
        return None
    topic = re.sub(r"\s+", " ", _WHERE_FILLER.sub(" ", question)).strip(" ?.!,")
    with _lock, _connect() as c:
        collections = [r["collection"] for r in c.execute("SELECT DISTINCT collection FROM documents")]
    wanted = [col for col in collections if col and col.lower() in question.lower()]
    for col in wanted:                                       # "dynamics" names the course, not the topic
        topic = re.sub(rf"\b{re.escape(col)}\b", " ", topic, flags=re.IGNORECASE).strip()
    if len(topic) < 3:
        return None
    hits = [h for h in search(topic, k=k * 4) if h.title not in ("ARYA core notes", CORRECTIONS_TITLE)]
    if wanted:
        hits = [h for h in hits if h.collection in wanted] or hits
    hits = [h for h in hits if h.similarity >= RELEVANT_SIMILARITY - 0.08][:k]
    return hits


def where_answer(question: str, hits: list[Passage]) -> str:
    topic = re.sub(r"\s+", " ", _WHERE_FILLER.sub(" ", question)).strip(" ?.!,")
    with _lock, _connect() as c:
        for (col,) in c.execute("SELECT DISTINCT collection FROM documents"):
            if col and col.lower() in question.lower():
                topic = re.sub(rf"\b{re.escape(col)}\b", " ", topic, flags=re.IGNORECASE)
    topic = re.sub(r"\s+", " ", topic).strip() or "that"
    if not hits:
        return f"I couldn't find {topic} in your course material."
    first, rest = hits[0], hits[1:]
    more = ""
    if rest:
        same = [h for h in rest if h.doc_id == first.doc_id]
        other = [h for h in rest if h.doc_id != first.doc_id]
        bits = []
        if same:
            unit = first.label().rsplit(", ", 1)[1].split(" ")[0]                     # "p." / "slide" / "section"
            pages = " and ".join(str(h.page) for h in same)
            bits.append(f"on {'pp.' if unit == 'p.' and len(same) > 1 else unit} {pages}")
        bits += [f"in {h.label()}" for h in other]
        more = " It also comes up " + ", and ".join(bits) + "."
    return f"{topic[:1].upper() + topic[1:]} is covered in {first.label()}; it's on screen.{more}"


# ── What's in the library ─────────────────────────────────────────────────────

_OV_ASK = re.compile(r"\b(?:what|which|list|show|tell me)\b", re.IGNORECASE)
_OV_NOUN = re.compile(r"\b(?:resources?|materials?|documents?|files?|library|sources?|scripts?|books?|pdfs?)\b",
                      re.IGNORECASE)
_OV_HAVE = re.compile(r"\b(?:have|got|there|contain\w*|includ\w*|available|indexed|in (?:my|your|the))\b",
                      re.IGNORECASE)


def overview(question: str) -> str | None:
    """'What do you have in your resources about dynamics?' -> a spoken list of that course's documents.
    None for anything else (including 'what do the documents say about X' — that's a real question)."""
    if len(question.split()) > 22 or not (_OV_ASK.search(question) and _OV_NOUN.search(question)
                                          and _OV_HAVE.search(question)):
        return None
    if re.search(r"\b(?:say|says|explain|mean|formula|how|why)\b", question, re.IGNORECASE):
        return None
    q = question.lower()
    with _lock, _connect() as c:
        docs = c.execute("SELECT title, collection, pages, status, kind FROM documents "
                         "WHERE status NOT LIKE 'duplicate%' AND path NOT IN (?, ?, ?, ?) ORDER BY collection, title",
                         (str(CORE_NOTES), str(CORRECTIONS), str(TRANSCRIPTIONS), str(PHOTO_NOTES))).fetchall()
    if not docs:
        return "Your course library is empty so far — I haven't indexed any documents yet."
    by_col: dict[str, list] = {}
    for d in docs:
        by_col.setdefault(d["collection"], []).append(d)
    wanted = [col for col in by_col if col.lower() in q]
    if not wanted:
        words = set(re.findall(r"[a-z]{4,}", q))
        wanted = [col for col in by_col if words & set(re.findall(r"[a-z]{4,}", col.lower()))]
    if not wanted and re.search(r"\babout\s+\w", q):
        return None                     # "what files do you have about Coriolis?" -> let search answer it
    if not wanted:
        cols = sorted(by_col, key=lambda col: -len(by_col[col]))
        listed = ", ".join(f"{col} ({len(by_col[col])})" for col in cols[:12])
        return (f"I have {len(docs)} documents from your courses, in {len(by_col)} folders: {listed}"
                + (", and more." if len(cols) > 12 else ".") + " Ask me about any of them.")
    parts = []
    for col in wanted:
        items = []
        for d in by_col[col]:
            unit = "slides" if d["kind"] == "pptx" else "pages"
            extra = "; a scan I can only partly read" if d["status"] in ("needs OCR", "no readable text") else ""
            items.append(f"{d['title']} ({d['pages']} {unit}{extra})")
        parts.append(f"For {col} I have {len(items)} documents: " + "; ".join(items) + ".")
    return " ".join(parts) + " Ask me about a topic or an exercise number, and I'll show you the page."


def context_block(passages: list[Passage]) -> str:
    return "\n\n".join(f"[{i}] ({p.label()})\n{p.text}" for i, p in enumerate(passages, start=1))


def stats() -> dict:
    with _lock, _connect() as c:
        docs = c.execute("SELECT status, COUNT(*) n, SUM(pages) p, SUM(chunks) ch FROM documents GROUP BY status").fetchall()
        collections = [r["collection"] for r in c.execute(
            "SELECT collection, COUNT(*) n FROM documents WHERE status='indexed' GROUP BY collection ORDER BY n DESC")]
        ocr = [r["title"] for r in c.execute("SELECT title FROM documents WHERE status='needs OCR' ORDER BY title")]
    by = {r["status"]: r for r in docs}
    indexed = by.get("indexed")
    return {
        "documents": indexed["n"] if indexed else 0,
        "pages": int(indexed["p"] or 0) if indexed else 0,
        "passages": int(indexed["ch"] or 0) if indexed else 0,
        "duplicates": sum(r["n"] for s, r in by.items() if s.startswith("duplicate")),
        "needs_ocr": ocr,
        "collections": collections,
        "indexing": status.running,
        "progress": [status.done, status.total],
        "current": status.current,
    }


def document(doc_id: int) -> sqlite3.Row | None:
    with _lock, _connect() as c:
        return c.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()


# ── Page previews ─────────────────────────────────────────────────────────────

_render_cache: "OrderedDict[tuple, bytes]" = OrderedDict()


def render_page(doc_id: int, page: int, highlight: str = "", zoom: float = 1.6) -> bytes | None:
    """PNG of one PDF page, with the passage highlighted. Rendered in memory —
    the file on disk is opened read-only and never saved."""
    key = (doc_id, page, highlight[:80], zoom)
    if key in _render_cache:
        _render_cache.move_to_end(key)
        return _render_cache[key]
    row = document(doc_id)
    if row is None or row["kind"] != "pdf":
        return None
    from arya.core.policy import check_path

    path = check_path(row["path"])
    import pymupdf

    with pymupdf.open(path) as doc:
        if not 1 <= page <= doc.page_count:
            return None
        pg = doc[page - 1]
        if highlight:
            words = re.findall(r"\S+", highlight)
            for n in (8, 5, 3):                      # find the passage's opening words on the page
                if len(words) < n:
                    continue
                rects = pg.search_for(" ".join(words[:n]))
                if rects:
                    for r in rects[:3]:
                        annot = pg.add_highlight_annot(r)
                        annot.set_colors(stroke=(1.0, 0.85, 0.2))
                        annot.update(opacity=0.45)
                    break
        png = pg.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), annots=True).tobytes("png")
    _render_cache[key] = png
    if len(_render_cache) > 40:
        _render_cache.popitem(last=False)
    return png
