"""Makes one small real sample of as many formats as possible, using the worker image's own tools.

    docker run --rm --network none --user "$(id -u):$(id -g)" -e HOME=/tmp --tmpfs /tmp:mode=1777 \\
        -v "$PWD/tests/fixtures:/fx:ro" -v "$OUT:/out" omniconvert-worker:1.2.0 \\
        /opt/py/bin/python /fx/make_fixtures.py /out

A sample that can't be made is reported and skipped, so one missing tool doesn't stop the rest.
"""

import gzip
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

OUT = Path(sys.argv[1])
TMP = Path("/tmp/fx")
TMP.mkdir(exist_ok=True)
ENV = {**os.environ, "HOME": "/tmp", "TMPDIR": "/tmp", "QT_QPA_PLATFORM": "offscreen",
       "PATH": f"/opt/libredwg/bin:{os.environ['PATH']}"}
made, failed = [], []


def run(*argv, **kwargs):
    subprocess.run([str(a) for a in argv], check=True, env=ENV, stdin=subprocess.DEVNULL, capture_output=True, timeout=240, **kwargs)


def sample(name, fn):
    try:
        fn(OUT / f"sample.{name}")
        if (OUT / f"sample.{name}").stat().st_size == 0:
            raise RuntimeError("empty")
        made.append(name)
    except Exception as err:
        failed.append((name, str(err)[:160]))


def text(content):
    return lambda p: p.write_text(content, encoding="utf-8")


def magick_from_png(p):
    run("magick", OUT / "sample.png", p)


def soffice_from(src, target):
    def make(p):
        run("soffice", "-env:UserInstallation=file:///tmp/lo", "--headless", "--nologo", "--norestore",
            "--convert-to", target, "--outdir", TMP, OUT / f"sample.{src}")
        shutil.move(str(TMP / f"sample.{target.split(':')[0]}"), p)
    return make


def pandoc_to(fmt):
    return lambda p: run("pandoc", "--standalone", "-f", "markdown", "-t", fmt, "-o", p, OUT / "sample.md")


def ffmpeg_a(p):
    run("ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", p)


