"""Office files, documents, ebooks, PDF and OCR."""

from __future__ import annotations

import subprocess

from worker.engines.base import SYS_PY, TOOLS, Engine, Simple, group

WRITER_IN = ["doc", "dot", "docx", "docm", "dotx", "odt", "ott", "fodt", "rtf", "wpd", "wps", "lwp", "abw", "hwp", "pages", "sxw", "txt", "html", "mw", "cwk", "wri"]
CALC_IN = ["xls", "xlsx", "xlsm", "xlsb", "xltx", "ods", "fods", "csv", "tsv", "dif", "slk", "dbf", "wk1", "numbers", "sxc", "qpw"]
IMPRESS_IN = ["ppt", "pptx", "pptm", "ppsx", "potx", "odp", "fodp", "key", "sxi"]
DRAW_IN = ["vsd", "vsdx", "pub", "cdr", "cmx", "wmf", "emf", "odg", "fodg", "cgm", "sxd"]

WRITER_OUT = ["pdf", "docx", "doc", "odt", "fodt", "rtf", "txt", "html", "epub"]
CALC_OUT = ["pdf", "xlsx", "xls", "ods", "fods", "csv", "tsv", "html"]
IMPRESS_OUT = ["pdf", "pptx", "ppt", "odp", "fodp"]
DRAW_OUT = ["pdf", "odg", "fodg", "png", "svg"]

# Written into each job's private LibreOffice profile before it starts.
_PROFILE = """<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry" xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop></item>
<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value></prop></item>
<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="BlockUntrustedRefererLinks" oor:op="fuse"><value>true</value></prop></item>
<item oor:path="/org.openoffice.Office.Calc/Content/Update"><prop oor:name="Link" oor:op="fuse"><value>2</value></prop></item>
<item oor:path="/org.openoffice.Office.Writer/Content/Update"><prop oor:name="Link" oor:op="fuse"><value>2</value></prop></item>
<item oor:path="/org.openoffice.Office.Common/Misc"><prop oor:name="UseOpenCL" oor:op="fuse"><value>false</value></prop></item>
<item oor:path="/org.openoffice.Office.Common/Misc"><prop oor:name="FirstRun" oor:op="fuse"><value>false</value></prop></item>
<item oor:path="/org.openoffice.Office.Recovery/AutoSave"><prop oor:name="Enabled" oor:op="fuse"><value>false</value></prop></item>
<item oor:path="/org.openoffice.Office.Recovery/RecoveryInfo"><prop oor:name="Enabled" oor:op="fuse"><value>false</value></prop></item>
</oor:items>
"""

_LO_TARGET = {
    "txt": "txt:Text (encoded):UTF8",
    "csv": 'csv:Text - txt - csv (StarCalc):44,34,76',
    "tsv": 'csv:Text - txt - csv (StarCalc):9,34,76',
}


class LibreOffice(Engine):
    name = "LibreOffice"
    binary = "soffice"
    # It insists on a control socket under /tmp, so it gets socket rights there and nothing else.
    extra_rules = {"sock": ["/tmp"]}

    def describe(self, db):
        return [
            group(self.name, db.ids(WRITER_IN), db.ids(WRITER_OUT), "lossy", priority=0.2),
            group(self.name, db.ids(CALC_IN), db.ids(CALC_OUT), "lossy", priority=0.2),
            group(self.name, db.ids(IMPRESS_IN), db.ids(IMPRESS_OUT), "lossy", priority=0.2),
            group(self.name, db.ids(DRAW_IN), db.ids(DRAW_OUT), "lossy", priority=0.2),
            # PDF opened through its Writer import, for editing. The layout rarely survives intact.
            group(self.name, db.ids(["pdf"]), db.ids(["docx", "odt", "rtf"]), "lossy", priority=0.2),
        ]

    def prepare(self, step):
        user = step.work / "lo" / "user"
        user.mkdir(parents=True, mode=0o700)
        (user / "registrymodifications.xcu").write_text(_PROFILE, encoding="utf-8")

    def env(self, step):
        return {"SAL_USE_VCLPLUGIN": "svp"}

    def commands(self, step):
        argv = [
            "soffice", f"-env:UserInstallation=file://{step.work}/lo",
            "--headless", "--nologo", "--nodefault", "--nolockcheck", "--norestore", "--nofirststartwizard",
        ]
        if step.src_fmt == "pdf":
            argv.append("--infilter=writer_pdf_import")
        elif step.src_fmt == "html":
            # Otherwise it opens as a web document, which can't be saved as most office formats.
            argv.append("--infilter=HTML (StarWriter)")
        elif step.src_fmt == "txt":
            argv.append("--infilter=Text (encoded):UTF8")
        argv += ["--convert-to", _LO_TARGET.get(step.dst_fmt, step.out_ext), "--outdir", str(step.out), str(step.src)]
        return [argv]


