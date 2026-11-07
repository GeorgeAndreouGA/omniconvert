"""Hostile files, against the real running service. Each must fail or come back harmless.

    PYTHONPATH=. python tests/attacks/run_attacks.py

Every attack here tries to read a local file or run a command through a format's own features. Before
anything runs, a canary job is left on the server holding a random secret, and every output is
checked for it and for /etc/passwd. Outputs are searched inside zip containers too, and PDFs are
converted to text first, because their streams are usually compressed.
"""

from __future__ import annotations

import base64
import bz2
import io
import lzma
import os
import secrets
import struct
import subprocess
import sys
import tarfile
import time
import zipfile
from pathlib import Path

from tests.client import Client

client = Client()
CANARY = f"OMNICANARY-{secrets.token_hex(12)}"
PASSWD = b"root:x:0:0"
# EMF and some Office formats store text as UTF-16.
LEAK_MARKERS = (PASSWD, CANARY.encode(), PASSWD.decode().encode("utf-16-le"), CANARY.encode("utf-16-le"))
results: list[tuple[str, str, str]] = []


def contains_leak(data: bytes | None, depth: int = 0) -> bytes | None:
    if not data:
        return None
    for marker in LEAK_MARKERS:
        if marker in data:
            return marker
    if data[:2] == b"PK" and depth < 2:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                for name in archive.namelist():
                    found = contains_leak(archive.read(name), depth + 1)
                    if found:
                        return found
        except Exception:
            pass
    if data[:4] == b"%PDF" and depth < 2:
        view, text = client.convert(data, "txt", "check.pdf", "pdf", timeout=120)
        if view["state"] == "done":
            return contains_leak(text, depth + 1)
    return None


def record(name: str, ok: bool, detail: str):
    results.append((name, "ok" if ok else "FAIL", detail))
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}: {detail}", flush=True)


def engines_of(view: dict) -> list[str]:
    return [step["engine"] for step in (view.get("route") or {}).get("steps", [])]


def control(name: str, data: bytes, filename: str, to: str, engine: str, hint: str | None = None):
    """A harmless file through the same engine. If this fails, an attack "failing" there proves
    nothing, because the engine would have refused anything."""
    view, output = client.convert(data, to, filename, hint, timeout=180)
    worked = view["state"] == "done" and bool(output) and engine in engines_of(view)
    record(f"control: {name}", worked, f"{view['state']} via {engines_of(view)} {str(view.get('error') or '')[:60]}")


def attack(name: str, data: bytes, filename: str, to: str, hint: str | None = None, must_fail: bool = False, extra_check=None, reaches: str | None = None, not_first: str | None = None):
    view, output = client.convert(data, to, filename, hint, timeout=180)
    state = view["state"]
    if not_first and engines_of(view)[:1] == [not_first]:
        record(name, False, f"{not_first} was the first tool to read it, so its contents could steer it: {engines_of(view)}")
        return
    if reaches and reaches not in engines_of(view):
        record(name, False, f"never reached {reaches}, so this tested nothing: {engines_of(view)} {state} {str(view.get('error'))[:60]}")
        return
    leak = contains_leak(output)
    if leak:
        record(name, False, f"LEAKED {leak!r}")
        return
    if state in ("timeout", "gone"):
        record(name, False, f"state {state}")
        return
    if must_fail and state == "done":
        record(name, False, "should have been refused, but it converted")
        return
    if extra_check and output is not None:
        problem = extra_check(output)
        if problem:
            record(name, False, problem)
            return
    message = view.get("error") or view.get("status") or f"converted to {len(output or b'')} bytes, nothing leaked"
    record(name, True, f"{state}: {str(message)[:90]}")


# ---- ffmpeg: playlists that open other files ----
HLS = b"#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:0\n#EXTINF:1.0,\nfile:///etc/passwd\n#EXT-X-ENDLIST\n"
FFCONCAT = b"ffconcat version 1.0\nfile /etc/passwd\n"
DASH = b'<?xml version="1.0"?><MPD xmlns="urn:mpeg:dash:schema:mpd:2011"><Period><AdaptationSet><Representation><BaseURL>file:///etc/passwd</BaseURL></Representation></AdaptationSet></Period></MPD>'
SDP = b"v=0\no=- 0 0 IN IP4 127.0.0.1\ns=x\nc=IN IP4 127.0.0.1\nt=0 0\nm=video 5004 RTP/AVP 96\n"

