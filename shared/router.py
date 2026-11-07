"""Finds how to get from one format to another, chaining engines when no single one can do it.

The catalog stores groups instead of individual pairs, because one engine can do tens of thousands
of them:

    {"engine": "FFmpeg", "reads": ["mp4", ...], "writes": ["mkv", ...], "grade": "auto"}

Grades, from best to worst:
    exact    everything comes across
    lossy    some quality or layout is lost
    extract  only part comes across (the text, the sound, the details)
    package  the file is wrapped unchanged

A group's grade may be "auto": lossy when the target format is itself lossy, otherwise exact.

A file is only offered formats of its own kind: a picture never becomes sound, a song never becomes a
picture, and a page never becomes a picture or the other way round. Every step of a route has to stay
inside a kind the source belongs to, so a detour through another kind can't sneak one in either. The
exceptions are video, which can also become pictures (a GIF, or frames), and the fallbacks every file
gets: packaging, base64 or hex, and its details. Tracing a picture into an SVG counts as a picture.
"""

from __future__ import annotations

import re

GRADES = ("exact", "lossy", "extract", "package")
GRADE_RANK = {grade: rank for rank, grade in enumerate(GRADES)}
# A route with more steps or worse grades costs more. Lossy is priced so a clean two-step route
# beats a lossy direct one.
STEP_COST = {"exact": 1.0, "lossy": 3.0, "extract": 8.0, "package": 12.0}
MAX_HOPS = 3
UNKNOWN = "unknown"

_ID = re.compile(r"^[a-z0-9][a-z0-9._+-]{0,31}$")
MAX_FORMATS = 20000
MAX_GROUPS = 4000
MAX_BLOCKED = 100000

# What kind of thing each family holds. Spreadsheets are both documents and tables, so CSV can become
# XLSX or JSON, and XLSX can become a PDF, while JSON still can't.
KINDS_BY_FAMILY = {
    "document": {"documents"}, "slides": {"documents"}, "ebook": {"documents"}, "mail": {"documents"},
    "spreadsheet": {"documents", "tables"}, "data": {"tables"},
    "image": {"pictures"}, "raw": {"pictures"}, "vector": {"pictures"}, "diagram": {"pictures"},
    "medical": {"pictures"},  # a DICOM file is an image
    "audio": {"sound"}, "video": {"sound"}, "media": {"sound"},
    "subtitle": {"subtitles"},
    "archive": {"archives"}, "disk": {"archives"},
    "model": {"3d"}, "cad": {"cad"}, "map": {"maps"}, "font": {"fonts"}, "science": {"science"},
}
# The "other" family is a mix, so its members are placed one by one. Any other format with no known
# kind gets one of its own, which leaves it only the fallbacks.
KINDS_BY_FORMAT = {
    "8svx": {"sound"}, "cdda": {"sound"}, "cvs": {"sound"}, "maud": {"sound"}, "prc-audio": {"sound"},
    "mw": {"documents"}, "cwk": {"documents"}, "wri": {"documents"}, "dvi": {"documents"},
    "qpw": {"documents", "tables"},
    # Outlook's winmail.dat holds attachments, which can come out as an archive.
    "tnef": {"documents", "archives"},
}
# Video can also become pictures: an animated GIF, or frames. A raw video stream counts as video.
VIDEO_STREAMS = {"f4v", "dv", "nut", "h264", "hevc"}
VIDEO_EXTRA = {"pictures"}
# Every file can be packaged, encoded or have its details pulled out, whatever its kind.
FALLBACK_FAMILIES = {"archive", "encoded"}
# Reading text out of pictures is left out by choice: it guesses, and a wrong guess looks right.
NEVER_ENGINES = {"Tesseract", "OCRmyPDF"}
NEVER_TARGETS = {"pdfocr"}
TRACERS = {"VTracer", "Potrace"}


