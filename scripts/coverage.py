"""Runs real conversions and records what actually works, in docs/COVERAGE.md.

    PYTHONPATH=. python scripts/coverage.py --samples DIR [--out docs/COVERAGE.md] [--bad worker/known_bad_routes.json]
        [--all --state FILE --pairs FILE]

For every sample, it asks the running service what the sample can become and tries it. Every target is
tried from at least one source; the rest of the routes are sampled so this finishes in reasonable
time. A target that fails from every source it was tried from is written to the bad routes list, which
the image build drops from the catalog. Numbers in the report are counts of real conversions.

With --all it runs every route from every sample instead, which takes hours. Each result goes to the
--state file as it happens, so a run that's stopped picks up where it left off. A conversion that
fails is tried once more before it counts, and each one that still fails is written to --pairs, the
list of single conversions the image build blocks (worker/known_bad_pairs.json).
"""

from __future__ import annotations

import argparse
import json
import platform
import random
import time
from collections import Counter, defaultdict
from pathlib import Path

from tests.client import Client

MAX_ROUTES_PER_SOURCE = 12


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", required=True)
    parser.add_argument("--out", default="docs/COVERAGE.md")
    parser.add_argument("--bad", default="worker/known_bad_routes.json")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--limit", type=int, default=0, help="stop after this many conversions")
    parser.add_argument("--progress", default="", help="write running totals here")
    parser.add_argument("--all", action="store_true", help="run every route from every sample")
    parser.add_argument("--state", default="", help="results so far, one JSON line each; resumes from it")
    parser.add_argument("--pairs", default="", help="write the (source, target) pairs that failed here")
    args = parser.parse_args()

    client = Client()
    info = client.http.get("/api/formats").json()
    formats = {f["id"]: f for f in info["formats"]}
    samples: dict[str, Path] = {}
    for path in sorted(Path(args.samples).glob("sample.*")):
        ext = path.name[len("sample."):]
        samples[ext] = path
    print(f"{len(formats)} formats in the catalog, {len(samples)} samples", flush=True)

    plan: list[tuple[str, dict]] = []
    all_routes: dict[str, list[dict]] = {}
    targets_seen: set[str] = set()
    routes_total = 0
    rng = random.Random(1)
    for source in samples:
        known = source if source in formats else "unknown"
        routes = client.http.get("/api/routes", params={"from": known}).json()["routes"]
        all_routes[source] = routes
        routes_total += len(routes)

    # First make sure every target gets tried, from whichever source reaches it most directly.
    by_target: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    for source, routes in all_routes.items():
        for route in routes:
            by_target[route["to"]].append((source, route))
    planned = set()
    for target, options in sorted(by_target.items()):
        options.sort(key=lambda item: (len(item[1]["steps"]), item[0]))
        for source, route in options[:2]:
            planned.add((source, target))
            plan.append((source, route))
    # Then give every source a spread of targets.
    for source, routes in all_routes.items():
        rng.shuffle(routes)
        chosen = 0
        for route in sorted(routes, key=lambda r: len(r["steps"])):
            if chosen >= MAX_ROUTES_PER_SOURCE:
                break
            if (source, route["to"]) in planned:
                chosen += 1
                continue
            planned.add((source, route["to"]))
            plan.append((source, route))
            chosen += 1
    if args.all:
        plan = [(source, route) for source, routes in all_routes.items() for route in routes]
    if args.limit:
        plan = plan[: args.limit]
    print(f"{len(plan)} conversions planned out of {routes_total} possible from these samples", flush=True)

    # Results already recorded by a run that was stopped, keyed by (sample, target).
    done: dict[tuple[str, str], dict] = {}
    if args.state and Path(args.state).exists():
        for line in Path(args.state).read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
            except ValueError:
                continue  # the last line of a run cut off mid-write
            done[(item["sample"], item["to"])] = item
        print(f"{len(done)} already done, from {args.state}", flush=True)
    state = open(args.state, "a", encoding="utf-8") if args.state else None

    def attempt(source: str, route: dict) -> tuple[str, str]:
        data = samples[source].read_bytes()
        hint = source if source in formats else None
        view, output = client.convert(data, route["to"], samples[source].name, hint, timeout=args.timeout)
        error = str(view.get("error") or view.get("state"))[:110]
        if error.startswith("No readable text"):
            # A correct answer: an MP3 has no text in it. Not a pass, and not a broken route.
            return "no_text", error
        return ("ok", "") if view["state"] == "done" and output else ("bad", error)

    started = time.monotonic()
    ran = 0
    results: list[dict] = []
    for index, (source, route) in enumerate(plan, 1):
        item = done.get((source, route["to"]))
        if item is None:
            outcome, error = attempt(source, route)
            retried = False
            if outcome == "bad":
                # Once more, so a one-off timeout on a busy machine doesn't block a conversion for good.
                outcome, error = attempt(source, route)
                retried = True
            item = {"sample": source, "source": source if source in formats else "unknown", "to": route["to"],
                    "engines": [step["engine"] for step in route["steps"]], "grade": route["grade"],
                    "outcome": outcome, "error": error, "retried": retried}
            if state:
                state.write(json.dumps(item) + "\n")
                state.flush()
            ran += 1
        results.append(item)
        if index % 25 == 0 or index == len(plan):
            bad_so_far = sum(1 for r in results if r["outcome"] == "bad")
            line = f"{index}/{len(plan)}: {len(results) - bad_so_far} ok or no text, {bad_so_far} failed, {ran} run now, {time.monotonic() - started:.0f}s"
            print(line, flush=True)
            if args.progress:
                Path(args.progress).write_text(line + "\n")
    if state:
        state.close()

    passed = failed = no_text = 0
    target_results: dict[str, Counter] = defaultdict(Counter)
    pair_results: dict[tuple[str, str], Counter] = defaultdict(Counter)
    engine_results: dict[str, Counter] = defaultdict(Counter)
    failures: list[tuple[str, str, str, str]] = []
    grades: Counter = Counter()
    failed_pairs: set[tuple[str, str]] = set()
    for item in results:
        engines = item["engines"]
        if item["outcome"] == "no_text":
            no_text += 1
            continue
        key = "ok" if item["outcome"] == "ok" else "bad"
        target_results[item["to"]][key] += 1
        pair_results[(engines[-1], item["to"])][key] += 1
        for engine in engines:
            engine_results[engine][key] += 1
        if key == "ok":
            passed += 1
            grades[item["grade"]] += 1
        else:
            failed += 1
            failed_pairs.add((item["source"], item["to"]))
            failures.append((item["sample"], item["to"], " > ".join(engines), item["error"]))
    if args.pairs:
        lines = ",\n".join(json.dumps(list(pair)) for pair in sorted(failed_pairs))
        Path(args.pairs).write_text(f"[\n{lines}\n]\n" if lines else "[]\n", encoding="utf-8")

    # A target that never worked, from any source, comes out of the catalog.
    bad_targets = sorted(t for t, c in target_results.items() if c["ok"] == 0 and c["bad"] > 0)
    # So does one engine's route to a target that only ever works through another engine. The list
    # only grows: a route dropped here isn't tried next time, so it mustn't come back just for that.
    bad_routes = set()
    if Path(args.bad).exists():
        bad_routes = {tuple(item) for item in json.loads(Path(args.bad).read_text(encoding="utf-8"))}
    for target in bad_targets:
        for _source, route in by_target[target]:
            bad_routes.add((route["steps"][-1]["engine"], target))
    bad_routes.update(pair for pair, c in pair_results.items() if c["ok"] == 0 and c["bad"] > 0)
    lines = ",\n".join(json.dumps(list(pair)) for pair in sorted(bad_routes))
    Path(args.bad).write_text(f"[\n{lines}\n]\n", encoding="utf-8")

    readable = {fid for fid, routes in ((s if s in formats else "unknown", r) for s, r in all_routes.items()) if fid != "unknown"}
    worked_targets = sum(1 for c in target_results.values() if c["ok"])
    worked_sources = len({s for s in samples if s in formats} - {f[0] for f in failures if False})
    lines = [
        "# Format coverage",
        "",
        "Generated by `scripts/coverage.py` from real conversions on the running service. Regenerate it after",
        "changing engines.",
        "",
        f"- **Run on:** {platform.machine()}, job isolation: {info['server'].get('isolation')}",
        f"- **Formats in the catalog:** {len(formats)}",
        f"- **Sample files made:** {len(samples)}. Sources with samples: {len(readable)}.",
        f"- **Different routes possible from those samples:** {routes_total}",
        f"- **Conversions actually run:** {len(plan)}. Passed: {passed}. Failed: {failed}. Correctly reported no text to extract: {no_text}.",
        f"- **Target formats tried:** {len(target_results)}. Worked at least once: {worked_targets}. Never worked, and so removed from the catalog: {len(bad_targets)}.",
        "",
        "Formats with no sample here are offered because the tool that handles them says it supports them, and",
        "haven't been run in this test. A rare format the tool lists can still fail on a real file.",
        "",
        "A conversion that passed produced a non-empty file that passed the type check. About 50 common targets",
        "are checked for their exact type. For the rest, the check only catches output that's plainly some",
        "other common format. Nobody looked at the content, so this says the conversion works, not that the",
        "result looks perfect.",
        "",
        "## By grade, of the conversions that passed",
        "",
        "| Grade | Count |",
        "|---|---|",
        *[f"| {grade} | {grades[grade]} |" for grade in ("exact", "lossy", "extract", "package")],
        "",
        "## By engine",
        "",
        "| Engine | Passed | Failed |",
        "|---|---|---|",
        *[f"| {name} | {c['ok']} | {c['bad']} |" for name, c in sorted(engine_results.items())],
        "",
    ]
    if bad_targets:
        lines += ["## Removed from the catalog: never worked", "", ", ".join(f"`{t}`" for t in bad_targets), ""]
    if failures:
        lines += ["## Failures", "", "| From | To | Route | Message |", "|---|---|---|---|"]
        lines += [f"| `{s}` | `{t}` | {e} | {m} |" for s, t, e, m in failures[:400]]
        lines.append("")
    Path(args.out).write_text("\n".join(lines), encoding="utf-8")
    print(f"Done: {passed} passed, {failed} failed. {len(bad_targets)} targets never worked.", flush=True)


if __name__ == "__main__":
    main()
