"""Bitmap and vector images."""

from __future__ import annotations

import subprocess

from worker.engines.base import PY, TOOLS, Engine, Simple, group

# ImageMagick "formats" that aren't files: generators, screen grabs, and script languages (MVG and
# MSL can read and write files). Also raw pixel dumps, which need a size to mean anything.
_IM_DENY = {
    "CANVAS", "CAPTION", "CLIP", "CLIPBOARD", "FILE", "FRACTAL", "FTP", "GRADIENT", "GRANITE", "GROUP4", "HALD",
    "HISTOGRAM", "HTTP", "HTTPS", "INFO", "INLINE", "JSON", "LABEL", "LOGO", "MAGICK", "MAP", "MASK", "MATTE", "MPC",
    "MPR", "MPRI", "MSL", "MVG", "NETSCAPE", "NULL", "PANGO", "PATTERN", "PLASMA", "PREVIEW", "PRINT", "RADIAL-GRADIENT",
    "ROSE", "SCAN", "SCANX", "SCREENSHOT", "SHOW", "SPARSE-COLOR", "STEGANO", "STRIMG", "TEXT", "THUMBNAIL", "TILE",
    "TXT", "UIL", "UNIQUE", "URL", "VID", "VIDEO", "WIN", "X", "XC", "XPS", "YAML", "HTML", "HTM", "SHTML", "EPHEMERAL",
    "A", "B", "C", "G", "K", "M", "O", "R", "Y", "CMYK", "CMYKA", "GRAY", "GRAYA", "RGB", "RGBA", "RGBO", "BGR", "BGRA",
    "BGRO", "YCBCR", "YCBCRA", "YCbCr", "YCbCrA", "UYVY", "YUV", "IPL", "PAL", "PALM", "SVG", "MSVG", "RSVG", "EPT", "EPT2",
    "EPT3", "EPI", "EPSF", "EPSI", "EPS2", "EPS3", "PDFA", "PS2", "PS3", "PTIF", "TIFF64", "PNG00", "PNG8", "PNG24", "PNG32",
    "PNG48", "PNG64", "BMP2", "BMP3", "DCX", "FAX", "G3", "G4", "CUBE", "DOT", "GV", "ICON", "AI", "HEIF",
}
_IM_VIDEO = {"3G2", "3GP", "AVI", "FLV", "M2V", "M4V", "MKV", "MOV", "MP4", "MPEG", "MPG", "WEBM", "WMV", "APNG"}
_IM_OK_FAMILIES = {"image", "raw", "other"}
_DOC_LIKE = {"pdf", "ps", "eps"}


class ImageMagick(Engine):
    name = "ImageMagick"
    binary = "magick"

    def __init__(self):
        self.tokens: dict[str, str] = {}

    def describe(self, db):
        out = subprocess.run(["magick", "-list", "format"], capture_output=True, text=True, timeout=60, check=False, errors="replace").stdout
        reads, writes, doc_reads = set(), set(), set()
        doc_writes = set()
        for line in out.splitlines():
            parts = line.split(None, 3)
            if len(parts) < 3:
                continue
            token = parts[0].rstrip("*")
            mode = parts[2]
            if len(mode) != 3 or mode[0] not in "r-" or mode[1] not in "w-":
                continue
            if token in _IM_DENY or token in _IM_VIDEO or not token.replace("-", "").isalnum():
                continue
            description = parts[3].strip() if len(parts) > 3 else None
            ext = token.lower()
            fid = db.resolve(ext)
            if fid is None:
                fid = db.ensure(ext, description, "image")
            if not fid:
                continue
            if fid in _DOC_LIKE:
                self.tokens.setdefault(fid, token)
                if mode[0] == "r":
                    doc_reads.add(fid)
                if mode[1] == "w":
                    doc_writes.add(fid)
                continue
            if db.family(fid) not in _IM_OK_FAMILIES:
                continue
            # The plain name beats aliases, so png uses PNG and not PNG8.
            if fid.upper() == token or fid not in self.tokens:
                self.tokens[fid] = token
            if mode[0] == "r":
                reads.add(fid)
            if mode[1] == "w":
                writes.add(fid)
        return [
            group(self.name, sorted(reads), sorted(writes), "auto", priority=0.2),
            # Rendering pages of a PDF or PostScript file goes through Ghostscript with -dSAFER.
            group(self.name, sorted(doc_reads), sorted(writes), "lossy", priority=0.2),
            # White background: PDF has no good answer for transparent PNGs, and img2pdf refuses them.
            group(self.name, sorted(reads), sorted(doc_writes), "auto", priority=0.3),
        ]

    def export_data(self):
        return {"tokens": self.tokens}

    def load_data(self, data):
        self.tokens = data["tokens"]

    def env(self, step):
        return {"MAGICK_TEMPORARY_PATH": str(step.tmp)}

    def commands(self, step):
        src_token = self.tokens.get(step.src_fmt)
        dst_token = self.tokens[step.dst_fmt]
        source = f"{src_token}:{step.src}" if src_token else str(step.src)
        argv = ["magick", "-limit", "thread", str(step.threads), source]
        if step.dst_fmt in _DOC_LIKE or step.dst_fmt == "jpg":
            argv += ["-background", "white", "-alpha", "remove", "-alpha", "off"]
        argv += ["-auto-orient", f"{dst_token}:{step.output}"]
        return [argv]