class Catalog:
    def __init__(self, data: dict):
        formats = data["formats"]
        groups = data["groups"]
        if len(formats) > MAX_FORMATS or len(groups) > MAX_GROUPS:
            raise ValueError("catalog too large")

        self.families: dict[str, str] = {str(k): str(v)[:60] for k, v in data.get("families", {}).items()}
        self.formats: dict[str, dict] = {}
        for fmt in formats:
            fid = fmt["id"]
            if not _ID.match(fid):
                raise ValueError(f"bad format id {fid!r}")
            self.formats[fid] = {
                "id": fid,
                "name": str(fmt.get("name", fid))[:80],
                "family": str(fmt.get("family", "other"))[:32],
                "exts": [str(e)[:32] for e in fmt.get("exts", [])][:32],
                "lossy": bool(fmt.get("lossy", False)),
                "out_ext": str(fmt.get("out_ext", ""))[:32] or None,
            }
        self.formats.setdefault(UNKNOWN, {
            "id": UNKNOWN, "name": "Unknown file", "family": "other", "exts": [], "lossy": False, "out_ext": None,
        })

        self.kinds: dict[str, frozenset[str]] = {
            fid: frozenset(KINDS_BY_FORMAT.get(fid) or KINDS_BY_FAMILY.get(fmt["family"]) or {f"own:{fid}"})
            for fid, fmt in self.formats.items()
        }

        self.by_ext: dict[str, str] = {}
        for fmt in self.formats.values():
            for ext in fmt["exts"]:
                self.by_ext.setdefault(ext.lower(), fmt["id"])

        self.groups: list[dict] = []
        self.by_read: dict[str, list[int]] = {}
        self.universal: list[int] = []
        self.writable: set[str] = set()
        for index, raw in enumerate(groups):
            grade = raw.get("grade", "auto")
            if grade != "auto" and grade not in GRADE_RANK:
                raise ValueError(f"bad grade {grade!r}")
            group = {
                "engine": str(raw["engine"])[:40],
                "reads": [r for r in raw["reads"] if r == "*" or r in self.formats],
                "writes": [w for w in raw["writes"] if w in self.formats],
                "grade": grade,
                # Ends a route: packaging, or anything whose result can't feed another engine.
                "terminal": bool(raw.get("terminal", False)),
                "priority": float(raw.get("priority", 0.0)),
            }
            self.groups.append(group)
            self.writable.update(group["writes"])
            if "*" in group["reads"]:
                self.universal.append(index)
            for fid in group["reads"]:
                if fid != "*":
                    self.by_read.setdefault(fid, []).append(index)

        # Conversions the coverage matrix found failing, as (source, target). Never offered.
        blocked = data.get("blocked", [])
        if len(blocked) > MAX_BLOCKED:
            raise ValueError("blocked list too large")
        self.blocked: set[tuple[str, str]] = set()
        for pair in blocked:
            src, dst = pair
            if not (isinstance(src, str) and isinstance(dst, str) and _ID.match(src) and _ID.match(dst)):
                raise ValueError("bad blocked pair")
            self.blocked.add((src, dst))

    def format_for_name(self, filename: str) -> str | None:
        """The format a file's name suggests. Only a hint: the worker checks the contents."""
        lower = filename.lower()
        for marker in (".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst"):
            if lower.endswith(marker):
                return self.by_ext.get(marker[1:])
        _stem, dot, ext = lower.rpartition(".")
        return self.by_ext.get(ext) if dot else None

    def is_writable(self, fid: str) -> bool:
        return fid in self.writable

    def public_formats(self) -> list[dict]:
        return [
            {"id": f["id"], "name": f["name"], "family": f["family"], "exts": f["exts"]}
            for f in self.formats.values()
            if f["id"] != UNKNOWN
        ]

    def reach(self, source: str) -> frozenset[str]:
        """The kinds a file of this format may become."""
        kinds = self.kinds[source]
        if self.formats[source]["family"] == "video" or source in VIDEO_STREAMS:
            kinds = kinds | VIDEO_EXTRA
        return kinds

    def _allowed(self, group: dict, dst: str, reach: frozenset[str], universal: bool) -> bool:
        if group["engine"] in NEVER_ENGINES or dst in NEVER_TARGETS:
            return False
        if universal and self.formats[dst]["family"] in FALLBACK_FAMILIES:
            return True
        return bool(self.kinds[dst] & reach)

    def step_grade(self, group: dict, dst: str) -> str:
        if group["grade"] != "auto":
            return group["grade"]
        return "lossy" if self.formats[dst]["lossy"] else "exact"

    def _search(self, source: str) -> dict[str, tuple[float, list[tuple[int, str, str, str]]]]:
        """Cheapest path to every reachable format within MAX_HOPS."""
        best: dict[str, tuple[float, list]] = {source: (0.0, [])}
        reach = self.reach(source)
        universal = set(self.universal)
        frontier = {source}
        for _hop in range(MAX_HOPS):
            changed: set[str] = set()
            # Sorted, so routes that cost the same are settled the same way in every process. A set's
            # order changes with each start, and the gateway, the worker and the tests must all agree.
            for node in sorted(frontier):
                cost, path = best[node]
                # A node can be given a longer, cheaper path earlier in this same hop. It mustn't
                # then be extended past the limit.
                if len(path) >= MAX_HOPS:
                    continue
                # Packaging and detail extraction apply to anything, but only as a first step from
                # the original file: a "PDF of what Tika could read from a zip" helps nobody.
                candidates = list(self.by_read.get(node, []))
                if not path:
                    candidates += self.universal
                for index in candidates:
                    group = self.groups[index]
                    for dst in group["writes"]:
                        if dst == node or dst == source:
                            continue
                        if not self._allowed(group, dst, reach, index in universal):
                            continue
                        grade = self.step_grade(group, dst)
                        new_cost = cost + STEP_COST[grade] + group["priority"]
                        if new_cost < best.get(dst, (float("inf"), None))[0]:
                            step = (index, node, dst, grade)
                            best[dst] = (new_cost, [*path, step])
                            if not group["terminal"]:
                                changed.add(dst)
                            else:
                                changed.discard(dst)
            frontier = changed
            if not frontier:
                break
        best.pop(source, None)
        return best

    def routes_from(self, source: str) -> list[dict]:
        if source not in self.formats:
            source = UNKNOWN
        routes = []
        for dst, (_cost, path) in self._search(source).items():
            if (source, dst) not in self.blocked:
                routes.append(self._describe(source, dst, path))
        return routes

    def route(self, source: str, target: str) -> dict | None:
        if source not in self.formats:
            source = UNKNOWN
        if (source, target) in self.blocked:
            return None
        found = self._search(source).get(target)
        return self._describe(source, target, found[1]) if found else None

    def _describe(self, source: str, dst: str, path: list) -> dict:
        worst = max((step[3] for step in path), key=GRADE_RANK.__getitem__)
        fmt = self.formats[dst]
        engines = {self.groups[i]["engine"] for i, _a, _b, _g in path}
        note = None
        if engines & TRACERS:
            note = "Traced outlines"
        elif "pictures" in self.kinds[dst] and "pictures" not in self.kinds[source]:
            note = "Pictures from the video"
        return {
            "to": dst,
            "name": fmt["name"],
            "family": fmt["family"],
            "grade": worst,
            "note": note,
            "steps": [{"engine": self.groups[i]["engine"], "from": a, "to": b} for i, a, b, _g in path],
        }