def ffmpeg_v(p):
    extra = {".3gp": ["-c:v", "libx264", "-c:a", "aac"], ".mxf": ["-ar", "48000", "-ac", "2"], ".vob": ["-ar", "48000"]}.get(p.suffix, [])
    run("ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=duration=2:size=160x120:rate=25",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-shortest", "-pix_fmt", "yuv420p", *extra, p)


def bsdtar(p):
    run("bsdtar", "-a", "-cf", p, "-C", TMP / "tree", ".")


MD = "# Sample document\n\nΚαλημέρα and hello. A paragraph with *emphasis*.\n\n- one\n- two\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"
TABLE = [{"name": "alpha", "value": 1, "price": 1.5}, {"name": "beta", "value": 2, "price": 2.5}]

# ---- documents ----
sample("md", text(MD))
sample("txt", text("Sample text.\nΚαλημέρα.\n"))
sample("html", text("<!doctype html><html><head><meta charset='utf-8'><title>Sample</title></head><body><h1>Sample</h1><p>Hello.</p></body></html>"))
for name, fmt in [("docx", "docx"), ("odt", "odt"), ("rtf", "rtf"), ("rst", "rst"), ("tex", "latex"), ("org", "org"), ("epub", "epub"),
                  ("adoc", "asciidoc"), ("dbk", "docbook5"), ("wiki", "mediawiki"), ("textile", "textile"), ("fb2", "fb2"), ("ipynb", "ipynb"), ("pptx", "pptx")]:
    sample(name, pandoc_to(fmt))
sample("man", text(".TH SAMPLE 1\n.SH NAME\nsample \\- a sample\n"))
sample("doc", soffice_from("docx", "doc"))
sample("pdf", soffice_from("docx", "pdf"))
sample("ps", lambda p: run("pdftops", OUT / "sample.pdf", p))
sample("mobi", lambda p: run("ebook-convert", OUT / "sample.epub", p, env=None) if False else subprocess.run(["ebook-convert", str(OUT / "sample.epub"), str(p)], check=True, capture_output=True, timeout=240, env={**ENV, "QTWEBENGINE_DISABLE_SANDBOX": "1"}))
sample("azw3", lambda p: subprocess.run(["ebook-convert", str(OUT / "sample.epub"), str(p)], check=True, capture_output=True, timeout=240, env=ENV))
sample("djvu", lambda p: run("pdf2djvu", "-q", "-o", p, OUT / "sample.pdf"))

# ---- spreadsheets and slides ----
sample("csv", text("name,value,price\nalpha,1,1.5\nbeta,2,2.5\n"))
sample("tsv", text("name\tvalue\tprice\nalpha\t1\t1.5\nbeta\t2\t2.5\n"))
sample("xlsx", soffice_from("csv", "xlsx"))
sample("xls", soffice_from("csv", "xls"))
sample("ods", soffice_from("csv", "ods"))
sample("ppt", soffice_from("pptx", "ppt"))
sample("odp", soffice_from("pptx", "odp"))

# ---- images ----
sample("png", lambda p: run("magick", "-size", "96x64", "xc:#3366cc", "-fill", "white", "-draw", "rectangle 10,10 50,40", p))
for name in ["jpg", "webp", "gif", "tiff", "bmp", "ico", "tga", "ppm", "pgm", "pbm", "xpm", "pcx", "jp2", "hdr", "psd", "sgi", "dds", "fits"]:
    sample(name, magick_from_png)
sample("heic", lambda p: run("heif-enc", "-o", p, OUT / "sample.png"))
sample("avif", lambda p: run("heif-enc", "-A", "-o", p, OUT / "sample.png"))
sample("jxl", lambda p: run("cjxl", OUT / "sample.png", p))
sample("svg", text('<svg xmlns="http://www.w3.org/2000/svg" width="96" height="64"><rect width="96" height="64" fill="#3366cc"/><circle cx="48" cy="32" r="20" fill="#fff"/></svg>'))
sample("svgz", lambda p: p.write_bytes(gzip.compress((OUT / "sample.svg").read_bytes())))
sample("eps", lambda p: run("gs", "-dSAFER", "-dBATCH", "-dNOPAUSE", "-dQUIET", "-sDEVICE=eps2write", f"-sOutputFile={p}", "-f", OUT / "sample.pdf"))
sample("emf", lambda p: run("inkscape", f"--export-filename={p}", OUT / "sample.svg"))
sample("wmf", lambda p: run("inkscape", f"--export-filename={p}", OUT / "sample.svg"))
sample("odg", soffice_from("svg", "odg") if False else (lambda p: run("soffice", "-env:UserInstallation=file:///tmp/lo", "--headless", "--nologo", "--norestore", "--infilter=draw_svg_import", "--convert-to", "odg", "--outdir", TMP, OUT / "sample.svg") or shutil.move(str(TMP / "sample.odg"), p)))
sample("dng", lambda p: (_ for _ in ()).throw(RuntimeError("no free RAW sample can be generated")))

# ---- audio, video, subtitles ----
for name in ["mp3", "wav", "flac", "ogg", "opus", "aac", "m4a", "aiff", "wma", "ac3", "wv", "mka", "au", "caf"]:
    sample(name, ffmpeg_a)
for name in ["mp4", "mkv", "webm", "mov", "avi", "wmv", "flv", "mpg", "ts", "ogv", "3gp", "vob", "mxf", "swf"]:
    sample(name, ffmpeg_v)
sample("srt", text("1\n00:00:00,000 --> 00:00:01,000\nHello\n\n2\n00:00:01,000 --> 00:00:02,000\nWorld\n"))
sample("vtt", text("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nHello\n"))
sample("ass", lambda p: run("ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-i", OUT / "sample.srt", p))
sample("mid", lambda p: p.write_bytes(bytes.fromhex("4d546864000000060000000100604d54726b0000001600ff510307a12000c0000090403c6080403c00ff2f00")))

# ---- archives and disk images ----
(TMP / "tree" / "sub").mkdir(parents=True, exist_ok=True)
(TMP / "tree" / "hello.txt").write_text("Hello from an archive.\n")
(TMP / "tree" / "sub" / "more.txt").write_text("More.\n")
for name in ["zip", "7z", "tar", "tar.gz", "tar.bz2", "tar.xz", "tar.zst"]:
    sample(name, bsdtar)
sample("cpio", lambda p: run("bsdtar", "--format=cpio", "-cf", p, "-C", TMP / "tree", "."))
sample("iso", lambda p: run("genisoimage", "-quiet", "-R", "-J", "-o", p, TMP / "tree"))
sample("squashfs", lambda p: run("mksquashfs", TMP / "tree", p, "-noappend", "-quiet", "-no-progress", "-all-root"))
sample("gz", lambda p: p.write_bytes(gzip.compress(b"Hello.\n")))
sample("bz2", lambda p: __import__("bz2").open(p, "wb").write(b"Hello.\n"))
sample("xz", lambda p: __import__("lzma").open(p, "wb").write(b"Hello.\n"))
sample("zst", lambda p: run("zstd", "-q", "-f", "-o", p, TMP / "tree" / "hello.txt"))
sample("raw", lambda p: run("qemu-img", "create", "-q", "-f", "raw", p, "1M"))
for name in ["qcow2", "vmdk", "vdi", "vhd", "vhdx", "qed"]:
    fmt = {"vhd": "vpc"}.get(name, name)
    sample(name, lambda p, fmt=fmt: run("qemu-img", "convert", "-O", fmt, OUT / "sample.raw", p))
sample("chd", lambda p: run("chdman", "createdvd", "-i", OUT / "sample.iso", "-o", p, "-f"))

# ---- data ----
def pandas_sample(writer):
    def make(p):
        import pandas as pd
        writer(pd.DataFrame(TABLE), p)
    return make


sample("json", text(json.dumps({"name": "sample", "items": [1, 2, 3], "nested": {"ok": True}}, indent=2)))
sample("jsonl", text("\n".join(json.dumps(row) for row in TABLE) + "\n"))
sample("yaml", text("name: sample\nitems: [1, 2, 3]\nnested:\n  ok: true\n"))
sample("toml", text('name = "sample"\nitems = [1, 2, 3]\n\n[nested]\nok = true\n'))
sample("xml", text('<?xml version="1.0"?><root><name>sample</name><item>1</item><item>2</item></root>'))
sample("ini", text("[main]\nname = sample\nok = true\n"))
sample("plist", lambda p: __import__("plistlib").dump({"name": "sample", "n": 1}, p.open("wb")))
sample("msgpack", lambda p: p.write_bytes(__import__("msgpack").packb({"name": "sample", "n": 1})))
sample("cbor", lambda p: p.write_bytes(__import__("cbor2").dumps({"name": "sample", "n": 1})))
sample("parquet", pandas_sample(lambda d, p: d.to_parquet(p)))
sample("feather", pandas_sample(lambda d, p: d.to_feather(p)))
sample("orc", pandas_sample(lambda d, p: d.to_orc(p)))
sample("dta", pandas_sample(lambda d, p: d.to_stata(p, write_index=False)))


def make_sqlite(p):
    conn = sqlite3.connect(p)
    conn.execute("CREATE TABLE items (name TEXT, value INT)")
    conn.executemany("INSERT INTO items VALUES (?, ?)", [("alpha", 1), ("beta", 2)])
    conn.commit()
    conn.close()


sample("sqlite", make_sqlite)
sample("npy", lambda p: __import__("numpy").save(p, __import__("numpy").arange(6).reshape(2, 3)))
sample("mat", lambda p: __import__("scipy.io").io.savemat(p, {"a": __import__("numpy").arange(4)}))


def make_h5(p):
    import h5py
    with h5py.File(p, "w") as store:
        store["values"] = [1, 2, 3]


sample("hdf5", make_h5)
sample("cdl", text("netcdf sample {\ndimensions:\n\tx = 2 ;\nvariables:\n\tint v(x) ;\ndata:\n v = 1, 2 ;\n}\n"))
sample("nc", lambda p: run("ncgen", "-o", p, OUT / "sample.cdl"))

# ---- 3D, CAD, maps ----
sample("obj", text("v 0 0 0\nv 1 0 0\nv 0 1 0\nv 0 0 1\nf 1 2 3\nf 1 2 4\nf 1 3 4\nf 2 3 4\n"))
sample("stl", lambda p: run("assimp", "export", OUT / "sample.obj", p))
sample("ply", lambda p: run("assimp", "export", OUT / "sample.obj", p))
sample("glb", lambda p: run("assimp", "export", OUT / "sample.obj", p))
sample("dae", lambda p: run("assimp", "export", OUT / "sample.obj", p))
sample("scad", text("cube([1,2,3]);\n"))
sample("off", lambda p: run("openscad", "-o", p, OUT / "sample.scad"))
sample("dxf", lambda p: run("openscad", "-o", p, TMP / "square.scad") if (TMP / "square.scad").write_text("square([2,3]);\n") else None)
sample("dwg", lambda p: run("dxf2dwg", "-y", "-o", p, OUT / "sample.dxf"))
GEOJSON = '{"type":"FeatureCollection","features":[{"type":"Feature","properties":{"name":"A"},"geometry":{"type":"Point","coordinates":[23.72,37.98]}}]}'
sample("geojson", text(GEOJSON))
for name, driver in [("kml", "KML"), ("gpkg", "GPKG"), ("gml", "GML"), ("gpx", "GPX"), ("shp", "ESRI Shapefile")]:
    extra = ["-nln", "waypoints"] if name == "gpx" else []
    sample(name, lambda p, driver=driver, extra=extra: run("ogr2ogr", "-f", driver, *extra, p, OUT / "sample.geojson"))

sample("asc", text("ncols 4\nnrows 4\nxllcorner 0\nyllcorner 0\ncellsize 1\nNODATA_value -9999\n1 2 3 4\n5 6 7 8\n1 2 3 4\n5 6 7 8\n"))
# A shapefile comes with helper files. On their own they aren't samples of anything.
for leftover in ("shx", "prj", "cpg", "dbf"):
    (OUT / f"sample.{leftover}").unlink(missing_ok=True)

# ---- fonts, mail, science, diagrams ----
sample("ttf", lambda p: shutil.copy("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", p))


def font_flavor(flavor):
    def make(p):
        from fontTools.ttLib import TTFont
        font = TTFont(OUT / "sample.ttf")
        font.flavor = flavor
        font.save(p)
    return make


sample("woff", font_flavor("woff"))
sample("woff2", font_flavor("woff2"))
sample("otf", lambda p: run("fontforge", "-quiet", "-lang=ff", "-c", "Open($1); Generate($2);", OUT / "sample.ttf", p))
sample("sfd", lambda p: run("fontforge", "-quiet", "-lang=ff", "-c", "Open($1); Generate($2);", OUT / "sample.ttf", p))
sample("eml", text("From: a@example.com\r\nTo: b@example.com\r\nSubject: Sample\r\nDate: Mon, 2 Nov 2026 10:00:00 +0000\r\nContent-Type: text/plain\r\n\r\nHello.\r\n"))
sample("gv", text("digraph G { a -> b; b -> c; }\n"))
sample("dcm", lambda p: run("img2dcm", OUT / "sample.jpg", p))
sample("sam", text("@HD\tVN:1.6\tSO:unsorted\n@SQ\tSN:chr1\tLN:100\nr1\t0\tchr1\t1\t60\t4M\t*\t0\t0\tACGT\tIIII\n"))
sample("bam", lambda p: run("samtools", "view", "-b", "-o", p, OUT / "sample.sam"))
sample("xyz", text("3\nwater\nO 0.000 0.000 0.000\nH 0.757 0.586 0.000\nH -0.757 0.586 0.000\n"))
sample("mol2", lambda p: run("obabel", "-ixyz", OUT / "sample.xyz", "-omol2", "-O", p))
sample("sdf", lambda p: run("obabel", "-ixyz", OUT / "sample.xyz", "-osdf", "-O", p))
sample("smi", text("CCO ethanol\n"))
# A file in no known format at all, for the fallbacks.
sample("sldprt", lambda p: p.write_bytes(bytes.fromhex("d0cf11e0a1b11ae1") + os.urandom(2048)))

print(json.dumps({"made": sorted(made), "failed": failed}, ensure_ascii=False, indent=1))