_PANDOC_READ = {
    "md": "markdown", "rst": "rst", "html": "html", "docx": "docx", "odt": "odt", "epub": "epub", "tex": "latex",
    "org": "org", "txt": "markdown", "csv": "csv", "tsv": "tsv", "wiki": "mediawiki", "textile": "textile",
    "dbk": "docbook", "fb2": "fb2", "ipynb": "ipynb", "rtf": "rtf", "man": "man", "opml": "opml", "muse": "muse",
    "t2t": "t2t", "typ": "typst",
}
_PANDOC_WRITE = {
    "md": "markdown", "rst": "rst", "html": "html", "docx": "docx", "odt": "odt", "epub": "epub3", "tex": "latex",
    "org": "org", "txt": "plain", "wiki": "mediawiki", "textile": "textile", "dbk": "docbook5", "fb2": "fb2",
    "ipynb": "ipynb", "rtf": "rtf", "man": "man", "adoc": "asciidoc", "pptx": "pptx", "opml": "opml", "muse": "muse",
    "typ": "typst", "texi": "texinfo", "icml": "icml",
}


class Pandoc(Engine):
    name = "pandoc"
    binary = "pandoc"

    def describe(self, db):
        readers = set(subprocess.run(["pandoc", "--list-input-formats"], capture_output=True, text=True, timeout=30, check=False).stdout.split())
        writers = set(subprocess.run(["pandoc", "--list-output-formats"], capture_output=True, text=True, timeout=30, check=False).stdout.split())
        self.reads = {db.ensure(k, None, "document"): v for k, v in _PANDOC_READ.items() if v in readers}
        self.writes = {db.ensure(k, None, "document"): v for k, v in _PANDOC_WRITE.items() if v in writers}
        return [group(self.name, sorted(self.reads), sorted(self.writes), "lossy", priority=0.1)]

    def export_data(self):
        return {"reads": self.reads, "writes": self.writes}

    def load_data(self, data):
        self.reads, self.writes = data["reads"], data["writes"]

    def commands(self, step):
        # --sandbox stops a document from pulling in other files through includes and images.
        return [["pandoc", "--sandbox", "--standalone", "-f", self.reads[step.src_fmt], "-t", self.writes[step.dst_fmt],
                 "-o", str(step.output), str(step.src)]]


_CALIBRE_IN = ["azw3", "cbc", "cbr", "cbz", "chm", "djvu", "docx", "epub", "fb2", "html", "htmlz", "lit", "lrf", "mobi", "odt", "pdb", "pdf", "pml", "rb", "rtf", "snb", "tcr", "txt", "txtz"]
_CALIBRE_OUT = ["azw3", "docx", "epub", "fb2", "htmlz", "lit", "lrf", "mobi", "oeb", "pdb", "pdf", "pml", "rb", "rtf", "snb", "tcr", "txt", "txtz"]


def _calibre_env(s):
    return {
        "CALIBRE_CONFIG_DIRECTORY": str(s.home / "calibre"),
        "CALIBRE_TEMP_DIR": str(s.tmp),
        "CALIBRE_CACHE_DIRECTORY": str(s.home / ".cache" / "calibre"),
        "QTWEBENGINE_DISABLE_SANDBOX": "1",
        "QTWEBENGINE_CHROMIUM_FLAGS": "--no-sandbox --disable-gpu --disable-dev-shm-usage --no-zygote",
        "QT_QPA_PLATFORM": "offscreen",
    }


def _ghostscript(s):
    base = ["gs", "-dSAFER", "-dBATCH", "-dNOPAUSE", "-dQUIET"]
    if s.dst_fmt in ("png", "jpg"):
        device = "png16m" if s.dst_fmt == "png" else "jpeg"
        return [*base, f"-sDEVICE={device}", "-r150", f"-sOutputFile={s.out}/page-%04d.{s.out_ext}", "-f", s.src]
    device = {"pdf": "pdfwrite", "ps": "ps2write", "eps": "eps2write"}[s.dst_fmt]
    return [*base, f"-sDEVICE={device}", f"-sOutputFile={s.output}", "-f", s.src]


