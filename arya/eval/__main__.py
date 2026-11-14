"""
ARYA quality benchmark:  python -m arya.eval [--tag control] [--id coriolis-formula] [--cloud] [-v]

Runs every question in arya/eval/golden.yaml through ARYA's real answer pipeline
(stream_with_tools: instant answers, tools, course search, local model) and checks
the text that would appear on screen. Your conversation history is not read or
written: the run uses a throwaway history database. The course library is the real
one, so library questions test your actual material.

Results go to arya/eval/results/<time>.json; each run is compared with the last.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"


def _norm(s: str) -> str:
    """Case, spaces and the way multiplication is written don't matter: 'V = I × R' == 'V = IR'."""
    s = s.replace("−", "-")
    s = re.sub(r"\s*[×·*⋅]\s*", "", s)
    return re.sub(r"\s+", "", s).lower()


def _hit(pattern: str, text: str) -> bool:
    if pattern.startswith("re:"):
        return re.search(pattern[3:], text, re.IGNORECASE) is not None
    return _norm(pattern) in _norm(text)


def grade(item: dict, text: str, defaults: dict) -> tuple[bool, list[str]]:
    """(passed, reasons it failed)."""
    from arya.core.llm import split_sentences

    problems = []
    for group in item.get("include", []):
        if not any(_hit(p, text) for p in group):
            problems.append(f"missing one of {group[:4]}{' …' if len(group) > 4 else ''}")
    for p in list(defaults.get("exclude", [])) + list(item.get("exclude", [])):
        if _hit(p, text):
            problems.append(f"contains forbidden {p!r}")
    limit = item.get("max_sentences", defaults.get("max_sentences", 8))
    n = len([s for s in split_sentences(text) if re.search(r"[A-Za-z]{2,}", s)])
    if n > limit:
        problems.append(f"{n} sentences (max {limit})")
    return not problems, problems


