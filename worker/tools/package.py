"""Wrap one file, unchanged, in an archive or compressed file. Runs inside the sandbox."""

import bz2
import gzip
import lzma
import os
import shutil
import subprocess
import sys
from pathlib import Path


def main():
    src, output, dst_fmt, src_ext, tmp = sys.argv[1:6]
    if dst_fmt in ("gz", "bz2", "xz"):
        opener = {"gz": gzip.open, "bz2": bz2.open, "xz": lzma.open}[dst_fmt]
        with open(src, "rb") as fh, opener(output, "wb") as out:
            shutil.copyfileobj(fh, out, 1024 * 1024)
        return
    if dst_fmt == "zst":
        # Through stdin, because zstd refuses to read a symlink, which is how every tool gets its input.
        with open(src, "rb") as fh:
            subprocess.run(["zstd", "-q", "-T0", "-o", output], check=True, stdin=fh)
        return
    # Inside the archive it gets a neutral name. The worker is never told what it was called.
    stage = Path(tmp) / f"package-{os.getpid()}"
    stage.mkdir(mode=0o700)
    name = f"file.{src_ext}"
    os.symlink(os.path.realpath(src), stage / name)
    try:
        # -L stores what the link points to, not the link. -a picks the format from the extension.
        subprocess.run(["bsdtar", "-a", "-L", "-cf", output, "-C", str(stage), name], check=True, stdin=subprocess.DEVNULL)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


main()
