"""Which conversions a file is offered: only its own kind, plus the fallbacks every file gets."""

import pytest

from shared.router import MAX_HOPS, Catalog

FAMILIES = {
    "jpg": "image", "png": "image", "pgm": "image", "gif": "image", "apng": "image",
    "svg": "vector", "eps": "vector", "dcm": "medical",
    "pdf": "document", "docx": "document", "odt": "document", "txt": "document", "ps": "document",
    "xlsx": "spreadsheet", "csv": "spreadsheet", "json": "data",
    "mp3": "audio", "wav": "audio", "mp4": "video", "mkv": "video", "h264": "media",
    "srt": "subtitle", "vtt": "subtitle",
    "dwg": "cad", "dxf": "cad", "stl": "model", "obj": "model",
    "zip": "archive", "iso": "disk", "b64": "encoded", "metadata": "encoded", "pdfocr": "encoded",
    "tnef": "mail", "8svx": "other",
}

GROUPS = [
    ("ImageMagick", ["jpg", "png", "gif"], ["jpg", "png", "pgm", "gif", "eps"], "auto"),
    ("img2pdf", ["jpg"], ["pdf"], "exact"),
    ("Poppler", ["pdf"], ["txt", "jpg", "png", "svg"], "lossy"),
    ("eSpeak NG", ["txt"], ["wav"], "lossy"),
    ("FFmpeg", ["wav", "mp3"], ["wav", "mp3"], "auto"),
    ("FFmpeg", ["mp3", "wav"], ["mp4", "mkv"], "lossy", True),   # the sound alone, in a video container
    ("FFmpeg", ["mp4", "mkv", "h264"], ["mp4", "mkv", "h264"], "auto"),
    ("FFmpeg", ["mp4", "mkv"], ["mp3", "wav"], "extract"),
    ("FFmpeg", ["mp4", "mkv", "h264"], ["gif", "apng"], "lossy", True),
    ("FFmpeg", ["mp4", "mkv", "h264"], ["png", "jpg"], "extract", True),
    ("FFmpeg", ["srt", "vtt"], ["srt", "vtt"], "lossy"),
    ("VTracer", ["png", "jpg"], ["svg"], "lossy"),
    ("Potrace", ["pgm"], ["svg", "eps", "pdf", "dxf"], "lossy"),
    ("Assimp", ["dxf", "stl", "obj"], ["stl", "obj"], "lossy"),
    ("LibreDWG", ["dwg"], ["dxf"], "lossy"),
    ("LibreDWG SVG", ["dwg"], ["svg"], "lossy"),
    ("DCMTK", ["jpg"], ["dcm"], "lossy"),
    ("DCMTK", ["dcm"], ["png"], "lossy"),
    ("DCMTK JSON", ["dcm"], ["json"], "extract"),
    ("LibreOffice", ["docx", "odt"], ["pdf", "odt", "docx", "txt"], "lossy"),
    ("LibreOffice", ["xlsx", "csv"], ["pdf", "xlsx", "csv"], "lossy"),
    ("Tables", ["csv", "xlsx", "json"], ["csv", "xlsx", "json"], "auto"),
    ("Ghostscript", ["ps", "eps"], ["pdf", "png"], "exact"),
    ("Tesseract", ["png", "jpg"], ["txt"], "extract"),
    ("OCRmyPDF", ["pdf"], ["pdfocr"], "exact"),
    ("SoX", ["8svx"], ["wav"], "auto"),
    ("tnef", ["tnef"], ["zip"], "extract"),
    ("Archive tools", ["iso", "zip"], ["zip"], "exact"),
    ("Package", ["*"], ["zip"], "package", True),
    ("Encode", ["*"], ["b64"], "package", True),
    ("ExifTool", ["*"], ["metadata"], "extract", True),
    ("Tika", ["*"], ["txt"], "extract", True),
]


def make(blocked=()):
    return Catalog({
        "formats": [{"id": fid, "name": fid.upper(), "family": fam, "exts": [fid]} for fid, fam in FAMILIES.items()],
        "groups": [{"engine": g[0], "reads": g[1], "writes": g[2], "grade": g[3], "terminal": len(g) > 4 and g[4]} for g in GROUPS],
        "blocked": [list(pair) for pair in blocked],
    })


CAT = make()
FALLBACKS = {"zip", "b64", "metadata"}


def targets(source, cat=CAT):
    return {route["to"] for route in cat.routes_from(source)}


@pytest.mark.parametrize("source, never", [
    ("jpg", {"mp3", "wav", "pdf", "txt", "dxf", "json", "mp4"}),     # no sound, no pages, no OCR, no CAD
    ("pdf", {"jpg", "png", "svg", "mp3", "wav", "stl", "dxf", "pdfocr"}),
    ("mp3", {"gif", "jpg", "png", "h264", "txt", "pdf"}),             # no pictures; no video stream from sound
    ("docx", {"wav", "png", "jpg", "dxf"}),
    ("json", {"pdf", "txt", "docx"}),
    ("dwg", {"svg", "stl", "png"}),
    ("dxf", {"stl", "obj"}),                                          # a 2D drawing isn't a 3D model
    ("dcm", {"json"}),
])
def test_other_kinds_are_never_offered(source, never):
    assert not targets(source) & never


