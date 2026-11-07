"""Runs once at image build. Asks each installed engine what it can do and writes the catalog.

    catalog.json      formats and engine groups; the worker publishes this to the gateway
    engine_data.json  what each engine learned here, such as coder names; stays in the worker
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from worker.engines import all_engines
from worker.formats import FAMILIES, FormatDB


def build(skip_path: Path | None = None, pairs_path: Path | None = None) -> tuple[dict, dict]:
    db = FormatDB()
    groups: list[dict] = []
    data: dict[str, dict] = {}
    # Routes that the coverage matrix found broken. They are dropped rather than offered.
    skip = set()
    if skip_path and skip_path.exists():
        skip = {tuple(item) for item in json.loads(skip_path.read_text(encoding="utf-8"))}

    for engine in all_engines():
        if not engine.available():
            print(f"  skip {engine.name}: {engine.binary} isn't installed", file=sys.stderr)
            continue
        try:
            found = engine.describe(db)
        except Exception as err:
            print(f"  skip {engine.name}: {err}", file=sys.stderr)
            continue
        for grp in found:
            if skip:
                grp["writes"] = [w for w in grp["writes"] if (engine.name, w) not in skip]
        found = [grp for grp in found if grp["reads"] and grp["writes"]]
        if not found:
            print(f"  skip {engine.name}: nothing usable", file=sys.stderr)
            continue
        groups.extend(found)
        data[engine.name] = engine.export_data()
        reads = {r for grp in found for r in grp["reads"]}
        writes = {w for grp in found for w in grp["writes"]}
        print(f"  {engine.name}: reads {len(reads)}, writes {len(writes)}", file=sys.stderr)

    used = {fid for grp in groups for fid in [*grp["reads"], *grp["writes"]] if fid != "*"}
    # Single conversions that failed when every route was run for real. The router never offers them.
    blocked = []
    if pairs_path and pairs_path.exists():
        blocked = sorted({(src, dst) for src, dst in json.loads(pairs_path.read_text(encoding="utf-8"))
                          if src in used | {"unknown"} and dst in used})
    catalog = {
        "families": FAMILIES,
        "formats": [fmt for fmt in db.export() if fmt["id"] in used],
        "groups": groups,
        "blocked": [list(pair) for pair in blocked],
    }
    print(f"  blocked conversions: {len(blocked)}", file=sys.stderr)
    return catalog, data


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="/app")
    parser.add_argument("--skip", default="/app/worker/known_bad_routes.json")
    parser.add_argument("--blocked", default="/app/worker/known_bad_pairs.json")
    args = parser.parse_args()
    catalog, data = build(Path(args.skip), Path(args.blocked))
    out = Path(args.out)
    (out / "catalog.json").write_text(json.dumps(catalog, separators=(",", ":")), encoding="utf-8")
    (out / "engine_data.json").write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    print(f"catalog: {len(catalog['formats'])} formats, {len(catalog['groups'])} groups, {len(data)} engines", file=sys.stderr)


if __name__ == "__main__":
    main()
