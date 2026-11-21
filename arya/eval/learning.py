"""
The learning benchmark: python -m arya.eval --learning

Every case in learning.yaml goes through the real local model and the real checks of
arya/memory/learning.py, each in a fresh scratch database. Reported:

  forbidden   memories that must never be kept (the number that has to be 0)
  recall      expected memories that were kept
  precision   kept memories that were expected / all kept memories
"""
from __future__ import annotations

import asyncio
import re
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent


def run(verbose: bool = False) -> int:
    import yaml

    from arya.core.embedder import load_embedder
    from arya.memory import learning

    load_embedder()
    cases = yaml.safe_load((HERE / "learning.yaml").read_text(encoding="utf-8"))["cases"]
    loop = asyncio.new_event_loop()
    forbidden = expected = found = kept_total = kept_expected = 0
    failures = []
    t0 = time.monotonic()
    for i, case in enumerate(cases, 1):
        learning.DB_OVERRIDE = Path(tempfile.mkdtemp(prefix="arya_learn_")) / "learn.db"
        from arya.memory.database import note_source, save_message
        for n, convo in enumerate(case["conversations"]):
            # A line can say how it reached ARYA: [user, "…", "follow:model"] (default: typed). They go into the
            # eval's scratch history, so learning reads the sources exactly as it does live.
            ids = []
            for line in convo:
                mid = save_message(line[0], line[1])
                if line[0] == "user":
                    src = line[2] if len(line) > 2 else "typed"
                    p = float(src.split("@")[1]) if "@" in src else None
                    note_source(mid, src.split("@")[0], p)
                ids.append(mid)
            cv = learning.Conversation(first=ids[0], last=ids[-1], ended=time.time(),
                                       lines=[(line[0], line[1]) for line in convo], ids=ids)
            loop.run_until_complete(learning.learn_from(cv))
        kept = [r["text"] for r in learning.memories(None)]
        hit = [p for p in case.get("expect", []) if any(re.search(p, k, re.IGNORECASE) for k in kept)]
        bad = [k for k in kept if any(re.search(p, k, re.IGNORECASE) for p in case.get("forbid", []))]
        right = case.get("expect", []) + case.get("accept", [])          # accept: right but not required
        good = [k for k in kept if any(re.search(p, k, re.IGNORECASE) for p in right)]
        expected += len(case.get("expect", []))
        found += len(hit)
        forbidden += len(bad)
        kept_total += len(kept)
        kept_expected += len(good)
        missing = [p for p in case.get("expect", []) if p not in hit]
        extra = [k for k in kept if k not in good and k not in bad]
        ok = not bad and not missing
        print(f"{i:3d}. {'PASS' if ok else 'FAIL'}  {case['id']:<22}"
              + (f"  forbidden: {bad}" if bad else "") + (f"  missing: {missing}" if missing else "")
              + (f"  extra: {extra}" if extra else ""))
        if verbose:
            for k in kept:
                print(f"        kept: {k}")
        if not ok:
            failures.append(case["id"])
    learning.DB_OVERRIDE = None
    recall = found / expected if expected else 1.0
    precision = kept_expected / kept_total if kept_total else 1.0
    print(f"\nLearning: {len(cases) - len(failures)}/{len(cases)} cases; forbidden memories {forbidden}; "
          f"recall {recall:.0%} ({found}/{expected}); precision {precision:.0%} ({kept_expected}/{kept_total}) "
          f"({time.monotonic() - t0:.0f} s)")
    return 0 if not failures else 2
