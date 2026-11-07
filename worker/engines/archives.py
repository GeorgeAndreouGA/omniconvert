"""Archives, disk images, and the fallbacks that work on any file."""

from __future__ import annotations

from worker.engines.base import PY, TOOLS, Simple

ARCHIVE_IN = [
    "zip", "7z", "tar", "tar.gz", "tar.bz2", "tar.xz", "tar.zst", "gz", "bz2", "xz", "zst", "rar", "cab", "cpio", "lzh",
    "arj", "deb", "rpm", "msi", "exe", "jar", "apk", "wim", "dmg", "xar", "iso", "squashfs", "cbz", "cbr",
]
ARCHIVE_OUT = ["zip", "7z", "tar", "tar.gz", "tar.bz2", "tar.xz", "tar.zst", "cpio", "iso", "squashfs"]
PACKAGE_OUT = ["zip", "7z", "tar", "tar.gz", "tar.bz2", "tar.xz", "tar.zst", "gz", "bz2", "xz", "zst"]
DISK = ["raw", "qcow2", "vmdk", "vdi", "vhd", "vhdx", "qed"]


def archive_engines():
    return [
        Simple("Archive tools", "bsdtar", [(ARCHIVE_IN, ARCHIVE_OUT, "exact")],
               lambda s: [PY, "-I", f"{TOOLS}/archive_convert.py", s.src, s.output, s.src_fmt, s.dst_fmt, s.tmp, s.max_bytes]),
        Simple("Package", "bsdtar", [(["*"], PACKAGE_OUT, "package", {"terminal": True, "priority": 0.5})],
               lambda s: [PY, "-I", f"{TOOLS}/package.py", s.src, s.output, s.dst_fmt, s.src_ext, s.tmp]),
        Simple("Encode", None, [(["*"], ["b64", "hex"], "package", {"terminal": True, "priority": 0.8})],
               lambda s: [PY, "-I", f"{TOOLS}/encode.py", s.src, s.output, s.dst_fmt]),
        Simple("Strings", None, [(["*"], ["strings"], "extract", {"terminal": True, "priority": 1.0})],
               lambda s: [PY, "-I", f"{TOOLS}/encode.py", s.src, s.output, "strings"]),
        Simple("ExifTool", "exiftool", [(["*"], ["metadata"], "extract", {"terminal": True})],
               lambda s: [PY, "-I", f"{TOOLS}/details.py", s.src, s.output]),
        Simple("Tika", "java", [(["*"], ["txt"], "extract", {"terminal": True, "priority": 0.5})],
               lambda s: [PY, "-I", f"{TOOLS}/tika_text.py", s.src, s.output, s.tmp]),
        Simple("qemu-img", "qemu-img", [(DISK, DISK, "exact")],
               lambda s: [PY, "-I", f"{TOOLS}/disk_convert.py", s.src, s.output, s.src_fmt, s.dst_fmt, s.max_bytes]),
        Simple("chdman", "chdman", [(["iso"], ["chd"], "exact"), (["chd"], ["iso"], "exact")],
               lambda s: ["chdman", "createdvd" if s.dst_fmt == "chd" else "extractdvd", "-i", s.src, "-o", s.output, "-f"]),
    ]
