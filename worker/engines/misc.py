"""Fonts, mail, data files and diagrams."""

from __future__ import annotations

from worker.engines.base import PY, TOOLS, Simple

TABLE_IN = ["csv", "tsv", "xlsx", "xls", "xlsb", "ods", "json", "jsonl", "parquet", "feather", "orc", "dta", "sas7bdat", "sav", "sqlite", "mat", "npy", "hdf5"]
TABLE_OUT = ["csv", "tsv", "xlsx", "json", "jsonl", "parquet", "feather", "orc", "dta", "sqlite"]
CONFIG = ["json", "yaml", "toml", "xml", "ini", "plist", "msgpack", "cbor"]
GRAPHVIZ_OUT = {"svg": "svg", "png": "png", "pdf": "pdf", "ps": "ps", "jpg": "jpg", "gif": "gif", "json": "json", "txt": "plain"}


def misc_engines():
    return [
        Simple("FontForge", "fontforge", [(["ttf", "otf", "pfb", "sfd", "bdf", "pcf"], ["ttf", "otf", "pfb", "sfd"], "auto", {"priority": 0.4})],
               lambda s: ["fontforge", "-quiet", "-lang=ff", "-c", "Open($1); Generate($2);", s.src, s.output]),
        # Rewrapping only. Going between ttf and otf redraws the outlines, which is FontForge's job.
        Simple("fontTools", None, [
            (["ttf", "otf", "woff", "woff2", "ttx"], ["woff", "woff2", "ttx"], "exact"),
            (["woff", "woff2", "ttx"], ["ttf", "otf"], "exact", {"priority": 0.1}),
        ], lambda s: [PY, "-I", f"{TOOLS}/font_convert.py", s.src, s.output, s.src_fmt, s.dst_fmt]),
        Simple("msgconvert", "msgconvert", [(["msg"], ["eml"], "lossy")], lambda s: ["msgconvert", "--outfile", s.output, s.src]),
        # One mailbox file per Outlook folder, returned as a zip.
        Simple("readpst", "readpst", [(["pst"], ["mbox"], "lossy", {"terminal": True})],
               lambda s: ["readpst", "-q", "-o", s.out, s.src]),
        Simple("tnef", "tnef", [(["tnef"], ["zip"], "extract", {"terminal": True})],
               lambda s: ["tnef", "--overwrite", "-C", s.out, "-f", s.src]),
        Simple("mdbtools", "mdb-export", [(["mdb"], ["csv"], "lossy", {"terminal": True})],
               lambda s: [PY, "-I", f"{TOOLS}/mdb_export.py", s.src, s.out]),
        Simple("Tables", None, [(TABLE_IN, TABLE_OUT, "auto", {"priority": 0.1})],
               lambda s: [PY, "-I", f"{TOOLS}/data_convert.py", s.src, s.output, s.src_fmt, s.dst_fmt, s.out],
               address_space=6 * 1024**3),
        Simple("Settings files", None, [(CONFIG, CONFIG, "lossy")],
               lambda s: [PY, "-I", f"{TOOLS}/config_convert.py", s.src, s.output, s.src_fmt, s.dst_fmt],
               address_space=2 * 1024**3),
        # With SERVER_NAME set, Graphviz refuses image files outside GV_FILE_PATH.
        Simple("Graphviz", "dot", [(["gv"], list(GRAPHVIZ_OUT), "lossy")],
               lambda s: ["dot", f"-T{GRAPHVIZ_OUT[s.dst_fmt]}", "-o", s.output, s.src],
               env=lambda s: {"SERVER_NAME": "omniconvert", "GV_FILE_PATH": "/nonexistent"}),
    ]