def _inkscape(s):
    return ["inkscape", f"--export-filename={s.output}", s.src]


def _djvu(s):
    if s.dst_fmt == "txt":
        return ["djvutxt", s.src, s.output]
    if s.src_fmt == "djvu":
        return ["ddjvu", f"-format={s.out_ext}", s.src, s.output]
    if s.src_fmt == "pdf":
        return ["pdf2djvu", "-q", "-o", s.output, s.src]
    return ["c44", s.src, s.output]


def _heif(s):
    if s.src_fmt in ("heic", "avif"):
        return ["heif-dec", "-o", s.output, s.src]
    extra = ["-A"] if s.dst_fmt == "avif" else []
    return ["heif-enc", *extra, "-o", s.output, s.src]


def _jxl(s):
    return ["djxl", s.src, s.output] if s.src_fmt == "jxl" else ["cjxl", s.src, s.output]


_VIPS_READ = ["png", "jpg", "webp", "tiff", "gif", "heic", "avif", "jxl", "jp2", "fits", "exr", "hdr", "ppm", "pgm", "pbm", "svg", "pdf"]
_VIPS_WRITE = ["png", "jpg", "webp", "tiff", "avif", "jxl", "jp2", "fits", "hdr", "ppm", "pgm", "pbm", "gif"]
_RAW = ["cr2", "cr3", "crw", "nef", "arw", "dng", "orf", "raf", "rw2", "pef", "srw", "3fr", "erf", "mrw", "kdc", "x3f", "iiq"]


def image_engines():
    return [
        ImageMagick(),
        # libvips streams large images instead of loading them whole, which suits a Raspberry Pi.
        Simple("libvips", "vips", [
            ([e for e in _VIPS_READ if e not in ("svg", "pdf")], _VIPS_WRITE, "auto", {"priority": 0.1}),
            (["svg", "pdf"], [e for e in _VIPS_WRITE], "lossy", {"priority": 0.3}),
        ], lambda s: ["vips", "copy", s.src, s.output], env=lambda s: {"VIPS_CONCURRENCY": str(s.threads), "TMPDIR": str(s.tmp)}),
        Simple("libheif", "heif-dec", [
            (["heic", "avif"], ["png", "jpg"], "auto"),
            (["png", "jpg"], ["heic", "avif"], "auto"),
        ], _heif),
        Simple("libjxl", "djxl", [
            (["jxl"], ["png", "jpg", "ppm", "pgm", "exr"], "auto"),
            (["png", "jpg", "gif", "ppm", "pgm", "exr"], ["jxl"], "exact"),
        ], _jxl),
        Simple("resvg", "resvg", [(["svg"], ["png"], "lossy")], lambda s: ["resvg", s.src, s.output]),
        Simple("librsvg", "rsvg-convert", [
            (["svg", "svgz"], ["png", "pdf", "eps", "ps"], "lossy", {"priority": 0.1}),
        ], lambda s: ["rsvg-convert", "--unlimited" if False else "--format", {"png": "png", "pdf": "pdf1.7", "eps": "eps", "ps": "ps"}[s.dst_fmt], "--output", s.output, s.src]),
        Simple("Inkscape", "inkscape", [
            (["svg", "svgz", "pdf", "ai", "eps", "wmf", "emf", "cdr", "vsd", "dxf"], ["svg", "png", "pdf", "eps", "ps", "emf", "wmf"], "lossy", {"priority": 0.5}),
        ], _inkscape, env=lambda s: {"INKSCAPE_PROFILE_DIR": str(s.home / "inkscape"), "QT_QPA_PLATFORM": "offscreen", "GTK_USE_PORTAL": "0"}),
        Simple("Potrace", "potrace", [(["bmp", "pbm", "pgm", "ppm"], ["svg", "eps", "pdf", "dxf"], "lossy", {"priority": 0.2})],
               lambda s: ["potrace", "-b", {"svg": "svg", "eps": "eps", "pdf": "pdf", "dxf": "dxf"}[s.dst_fmt], "-o", s.output, s.src]),
        Simple("VTracer", None, [(["png", "jpg", "bmp", "gif", "tiff", "webp"], ["svg"], "lossy")],
               lambda s: [PY, "-I", f"{TOOLS}/vectorize.py", s.src, s.output]),
        Simple("LibRaw", "dcraw_emu", [(_RAW, ["tiff"], "lossy")],
               lambda s: ["dcraw_emu", "-T", "-w", "-Z", s.output, s.src]),
        Simple("DjVuLibre", "ddjvu", [
            (["djvu"], ["pdf", "tiff", "ppm", "pgm", "pbm"], "lossy"),
            (["djvu"], ["txt"], "extract"),
            (["pdf"], ["djvu"], "lossy", {"priority": 0.5}),
            (["jpg", "ppm", "pgm"], ["djvu"], "lossy", {"priority": 0.5}),
        ], _djvu),
        # JPEG and JPEG 2000 go into the PDF without being re-encoded. PNG goes through ImageMagick
        # instead, because img2pdf refuses transparency.
        Simple("img2pdf", "img2pdf", [(["jpg", "jp2", "tiff"], ["pdf"], "exact")],
               lambda s: ["img2pdf", "--rotation=ifvalid", "-o", s.output, s.src]),
    ]