def _poppler(s):
    if s.dst_fmt == "txt":
        return ["pdftotext", "-layout", "-enc", "UTF-8", s.src, s.output]
    if s.dst_fmt == "html":
        return ["pdftohtml", "-s", "-noframes", "-q", s.src, s.output]
    if s.dst_fmt == "svg":
        return ["pdftocairo", "-svg", s.src, s.output]
    if s.dst_fmt == "ps":
        return ["pdftops", s.src, s.output]
    flag = {"png": "-png", "jpg": "-jpeg", "tiff": "-tiff"}[s.dst_fmt]
    return ["pdftoppm", "-r", "150", flag, s.src, f"{s.out}/page"]


def _tex_prepare(s):
    # openin_any=p refuses absolute paths and "..", so the input has to sit in the current folder.
    (s.work / "input.tex").symlink_to(s.src)


def _tex_env(s):
    # Kpathsea reads these from the environment: no shell commands, and only ordinary file names.
    return {
        "openin_any": "p", "openout_any": "p", "shell_escape": "f",
        "TEXMFVAR": str(s.home / "texmf-var"), "TEXMFCONFIG": str(s.home / "texmf-config"),
    }


def _dvisvgm(s):
    flag = {"pdf": ["--pdf"], "eps": ["--eps"]}.get(s.src_fmt, [])
    return ["dvisvgm", *flag, "--no-fonts", f"--output={s.output}", s.src]


def document_engines():
    return [
        LibreOffice(),
        Pandoc(),
        Simple("Calibre", "ebook-convert", [(_CALIBRE_IN, _CALIBRE_OUT, "lossy", {"priority": 0.3})],
               lambda s: ["ebook-convert", s.src, s.output], env=_calibre_env),
        Simple("XeLaTeX", "xelatex", [(["tex"], ["pdf"], "lossy", {"priority": 0.1})],
               lambda s: ["xelatex", "-no-shell-escape", "-interaction=nonstopmode", "-halt-on-error", "-output-directory=out", "input.tex"],
               env=_tex_env, keep=(".pdf",), prepare=_tex_prepare),
        Simple("dvisvgm", "dvisvgm", [(["dvi", "eps", "pdf"], ["svg"], "lossy", {"priority": 0.4})], _dvisvgm,
               env=lambda s: {"TEXMFVAR": str(s.home / "texmf-var")}),
        Simple("Ghostscript", "gs", [
            (["ps", "eps", "ai"], ["pdf"], "exact"),
            (["pdf", "ps", "eps"], ["ps", "eps"], "exact", {"priority": 0.2}),
            (["ps", "eps"], ["png", "jpg"], "lossy", {"terminal": True, "priority": 0.1}),
        ], _ghostscript),
        Simple("Poppler", "pdftotext", [
            (["pdf"], ["txt"], "lossy"),
            (["pdf"], ["html"], "lossy", {"priority": 0.2}),
            (["pdf"], ["svg"], "lossy", {"priority": 0.2}),
            (["pdf"], ["ps"], "exact"),
            # One image per page, returned as a zip.
            (["pdf"], ["png", "jpg", "tiff"], "lossy", {"terminal": True}),
        ], _poppler),
        Simple("WeasyPrint", None, [(["html"], ["pdf"], "lossy", {"priority": 0.1})],
               lambda s: [SYS_PY, "-I", f"{TOOLS}/html2pdf.py", s.src, s.output]),
        Simple("Tesseract", "tesseract", [(["png", "jpg", "tiff", "bmp", "ppm", "pgm", "pbm", "webp", "gif"], ["txt"], "extract", {"priority": 0.2})],
               lambda s: ["tesseract", s.src, str(s.output)[: -len(s.out_ext) - 1], "-l", "eng+ell"]),
        Simple("OCRmyPDF", "ocrmypdf", [(["pdf"], ["pdfocr"], "exact")],
               lambda s: ["ocrmypdf", "-l", "eng+ell", "--skip-text", "--jobs", str(s.threads), "--quiet", s.src, s.output]),
        Simple("eSpeak NG", "espeak-ng", [(["txt"], ["wav"], "lossy", {"priority": 0.5})],
               lambda s: [SYS_PY, "-I", f"{TOOLS}/tts.py", s.src, s.output]),
    ]
