"""Course library: indexing, duplicates, search, exercises by number, overview, OCR."""
from pathlib import Path

import pytest


def _docs(library):
    with library._connect() as c:
        return {r["title"]: r["status"] for r in c.execute("SELECT title, status FROM documents")}


def test_index_and_duplicate_prefers_the_original(kb):
    docs = _docs(kb["library"])
    assert docs["lecture2"] == "indexed"
    assert docs["copy of lecture2"] == "duplicate of lecture2.md"
    assert docs["Exercises chapter 1"] == "indexed"


def test_reindex_is_incremental(kb):
    again = kb["library"].index([kb["root"]])
    assert again["added"] == again["updated"] == 0


@pytest.mark.parametrize("name, score", [
    ("lecture1.pdf", 0), ("copy of lecture1.pdf", 1), ("Copy of Copy of notes.pdf", 2), ("lecture1 - Copy.pdf", 1),
    ("lecture1 (1).pdf", 1), ("Kopie von Skript.pdf", 1), ("copyright notes.md", 0), ("photocopy.md", 0),
])
def test_copy_names(name, score, kb):
    assert kb["library"]._copy_score(name) == score


def test_relevant_finds_on_topic_and_nothing_off_topic(kb):
    lib = kb["library"]
    hits = lib.relevant("how do I compute the percent overshoot from the damping ratio?")
    assert hits and "overshoot" in hits[0].text and "copy" not in hits[0].label()
    assert lib.relevant("who won the football world cup in 2014?") == []


@pytest.mark.parametrize("question, refs", [
    ("Can you look dynamics exercise 113 and explain me?", ["1-13", "11-3"]),
    ("in case you have a solution in chapter one, question 30.", ["1-30"]),
    ("explain exercise 1-13", ["1-13"]), ("homework A 1-13 please", ["1-13"]), ("aufgabe 1-12", ["1-12"]),
    ("chapter three problem number five", ["3-5"]), ("What is the Coriolis acceleration?", []),
    ("I have 3 questions about 2 topics", []), ("plot x from 1 to 13", []),
])
def test_exercise_refs(question, refs, kb):
    assert kb["library"].exercise_refs(question) == refs


def test_find_exercise_returns_statement_and_answers(kb):
    lib = kb["library"]
    found = lib.find_exercise(lib.exercise_refs("dynamics exercise 113"), "dynamics exercise 113")
    texts = [p.text for p in found]
    assert len(found) == 2 and all(p.note == "exercise 1-13" for p in found)
    assert texts[0].startswith("1-13)") and "industrial robot" in texts[0] and "1-14)" not in texts[0]
    assert any("2.83 m/s" in t for t in texts)
    assert lib.find_exercise(["1-30"], "chapter one question 30") == []


@pytest.mark.parametrize("question, expect", [
    ("tell me what you have in your resources about dynamics.", "For Dynamics I have 1 documents"),
    ("what documents do you have?", "I have "),
    ("What is the Coriolis acceleration?", None),
    ("what do the documents say about friction?", None),
    ("what files do you have about coriolis?", None),
])
def test_library_overview(question, expect, kb):
    answer = kb["library"].overview(question)
    assert (answer is None) if expect is None else (answer and answer.startswith(expect))


def test_ocr_reads_a_scanned_page(tmp_path, kb):
    lib = kb["library"]
    if not lib.ocr_available():
        pytest.skip("Windows OCR not available")
    import pymupdf

    text_doc = pymupdf.open()
    page = text_doc.new_page()
    page.insert_text((72, 100), "1-9) A platform of a merry-go-round is rotating", fontsize=16)
    page.insert_text((72, 125), "with constant angular velocity. Determine the acceleration.", fontsize=16)
    pix = page.get_pixmap(dpi=200)
    scan = pymupdf.open()                                  # an image-only PDF, like a scanner makes
    scan.new_page(width=page.rect.width, height=page.rect.height).insert_image(page.rect, pixmap=pix)
    path = tmp_path / "scan.pdf"
    scan.save(path)
    pages = lib._pages(Path(path))
    assert "merry-go-round" in pages[0][1] and "1-9)" in pages[0][1]