@pytest.mark.parametrize("source, offered", [
    ("mp4", {"gif", "apng", "png", "jpg", "mp3", "wav", "mkv", "h264"}),   # video can become pictures
    ("h264", {"gif", "png"}),                                              # a raw video stream is video
    ("png", {"svg", "jpg", "gif", "eps", "dcm"}),                         # tracing stays a picture
    ("pgm", {"svg", "eps"}),
    ("docx", {"pdf", "odt", "txt"}),
    ("csv", {"xlsx", "json", "pdf"}),                                     # spreadsheets are tables and documents
    ("xlsx", {"pdf", "json"}),
    ("json", {"csv", "xlsx"}),
    ("mp3", {"wav", "mp4", "mkv"}),                                       # the sound alone in a video container
    ("dwg", {"dxf"}),
    ("ps", {"pdf"}),
    ("eps", {"png"}),
    ("srt", {"vtt"}),
    ("8svx", {"wav"}),
    ("tnef", {"zip"}),
    ("iso", {"zip"}),
])
def test_its_own_kind_is_offered(source, offered):
    assert offered <= targets(source)


@pytest.mark.parametrize("source", list(FAMILIES) + ["unknown"])
def test_every_file_keeps_the_fallbacks(source):
    assert FALLBACKS - {source} <= targets(source)


def test_an_unrecognised_file_only_gets_the_fallbacks():
    assert targets("unknown") == FALLBACKS


def test_ocr_is_never_offered():
    for source in FAMILIES:
        for route in CAT.routes_from(source):
            assert {"Tesseract", "OCRmyPDF"}.isdisjoint(step["engine"] for step in route["steps"])
            assert route["to"] != "pdfocr"


def test_no_detour_through_another_kind():
    # Every step stays within what the source may become, not just the last one.
    for source in FAMILIES:
        reach = CAT.reach(source)
        for route in CAT.routes_from(source):
            if CAT.formats[route["to"]]["family"] in ("archive", "encoded") and len(route["steps"]) == 1:
                continue
            for step in route["steps"]:
                assert CAT.kinds[step["to"]] & reach, (source, route)


def test_sound_in_a_video_container_goes_no_further():
    assert CAT.route("mp3", "h264") is None
    assert [s["to"] for s in CAT.route("mp3", "mp4")["steps"]] == ["mp4"]


def test_the_kept_cross_kind_conversions_say_what_you_get():
    assert CAT.route("mp4", "gif")["note"] == "Pictures from the video"
    assert CAT.route("png", "svg")["note"] == "Traced outlines"
    assert CAT.route("png", "jpg")["note"] is None
    assert CAT.route("docx", "pdf")["note"] is None


def test_a_blocked_pair_is_never_offered():
    cat = make(blocked=[("docx", "pdf"), ("unknown", "b64")])
    assert "pdf" not in targets("docx", cat)
    assert cat.route("docx", "pdf") is None
    assert "pdf" in targets("odt", cat)          # only that pair, nothing else
    assert "b64" not in targets("unknown", cat)
    assert "b64" in targets("docx", cat)


@pytest.mark.parametrize("bad", [[["../x", "pdf"]], [["docx"]], [[1, 2]], [["docx", "pdf", "x"]], "nope"])
def test_a_bad_blocked_list_is_refused(bad):
    data = {"formats": [{"id": "docx"}, {"id": "pdf"}], "groups": [], "blocked": bad}
    with pytest.raises((ValueError, TypeError)):
        Catalog(data)


@pytest.mark.parametrize("variant", range(20))
def test_no_route_is_longer_than_the_limit(variant):
    # A node can get a longer but cheaper path from another node in the same hop. Before the fix, it
    # was then extended within that hop, giving four-step routes. The names change the set order, so
    # the old code fails on some variants and the fix has to hold on all of them.
    n = [f"d{variant}{c}" for c in "abcdef"]
    cat = Catalog({
        "formats": [{"id": fid, "family": "document"} for fid in n],
        "groups": [
            {"engine": "e1", "reads": [n[0]], "writes": [n[1]], "grade": "exact"},
            {"engine": "e2", "reads": [n[0]], "writes": [n[2]], "grade": "extract"},
            {"engine": "e3", "reads": [n[1]], "writes": [n[2]], "grade": "exact"},
            {"engine": "e4", "reads": [n[2]], "writes": [n[3]], "grade": "exact"},
            {"engine": "e5", "reads": [n[3]], "writes": [n[4]], "grade": "exact"},
            {"engine": "e6", "reads": [n[4]], "writes": [n[5]], "grade": "exact"},
        ],
    })
    assert all(len(route["steps"]) <= MAX_HOPS for route in cat.routes_from(n[0]))


def test_the_same_route_wins_in_every_process():
    # Ties between routes of equal cost used to follow a set's order, which changes with each process,
    # so the worker could run a different route from the one the page showed, or the tests had tried.
    import json
    import subprocess
    import sys
    from pathlib import Path

    script = (
        "import json, sys; sys.path.insert(0, sys.argv[1]); from tests.unit.test_router import CAT, FAMILIES;"
        "print(json.dumps({s: sorted((r['to'], [st['to'] for st in r['steps']]) for r in CAT.routes_from(s)) for s in FAMILIES}))"
    )
    root = str(Path(__file__).resolve().parents[2])
    outputs = {
        subprocess.run([sys.executable, "-c", script, root], capture_output=True, text=True, check=True,
                       env={"PYTHONHASHSEED": str(seed), "PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/bin:/bin"}).stdout
        for seed in (1, 2, 3, 4, 5, 6)
    }
    assert len(outputs) == 1 and json.loads(outputs.pop())