# ---- ImageMagick: script formats ----
MVG = b"push graphic-context\nviewbox 0 0 640 480\nimage over 0,0 0,0 'file:///etc/passwd'\npop graphic-context\n"
MSL = b'<?xml version="1.0"?><image><read filename="/etc/passwd"/><write filename="/tmp/pwned.png"/></image>'
IM_AT = b'P3\n1 1\n255\n0 0 0\n'

# ---- SVG: entities and external images ----
SVG_XXE = b'<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY x SYSTEM "file:///etc/passwd">]><svg xmlns="http://www.w3.org/2000/svg" width="900" height="200"><text x="5" y="20">&x;</text></svg>'
SVG_IMG = b'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="200" height="200"><image xlink:href="file:///etc/passwd" width="200" height="200"/></svg>'

# ---- LibreOffice: linked sections and external data ----
FODT = b"""<?xml version="1.0"?><office:document xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" xmlns:xlink="http://www.w3.org/1999/xlink" office:version="1.2" office:mimetype="application/vnd.oasis.opendocument.text"><office:body><office:text><text:section text:name="s"><text:section-source xlink:href="file:///etc/passwd" xlink:type="simple" xlink:show="embed" text:filter-name="Text"/></text:section></office:text></office:body></office:document>"""
CSV_INJECT = b'=WEBSERVICE("http://169.254.169.254/latest/meta-data/"),=COM.MICROSOFT.WEBSERVICE("http://example.com")\n'
HTML_FRAME = b'<html><body><iframe src="file:///etc/passwd"></iframe><img src="file:///etc/passwd"><link rel="attachment" href="file:///etc/passwd"><object data="file:///etc/passwd"></object></body></html>'

# ---- Ghostscript: escaping -dSAFER ----
PS_PIPE = b"%!PS\n(%pipe%id > /tmp/pwned) (w) file closefile\nshowpage\n"
PS_READ = b"%!PS\n/Courier findfont 10 scalefont setfont 50 700 moveto\n/f (/etc/passwd) (r) file def f 200 string readstring pop show\nshowpage\n"

# ---- TeX: shell escape and reading files ----
TEX_SHELL = rb"\documentclass{article}\begin{document}\immediate\write18{id > /tmp/pwned}Hello\end{document}"
TEX_READ = rb"\documentclass{article}\begin{document}\input{/etc/passwd}\end{document}"

# ---- pandoc: includes and images ----
MD_READ = b"![x](/etc/passwd)\n\n<img src='file:///etc/passwd'>\n"
TEX_PANDOC = rb"\include{/etc/passwd}"

# ---- Graphviz ----
DOT_IMG = b'digraph G { a [image="/etc/passwd"]; b [label=<<TABLE><TR><TD><IMG SRC="/etc/passwd"/></TD></TR></TABLE>>]; }\n'

# ---- settings files: entity bombs and object construction ----
XML_XXE = b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]><r>&x;</r>'
XML_BOMB = b'<?xml version="1.0"?><!DOCTYPE b [' + b"".join(b'<!ENTITY a%d "%s">' % (i, b"&a%d;" % (i - 1) * 10 if i else b"x" * 10) for i in range(9)) + b']><b>&a8;</b>'
YAML_OBJECT = b"!!python/object/apply:os.system ['id > /tmp/pwned']\n"
YAML_BOMB = b"a: &a [x,x,x,x,x,x,x,x,x]\n" + b"".join(b"%c: &%c [*%c,*%c,*%c,*%c,*%c,*%c,*%c,*%c,*%c]\n" % ((98 + i,) * 2 + (97 + i,) * 9) for i in range(10))

