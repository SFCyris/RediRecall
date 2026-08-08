#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Mutation sweep — the gate that decides whether tests/ is worth anything.

Install as  tests/mutation_sweep.py  next to  tests/mutations.json .

    venv/bin/python3 tests/mutation_sweep.py                 # whole catalogue
    venv/bin/python3 tests/mutation_sweep.py -k lane         # ids matching /lane/
    venv/bin/python3 tests/mutation_sweep.py -j 4            # 4 labs in parallel
    venv/bin/python3 tests/mutation_sweep.py --check         # catalogue freshness only

For each entry it copies the repo to a throwaway lab, applies exactly one edit,
runs the suite, and records which tests noticed.

    KILLED    at least one test failed          -> that defect is guarded
    SURVIVED  the whole suite stayed green      -> the defect can ship again
    STALE     the `find` text is gone           -> the catalogue lies; fix it

A SURVIVED row is a bug in tests/, not in the catalogue. Exit code is 1 if any
mutation survives or any entry is stale, so CI can gate on it.

Safety: the lab is a copy under $TMPDIR; venv/ .git/ are symlinked or skipped and
the real working tree is never written to. The suite's own Redis namespacing
(__rrtest_<pid>__) still applies, so no FLUSHDB and no touching db 0's real keys.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CATALOGUE = Path(__file__).with_name("mutations.json")
PYTHON = ROOT / "venv" / "bin" / "python3"
SKIP = shutil.ignore_patterns("venv", ".git", ".pytest_cache", "__pycache__",
                              "screenshots", "node_modules", ".venv")
_FAIL = re.compile(r"^(?:FAILED|ERROR) (\S+)", re.M)


def _apply(text: str, mut: dict) -> tuple[str, int]:
    """Apply every (find, replace) pair in `mut`. Returns (new_text, sites)."""
    pairs = [(mut["find"], mut["replace"])]
    pairs += [(e["find"], e["replace"]) for e in mut.get("also", [])]
    sites = 0
    for find, repl in pairs:
        n = text.count(find)
        if n == 0:
            return text, 0
        sites += n
        text = text.replace(find, repl)
    return text, sites


def run_one(mut: dict, timeout: int) -> dict:
    lab = Path(tempfile.mkdtemp(prefix="rr-mutsweep-"))
    try:
        work = lab / "repo"
        shutil.copytree(ROOT, work, symlinks=True, ignore=SKIP)
        os.symlink(ROOT / "venv", work / "venv")
        target = work / mut["file"]
        new, sites = _apply(target.read_text(encoding="utf-8"), mut)
        if sites == 0:
            return {**mut, "verdict": "STALE", "sites": 0, "killers": [],
                    "secs": 0.0, "returncode": None, "skipped": 0}
        # A declared `sites` count that no longer matches the tree means the `find`
        # has drifted onto more (or fewer) occurrences than the catalogue vouches
        # for — the exact shape that let M31 "kill" by rewriting a prose comment
        # rather than the directive. Refuse to score it; fix the catalogue.
        if mut.get("sites") is not None and sites != mut["sites"]:
            return {**mut, "verdict": "STALE", "sites": sites, "killers": [],
                    "secs": 0.0, "returncode": None, "skipped": 0,
                    "stale_reason": f"declared sites={mut['sites']} but found {sites}"}
        target.write_text(new, encoding="utf-8")
        t0 = time.time()
        proc = subprocess.run(
            [str(PYTHON), "-m", "pytest", "tests/", "-q", "-p", "no:cacheprovider",
             "--tb=no", "-rfE", "--deselect", "tests/mutation_sweep.py"],
            cwd=work, capture_output=True, text=True, timeout=timeout)
        secs = round(time.time() - t0, 1)
        killers = sorted({m.group(1).split("::")[-1] for m in _FAIL.finditer(proc.stdout)})
        rc = proc.returncode
        skipped = int(next(iter(re.findall(r"(\d+) skipped", proc.stdout)), 0))
        # The verdict is derived from BOTH the killer lines AND the return code.
        # -rfE (not -rf) makes ERROR-class failures — collection/fixture errors,
        # a broken import — emit a summary line the killer regex can see. And a red
        # suite (rc not in {0 = all-passed, 5 = no-tests-collected}) with no named
        # failure is RED-NO-KILLER, never SURVIVED: pytest can exit non-zero with no
        # `^ERROR nodeid` line (internal/usage error), and scraping stdout alone
        # scored that as a survivor.
        verdict = ("KILLED" if killers
                   else "RED-NO-KILLER" if rc not in (0, 5)
                   else "SURVIVED")
        # The `expect` field is a contract. If the ONLY tests that went red are
        # outside it, the catalogue is crediting the kill to a test that did not do
        # it (e.g. a bare-rename mutation caught by an unrelated identifier grep).
        # Params are stripped so expect "test_foo" matches killer "test_foo[bar]".
        want = {t.split("[")[0] for t in
                re.findall(r"test_[A-Za-z0-9_]+(?:\[[^\]]+\])?", mut.get("expect", ""))}
        if verdict == "KILLED" and want and not (want & {k.split("[")[0] for k in killers}):
            verdict = "MISATTRIBUTED"
        return {**mut, "verdict": verdict, "sites": sites, "killers": killers,
                "returncode": rc, "skipped": skipped, "secs": secs}
    except subprocess.TimeoutExpired:
        # A hang is a kill too: the mutation made the suite non-terminating.
        return {**mut, "verdict": "KILLED", "sites": -1, "killers": ["<suite timed out>"],
                "secs": float(timeout), "returncode": None, "skipped": 0}
    finally:
        shutil.rmtree(lab, ignore_errors=True)