def _regrade() -> int:
    """After fixing a check in golden.yaml: re-score the saved answers of the latest run."""
    import yaml

    sys.path.insert(0, str(HERE.parents[1]))
    runs = sorted(p for p in RESULTS.glob("*.json") if "-partial" not in p.name)
    if not runs:
        print("No saved run to re-grade.")
        return 1
    run = json.loads(runs[-1].read_text(encoding="utf-8"))
    data = yaml.safe_load((HERE / "golden.yaml").read_text(encoding="utf-8"))
    items = {q["id"]: q for q in data["questions"]}
    changed = []
    for r in run["results"]:
        if r["id"] in items and r["answer"]:
            ok, problems = grade(items[r["id"]], r["answer"], data.get("defaults", {}))
            if ok != r["passed"]:
                changed.append(f"{r['id']}: {'PASS' if ok else 'FAIL'}")
            r["passed"], r["problems"] = ok, problems
    run["passed"] = sum(r["passed"] for r in run["results"])
    run["score"] = 100.0 * run["passed"] / len(run["results"])
    run["regraded_from"] = runs[-1].name
    out = runs[-1].with_name(runs[-1].stem + "-regraded.json")
    out.write_text(json.dumps(run, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Re-graded {runs[-1].name}: {run['passed']}/{len(run['results'])} = {run['score']:.0f} %; "
          f"changed: {changed or 'none'}")
    print(f"Saved {out.relative_to(HERE.parents[1])}")
    return 0


async def _answer(question: str) -> str:
    import arya.core.llm as llm

    parts = [s async for s in llm.stream_with_tools(question)]
    return "".join(parts).strip()


def main() -> int:
    ap = argparse.ArgumentParser(description="Run ARYA's golden questions and score the answers.")
    ap.add_argument("--tag", help="only questions with this tag (e.g. control, dynamics, library)")
    ap.add_argument("--id", help="only this question id")
    ap.add_argument("--cloud", action="store_true", help="answer with Groq first (ARYA_MODE=cloud)")
    ap.add_argument("-v", "--verbose", action="store_true", help="print every answer")
    ap.add_argument("--regrade", action="store_true",
                    help="re-score the answers of the latest run with the current golden.yaml (no model calls)")
    args = ap.parse_args()
    if args.regrade:
        return _regrade()

    # Isolate history before ARYA's modules read their settings.
    os.environ["ARYA_DB_PATH"] = str(Path(tempfile.mkdtemp(prefix="arya_eval_")) / "history.db")
    if args.cloud:
        os.environ["ARYA_MODE"] = "cloud"
    sys.path.insert(0, str(HERE.parents[1]))

    import yaml

    import arya.core.llm as llm

    llm.set_artifact_sink(lambda kind, payload: None)          # no app window during the run
    from arya.core.embedder import load_embedder
    from arya.knowledge import library
    load_embedder()                                            # warm up like the running app is, so the first
    library.relevant("warm up")                                # question isn't timed against model loading
    data = yaml.safe_load((HERE / "golden.yaml").read_text(encoding="utf-8"))
    defaults, items = data.get("defaults", {}), data["questions"]
    if args.tag:
        items = [q for q in items if args.tag in q.get("tags", [])]
    if args.id:
        items = [q for q in items if q["id"] == args.id]
    if not items:
        print("No questions match.")
        return 1

    mode = "cloud" if args.cloud else llm.ARYA_MODE
    print(f"ARYA eval — {len(items)} questions, mode {mode}\n")
    results, started = [], time.monotonic()
    loop = asyncio.new_event_loop()                 # one loop for the run: cached HTTP clients stay valid
    for i, item in enumerate(items, 1):
        t0 = time.monotonic()
        try:
            text = loop.run_until_complete(_answer(item["q"]))
            ok, problems = grade(item, text, defaults)
        except Exception as exc:                                  # a crash is a failure, not the end of the run
            text, ok, problems = "", False, [f"error: {type(exc).__name__}: {exc}"]
        pending = [t for t in asyncio.all_tasks(loop) if not t.done()]     # e.g. memory embeddings
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        took = time.monotonic() - t0
        results.append({"id": item["id"], "tags": item.get("tags", []), "q": item["q"], "passed": ok,
                        "problems": problems, "answer": text, "seconds": round(took, 1)})
        mark = "PASS" if ok else "FAIL"
        print(f"{i:3d}. {mark}  {item['id']:<24} {took:5.1f}s" + ("" if ok else "  — " + "; ".join(problems)))
        if args.verbose or not ok:
            print("        " + (text[:400].replace("\n", " ") or "(no answer)"))

    passed = sum(r["passed"] for r in results)
    score = 100.0 * passed / len(results)
    print(f"\nScore: {passed}/{len(results)} = {score:.0f} %   ({time.monotonic() - started:.0f} s)")
    by_tag: dict[str, list[bool]] = {}
    for r in results:
        for t in r["tags"]:
            by_tag.setdefault(t, []).append(r["passed"])
    print("By tag: " + ", ".join(f"{t} {sum(v)}/{len(v)}" for t, v in sorted(by_tag.items())))

    RESULTS.mkdir(exist_ok=True)
    partial = bool(args.tag or args.id)                        # never the baseline for the next full run
    previous = sorted(p for p in RESULTS.glob("*.json") if "-partial" not in p.name)
    if previous:
        before = {r["id"]: r["passed"] for r in json.loads(previous[-1].read_text(encoding="utf-8"))["results"]}
        fixed = [r["id"] for r in results if r["passed"] and before.get(r["id"]) is False]
        broke = [r["id"] for r in results if not r["passed"] and before.get(r["id"]) is True]
        print(f"Compared with {previous[-1].name}: fixed {fixed or 'none'}; newly failing {broke or 'none'}")
    out = RESULTS / f"{datetime.now():%Y%m%d-%H%M%S}-{mode}{'-partial' if partial else ''}.json"
    out.write_text(json.dumps({"mode": mode, "score": score, "passed": passed, "total": len(results),
                               "results": results}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Saved {out.relative_to(HERE.parents[1])}")
    return 0 if passed == len(results) else 2


if __name__ == "__main__":
    sys.exit(main())