# ---- pickle: must never be loaded ----
PICKLE = base64.b64decode("gASVLQAAAAAAAACMBXBvc2l4lIwGc3lzdGVtlJOUjBFpZCA+IC90bXAvcHduZWQxMpSFlFKULg==")


def make_zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, body in entries.items():
            archive.writestr(name, body)
    return buf.getvalue()


def tar_with_symlink() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        link = tarfile.TarInfo("leak")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        tar.addfile(link)
        info = tarfile.TarInfo("ok.txt")
        info.size = 2
        tar.addfile(info, io.BytesIO(b"ok"))
    return buf.getvalue()


def tar_with_traversal() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo("../../../../../tmp/pwned.txt")
        info.size = 2
        tar.addfile(info, io.BytesIO(b"no"))
        info = tarfile.TarInfo("ok.txt")
        info.size = 2
        tar.addfile(info, io.BytesIO(b"ok"))
    return buf.getvalue()


def no_escaping_members(output: bytes) -> str | None:
    with tarfile.open(fileobj=io.BytesIO(output)) as tar:
        for member in tar.getmembers():
            if member.issym() and (member.linkname.startswith("/") or ".." in member.linkname):
                return f"kept a link out of the tree: {member.name} -> {member.linkname}"
            if ".." in member.name or member.name.startswith("/"):
                return f"kept an escaping name: {member.name}"
    return None


def qcow2_with_backing(path: bytes = b"/etc/passwd") -> bytes:
    """A minimal QCOW2 version 2 header naming a backing file."""
    header_size = 72
    return (b"QFI\xfb" + struct.pack(">IQIIQIIQQIIQ", 2, header_size, len(path), 16, 1024 * 1024, 0, 1, 196608, 65536, 1, 1, 0)
            + path + b"\0" * (65536 * 4))


def huge_xz() -> bytes:
    return lzma.compress(b"\0" * (64 * 1024 * 1024), preset=9) if False else _stream_compress(lzma.LZMACompressor(preset=1))


def _stream_compress(compressor) -> bytes:
    out = [compressor.compress(b"\0" * (1024 * 1024)) for _ in range(3000)]
    out.append(compressor.flush())
    return b"".join(out)


def guards():
    probe = Path(__file__).with_name("guard_probe.py")
    for name, kind in [
        ("ffmpeg refuses the concat demuxer", "concat"),
        ("ffmpeg refuses the HLS demuxer", "hls"),
        ("ffmpeg refuses a network input", "http"),
        ("ImageMagick refuses MVG", "mvg"),
        ("ImageMagick refuses MSL", "msl"),
        ("ImageMagick refuses a URL", "url"),
        ("ImageMagick refuses @file", "at"),
    ]:
        with open(probe, "rb") as fh:
            done = subprocess.run(["docker", "exec", "-i", "omniconvert-worker", "/usr/bin/python3", "-I", "-", kind],
                                  stdin=fh, capture_output=True, text=True, errors="replace", check=False)
        out = done.stdout.strip()
        said = out.lower()
        refused = bool(out) and not out.startswith("0 ") and any(w in said for w in ("whitelist", "not authorized", "not allowed", "no decode delegate", "unable to open"))
        record(name, refused, (out or done.stderr)[-120:])