def check_only(muts: list[dict]) -> int:
    """Fast freshness pass: every `find` must still be present in the tree.

    Also flags entries whose `replace` still contains `find`. That is legitimate
    for a mutation that WRAPS code (M14, M63 …) but is the exact shape of a
    self-defeating edit for one that means to delete it — `tickInterval` ->
    `xtickIntervalx` still satisfies a test grepping for `tickInterval`, which
    produced four false survivors while this catalogue was being written. The
    verdict for a flagged entry is only trustworthy once it has been KILLED.
    """
    bad = 0
    for mut in muts:
        text = (ROOT / mut["file"]).read_text(encoding="utf-8")
        pairs = [mut] + list(mut.get("also", []))
        missing = [p["find"] for p in pairs if p["find"] not in text]
        if missing:
            bad += 1
            print(f"STALE {mut['id']}: {len(missing)} pattern(s) no longer in "
                  f"{mut['file']}")
            continue
        for p in pairs:
            got = text.count(p["find"])
            want = p.get("sites")
            if want is not None and got != want:
                bad += 1
                print(f"STALE {mut['id']}: {p['find']!r} matches {got} site(s), "
                      f"catalogue says {want}")
            elif want is None and got > 1:
                print(f"note  {mut['id']}: {p['find']!r} applies at {got} sites — "
                      f"pin it with \"sites\": {got} so a kill via one of them is caught")
        if any(p["find"] and p["find"] in p["replace"] for p in pairs):
            print(f"note  {mut['id']}: replacement retains the pattern (wrapping "
                  f"mutation — confirm it is KILLED, not merely green)")
    print(f"{len(muts)} entries checked, {bad} stale")
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="run the mutation sweep")
    ap.add_argument("-k", metavar="REGEX", help="only ids matching this regex")
    ap.add_argument("-j", type=int, default=1, help="parallel labs (default 1)")
    ap.add_argument("--timeout", type=int, default=1800, help="per-run seconds")
    ap.add_argument("--check", action="store_true", help="freshness pass only")
    ap.add_argument("--json", metavar="PATH", help="write full results here")
    ap.add_argument("--catalogue", default=str(CATALOGUE),
                    help="mutation catalogue to sweep (default tests/mutations.json)")
    args = ap.parse_args()

    muts = json.loads(Path(args.catalogue).read_text(encoding="utf-8"))
    if args.k:
        rx = re.compile(args.k)
        muts = [m for m in muts if rx.search(m["id"])]
    if not muts:
        print("no mutations selected")
        return 1
    if args.check:
        return check_only(muts)

    print(f"baseline: the suite must be green before a sweep means anything")
    base = subprocess.run([str(PYTHON), "-m", "pytest", "tests/", "-q",
                           "-p", "no:cacheprovider", "--tb=no"],
                          cwd=ROOT, capture_output=True, text=True, timeout=args.timeout)
    if base.returncode != 0:
        print(base.stdout[-2000:])
        print("ABORT: tests/ is already red; fix that first.")
        return 1
    base_skipped = int(next(iter(re.findall(r"(\d+) skipped", base.stdout)), 0))
    print(f"baseline green ({base_skipped} skipped) — sweeping {len(muts)} "
          f"mutations, {args.j} lab(s)\n")

    done, results = 0, []
    _TAG = {"KILLED": "kill", "SURVIVED": "SURVIVED", "STALE": "STALE",
            "RED-NO-KILLER": "RED-NOKILL", "MISATTRIBUTED": "MISATTRIB"}
    with ThreadPoolExecutor(max_workers=args.j) as pool:
        for res in pool.map(lambda m: run_one(m, args.timeout), muts):
            done += 1
            results.append(res)
            tag = _TAG[res["verdict"]]
            skipnote = ("" if res.get("skipped", 0) <= base_skipped
                        else f"  [!{res['skipped']} skipped]")
            print(f"[{done}/{len(muts)}] {tag:10s} {res['id']:44s} "
                  f"{res['secs']}s  {', '.join(res['killers'])}{skipnote}", flush=True)

    surv = [r for r in results if r["verdict"] == "SURVIVED"]
    stale = [r for r in results if r["verdict"] == "STALE"]
    rednokill = [r for r in results if r["verdict"] == "RED-NO-KILLER"]
    misattr = [r for r in results if r["verdict"] == "MISATTRIBUTED"]
    # A mutation whose expect-test SKIPPED (no Redis, no node) ran a DIFFERENT suite
    # than the catalogue vouches for; its verdict is not trustworthy.
    overskipped = [r for r in results if r.get("skipped", 0) > base_skipped]
    killed = len(results) - len(surv) - len(stale) - len(rednokill) - len(misattr)
    scored = max(1, len(results) - len(stale))
    print("\n" + "=" * 78)
    print(f"{len(results)} mutations | KILLED {killed} | SURVIVED {len(surv)} | "
          f"RED-NO-KILLER {len(rednokill)} | MISATTRIBUTED {len(misattr)} | "
          f"STALE {len(stale)}  ->  score {killed / scored:.0%}")
    print("=" * 78)
    for r in surv:
        print(f"\nSURVIVED {r['id']}")
        print(f"  defect that shipped : {r['defect']}")
        print(f"  test that claims it : {r['expect']}")
    for r in rednokill:
        print(f"\nRED-NO-KILLER {r['id']}  — suite exited {r.get('returncode')} with no "
              f"named failure (collection/fixture/internal error); NOT a survivor")
    for r in misattr:
        print(f"\nMISATTRIBUTED {r['id']}")
        print(f"  expect names        : {r['expect']}")
        print(f"  but the killers were: {', '.join(r['killers']) or '(none)'}")
    for r in stale:
        why = r.get("stale_reason", f"`find` text no longer in {r['file']}")
        print(f"\nSTALE    {r['id']}  — {why}")
    for r in overskipped:
        print(f"\nWARNING  {r['id']}: {r['skipped']} tests skipped (baseline "
              f"{base_skipped}) — its expect-test may not have run at all")

    # Every test that never went red across the whole sweep. A test in this list
    # has not been shown capable of failing; it is not coverage until it is.
    if not args.k:
        noticed = {k for r in results for k in r["killers"]}
        listed = subprocess.run([str(PYTHON), "-m", "pytest", "tests/", "-q",
                                 "--collect-only", "-p", "no:cacheprovider"],
                                cwd=ROOT, capture_output=True, text=True)
        allt = sorted({l.split("::")[-1].strip() for l in listed.stdout.splitlines()
                       if "::" in l})
        never = [t for t in allt if t not in noticed]
        print(f"\n{len(never)}/{len(allt)} tests never failed anywhere in this sweep:")
        for t in never:
            print("   ", t)

    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=1), encoding="utf-8")
    return 1 if (surv or stale or rednokill or misattr) else 0


if __name__ == "__main__":
    sys.exit(main())