def main() -> int:
    print("Leaving a canary job on the server...", flush=True)
    canary_view, _ = client.convert(CANARY.encode() * 4, "zip", "canary.txt", "txt", keep=True)
    if canary_view["state"] != "done":
        print("Could not create the canary job:", canary_view)
        return 2
    print(f"  canary job done: {canary_view['id'][:8]}...\n", flush=True)

    print("Controls: harmless files through the same engines")
    samples = os.environ.get("SAMPLES")
    if samples:
        for source, target, engine in [
            ("mp4", "mkv", "FFmpeg"), ("mp3", "wav", "FFmpeg"), ("png", "pcx", "ImageMagick"), ("svg", "png", "resvg"), ("svg", "eps", "librsvg"),
            ("docx", "pdf", "LibreOffice"), ("csv", "ods", "LibreOffice"), ("html", "pdf", "WeasyPrint"), ("html", "docx", "pandoc"),
            ("ps", "pdf", "Ghostscript"), ("tex", "pdf", "XeLaTeX"), ("md", "docx", "pandoc"), ("md", "epub", "pandoc"), ("gv", "svg", "Graphviz"),
            ("xml", "json", "Settings files"), ("yaml", "json", "Settings files"), ("npy", "csv", "Tables"), ("parquet", "csv", "Tables"),
            ("xz", "zip", "Archive tools"), ("zip", "tar", "Archive tools"), ("qcow2", "raw", "qemu-img"), ("vmdk", "raw", "qemu-img"),
        ]:
            with open(f"{samples}/sample.{source}", "rb") as fh:
                control(f"{source} to {target}", fh.read(), f"sample.{source}", target, engine, hint=source)
    else:
        print("  (set SAMPLES to the fixture folder to run these)")

    print("Detection: the contents decide, not the name")
    if samples:
        def detected(sample: str, filename: str, hint: str | None, to: str = "metadata") -> str | None:
            with open(f"{samples}/sample.{sample}", "rb") as fh:
                res = client.upload(fh.read(), to, filename, hint)
            job = res.json()["id"]
            view = client.wait(job, 120)
            client.http.delete(f"/api/jobs/{job}")
            return view.get("detected")

        for sample, filename, hint, want, why in [
            ("png", "photo.jpg", "jpg", "png", "a PNG named .jpg"),
            ("jpg", "photo.png", "png", "jpg", "a JPEG named .png"),
            ("pdf", "report.docx", "docx", "pdf", "a PDF named .docx"),
            ("png", "noname", None, "png", "a PNG with no extension"),
            ("pdf", "noname", None, "pdf", "a PDF with no extension"),
            ("docx", "noname", None, "docx", "a Word file with no extension, inside a zip"),
            ("docx", "report.docx", "docx", "docx", "a real .docx stays one, though it's a zip"),
            ("xlsx", "sheet.xlsx", "xlsx", "xlsx", "a real .xlsx stays one"),
            ("csv", "data.csv", "csv", "csv", "a .csv stays one, though it's text"),
            ("md", "notes.md", "md", "md", "a .md stays one, though it's text"),
            ("bam", "reads.bam", "bam", "bam", "a .bam stays one, though it's gzip"),
        ]:
            got = detected(sample, filename, hint)
            record(f"Detects {why}", got == want, f"{got}")
    else:
        print("  (set SAMPLES to the fixture folder to run these)")

    print("ffmpeg")
    for name, body, filename, hint in [
        ("HLS playlist as .m3u8", HLS, "a.m3u8", None), ("HLS playlist as .mp4", HLS, "a.mp4", "mp4"),
        ("HLS playlist as .avi", HLS, "a.avi", "avi"), ("HLS playlist as .mkv", HLS, "a.mkv", "mkv"),
        ("ffconcat as .mp4", FFCONCAT, "a.mp4", "mp4"), ("ffconcat as .mov", FFCONCAT, "a.mov", "mov"),
        ("DASH manifest as .mp4", DASH, "a.mp4", "mp4"), ("SDP as .mp4", SDP, "a.mp4", "mp4"),
    ]:
        # These are text whatever they're called, so they never reach FFmpeg. To audio, text is spoken.
        attack(name, body, filename, "wav", hint=hint)
        attack(f"{name}, to video", body, filename, "mkv" if hint != "mkv" else "webm", hint=hint, not_first="FFmpeg")

    print("ImageMagick")
    attack("MVG as .png", MVG, "a.png", "pcx", hint="png", must_fail=True)
    attack("MVG as .tga", MVG, "a.tga", "pcx", hint="tga", must_fail=True)
    attack("MVG with its own name", MVG, "a.mvg", "png", must_fail=True)
    attack("MSL as .png", MSL, "a.png", "pcx", hint="png", must_fail=True)
    attack("MSL with its own name", MSL, "a.msl", "png", must_fail=True)

    print("The guards themselves")
    # Detection now stops all of the above before any parser. A file built to fool detection could
    # still get through, so these feed the same tricks straight to the tools, with their real settings.
    guards()

    print("SVG")
    for target, engine in [("png", "resvg"), ("eps", "librsvg")]:
        attack(f"SVG entity file read, to {target}", SVG_XXE, "a.svg", target, hint="svg", reaches=engine)
        attack(f"SVG external image, to {target}", SVG_IMG, "a.svg", target, hint="svg", reaches=engine)
    attack("SVG entity file read, through Inkscape", SVG_XXE, "a.svg", "emf", hint="svg", reaches="Inkscape")
    attack("SVG external image, through Inkscape", SVG_IMG, "a.svg", "emf", hint="svg", reaches="Inkscape")

    print("LibreOffice")
    attack("Linked section to /etc/passwd, to txt", FODT, "a.fodt", "txt", hint="fodt", reaches="LibreOffice")
    attack("Linked section to /etc/passwd, to pdf", FODT, "a.fodt", "pdf", hint="fodt", reaches="LibreOffice")
    attack("Formula calling a web service", CSV_INJECT, "a.csv", "ods", hint="csv", reaches="LibreOffice")
    attack("HTML pulling in local files, to odt", HTML_FRAME, "a.html", "doc", hint="html", reaches="LibreOffice")

    print("WeasyPrint")
    attack("HTML pulling in local files", HTML_FRAME, "a.html", "pdf", hint="html", reaches="WeasyPrint")

    print("Ghostscript")
    attack("%pipe% command", PS_PIPE, "a.ps", "pdf", hint="ps", reaches="Ghostscript")
    attack("Reading /etc/passwd", PS_READ, "a.ps", "pdf", hint="ps", reaches="Ghostscript")

    print("TeX")
    attack("\\write18 shell escape", TEX_SHELL, "a.tex", "pdf", hint="tex", reaches="XeLaTeX")
    attack("\\input of /etc/passwd", TEX_READ, "a.tex", "pdf", hint="tex", reaches="XeLaTeX")

    print("pandoc")
    attack("Image naming /etc/passwd, to docx", MD_READ, "a.md", "docx", hint="md", reaches="pandoc")
    attack("Image naming /etc/passwd, to epub", MD_READ, "a.md", "epub", hint="md", reaches="pandoc")
    attack("LaTeX include", TEX_PANDOC, "a.tex", "docx", hint="tex", reaches="pandoc")

    print("Graphviz")
    attack("Image attributes naming /etc/passwd", DOT_IMG, "a.gv", "svg", hint="gv", reaches="Graphviz")

    print("Settings and data files")
    attack("XML external entity", XML_XXE, "a.xml", "json", hint="xml", must_fail=True, reaches="Settings files")
    attack("XML entity bomb", XML_BOMB, "a.xml", "json", hint="xml", must_fail=True, reaches="Settings files")
    attack("YAML object construction", YAML_OBJECT, "a.yaml", "json", hint="yaml", must_fail=True, reaches="Settings files")
    attack("YAML alias bomb", YAML_BOMB, "a.yaml", "json", hint="yaml", reaches="Settings files")
    attack("Pickle as .npy", PICKLE, "a.npy", "csv", hint="npy", must_fail=True, reaches="Tables")
    attack("Pickle as .parquet", PICKLE, "a.parquet", "csv", hint="parquet", must_fail=True, reaches="Tables")

    print("Archives")
    bomb = _stream_compress(lzma.LZMACompressor(preset=1))
    attack(f"XZ bomb ({len(bomb) // 1024} KiB expanding to about 3 GiB)", bomb, "a.xz", "zip", hint="xz", must_fail=True, reaches="Archive tools")
    bz_bomb = _stream_compress(bz2.BZ2Compressor(9))
    attack(f"Bzip2 bomb ({len(bz_bomb) // 1024} KiB expanding to about 3 GiB)", bz_bomb, "a.bz2", "zip", hint="bz2", must_fail=True, reaches="Archive tools")
    attack("Zip with ../ names", make_zip({"../../../../../tmp/pwned.txt": b"no", "ok.txt": b"ok"}), "a.zip", "tar", hint="zip", extra_check=no_escaping_members, reaches="Archive tools")
    attack("Tar with ../ names", tar_with_traversal(), "a.tar", "tar.gz", hint="tar", extra_check=no_escaping_members, reaches="Archive tools")
    attack("Tar with a symlink to /etc/passwd", tar_with_symlink(), "a.tar", "tar.gz", hint="tar", extra_check=no_escaping_members, reaches="Archive tools")

    print("Disk images")
    attack("QCOW2 with a backing file", qcow2_with_backing(), "a.qcow2", "raw", hint="qcow2", must_fail=True, reaches="qemu-img")
    attack("VMDK descriptor naming a file", b'# Disk DescriptorFile\nversion=1\nCID=1\nparentCID=ffffffff\ncreateType="monolithicFlat"\nRW 2048 FLAT "/etc/passwd" 0\n', "a.vmdk", "raw", hint="vmdk", must_fail=True, reaches="qemu-img")

    print("The API")
    traversal = [
        ("to with ../", {"to": "../../etc/passwd"}), ("to with a slash", {"to": "a/b"}),
        ("to with shell text", {"to": "pdf;id"}), ("to with an option", {"to": "--help"}),
    ]
    for name, params in traversal:
        res = client.http.put("/api/jobs", params=params, content=b"x")
        record(name, res.status_code == 400, f"HTTP {res.status_code}")
    res = client.upload(b"hello", "zip", "../../../../etc/cron.d/pwned")
    job_id = res.json()["id"]
    view = client.wait(job_id, 60)
    record("filename with ../", view["output"]["name"] in ("pwned.zip",) and "/" not in view["output"]["name"], f"offered as {view['output']['name']!r}")
    client.http.delete(f"/api/jobs/{job_id}")
    # Sent raw as well as encoded: a client needn't use the page.
    raw = client.http.put("/api/jobs", params={"to": "zip"}, content=b"hello", headers={"X-Filename": "%0d%0aSet-Cookie:%20pwned=1%0d%0a.txt"})
    job_id = raw.json()["id"]
    client.wait(job_id, 60)
    res = client.http.get(f"/api/jobs/{job_id}/download")
    record("encoded line breaks in the filename header", "set-cookie" not in res.headers, res.headers["content-disposition"][:80])
    client.http.delete(f"/api/jobs/{job_id}")
    res = client.upload(b"hello", "zip", 'x"\r\nSet-Cookie: pwned=1\r\nX: "y.txt')
    job_id = res.json()["id"]
    client.wait(job_id, 60)
    res = client.http.get(f"/api/jobs/{job_id}/download")
    record("filename with header injection", "set-cookie" not in res.headers and "x" not in {k for k in res.headers if k == "x"}, res.headers["content-disposition"][:80])
    client.http.delete(f"/api/jobs/{job_id}")

    print("\nNothing was left behind by a command that ran")
    for container, python in (("omniconvert-worker", "/opt/py/bin/python"), ("omniconvert-gateway", "python")):
        found = subprocess.run(["docker", "exec", container, python, "-I", "-c",
                                "import glob; print(glob.glob('/tmp/pwned*') + glob.glob('/jobs/pwned*') + glob.glob('/work/**/pwned*', recursive=True))"],
                               capture_output=True, text=True, check=False).stdout.strip()
        record(f"No files dropped in {container}", found == "[]", found)

    print("\nThe canary job is untouched")
    res = client.http.get(f"/api/jobs/{canary_view['id']}/download")
    with zipfile.ZipFile(io.BytesIO(res.content)) as archive:
        inside = archive.read(archive.namelist()[0])
    record("Canary result still intact", CANARY.encode() in inside, f"{len(inside)} bytes")
    client.http.delete(f"/api/jobs/{canary_view['id']}")

    failures = [r for r in results if r[1] != "ok"]
    print(f"\n{len(results) - len(failures)} of {len(results)} attacks handled.")
    for name, _status, detail in failures:
        print(f"  FAILED: {name}: {detail}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
