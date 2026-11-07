"""Unpack any archive and repack it as another. Runs inside the sandbox.

The sizes in an archive's headers can lie, so they aren't what's trusted: the tree is measured while
extraction runs, and the extractor is killed the moment it goes past a limit. Exit code 77 tells the
supervisor the archive was refused for its size.
"""

import bz2
import gzip
import lzma
import os
import shutil
import subprocess
import sys
from pathlib import Path

MAX_RATIO = 1000
MAX_FILES = 100000
CHUNK = 1024 * 1024


class TooBig(Exception):
    pass


def measure(root):
    total = count = 0
    for folder, dirs, files in os.walk(root, followlinks=False):
        count += len(files) + len(dirs)
        for name in files:
            try:
                total += os.lstat(os.path.join(folder, name)).st_size
            except OSError:
                pass
    return total, count


def run_watched(argv, root, limit, cwd=None):
    proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        while True:
            try:
                proc.wait(timeout=1)
                break
            except subprocess.TimeoutExpired:
                total, count = measure(root)
                if total > limit or count > MAX_FILES:
                    raise TooBig()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    total, count = measure(root)
    if total > limit or count > MAX_FILES:
        raise TooBig()
    return proc.returncode == 0


def decompress_single(src, root, kind, limit):
    """gz, bz2, xz and zst hold one stream, with no real name to go by."""
    target = root / "file"
    openers = {"gz": gzip.open, "bz2": bz2.open, "xz": lzma.open}
    if kind == "zst":
        with open(target, "wb") as out, open(src, "rb") as compressed:
            # Through stdin, because zstd refuses to read a symlink.
            proc = subprocess.Popen(["zstd", "-dcq", "--no-progress"], stdin=compressed, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            written = 0
            while chunk := proc.stdout.read(CHUNK):
                written += len(chunk)
                if written > limit:
                    proc.kill()
                    raise TooBig()
                out.write(chunk)
            return proc.wait() == 0
    with openers[kind](src, "rb") as stream, open(target, "wb") as out:
        written = 0
        while chunk := stream.read(CHUNK):
            written += len(chunk)
            if written > limit:
                raise TooBig()
            out.write(chunk)
    return True


def extractors(src, root, kind):
    bsdtar = ["bsdtar", "-xf", str(src), "-C", str(root), "--no-same-owner", "--no-same-permissions"]
    seven = ["7z", "x", "-y", "-bd", f"-o{root}", str(src)]
    if kind == "arj":
        return [["arj", "x", "-y", str(src), f"{root}/"], seven]
    if kind == "exe":
        return [["innoextract", "--silent", "--output-dir", str(root), str(src)], ["cabextract", "-q", "-d", str(root), str(src)], seven]
    if kind == "cab":
        return [["cabextract", "-q", "-d", str(root), str(src)], bsdtar, seven]
    if kind == "lzh":
        return [["lha", f"xqfw={root}", str(src)], bsdtar]
    if kind in ("msi", "wim", "dmg", "squashfs"):
        return [seven]
    return [bsdtar, seven]


def scrub(root):
    """Drop links that lead out of the tree and anything that isn't a file or a folder. Make it all
    readable so it can be packed again."""
    resolved_root = os.path.realpath(root)
    for folder, dirs, files in os.walk(root, followlinks=False):
        for name in [*files, *dirs]:
            path = os.path.join(folder, name)
            info = os.lstat(path)
            if os.path.islink(path):
                target = os.path.realpath(path)
                if target != resolved_root and not target.startswith(resolved_root + os.sep):
                    os.unlink(path)
                continue
            if name in files and not (info.st_mode & 0o100000):
                os.unlink(path)
                continue
            os.chmod(path, 0o700 if name in dirs else 0o600)


def repack(root, output, kind):
    if kind == "iso":
        cmd = ["genisoimage", "-quiet", "-R", "-J", "-o", str(output), str(root)]
    elif kind == "squashfs":
        cmd = ["mksquashfs", str(root), str(output), "-noappend", "-quiet", "-no-progress", "-all-root"]
    elif kind == "cpio":
        cmd = ["bsdtar", "--format=cpio", "-cf", str(output), "-C", str(root), "."]
    else:
        # -a chooses the format and compression from the output's extension.
        cmd = ["bsdtar", "-a", "-cf", str(output), "-C", str(root), "."]
    subprocess.run(cmd, check=True, stdin=subprocess.DEVNULL)


def main():
    src, output, src_fmt, dst_fmt, tmp, max_bytes = sys.argv[1:7]
    max_bytes = int(max_bytes)
    root = Path(tmp) / f"extract-{os.getpid()}"
    root.mkdir(mode=0o700)
    input_size = max(os.stat(src).st_size, 1)
    limit = min(max_bytes, input_size * MAX_RATIO)
    try:
        if src_fmt in ("gz", "bz2", "xz", "zst"):
            ok = decompress_single(src, root, src_fmt, limit)
        else:
            ok = False
            for argv in extractors(src, root, src_fmt):
                if shutil.which(argv[0]) is None:
                    continue
                if run_watched(argv, root, limit):
                    ok = True
                    break
                for child in root.iterdir():
                    shutil.rmtree(child, ignore_errors=True) if child.is_dir() else child.unlink(missing_ok=True)
        if not ok or not any(root.iterdir()):
            print("could not unpack this archive", file=sys.stderr)
            sys.exit(1)
        scrub(root)
        repack(root, output, dst_fmt)
    except TooBig:
        print("archive expands past the limits", file=sys.stderr)
        sys.exit(77)
    finally:
        shutil.rmtree(root, ignore_errors=True)


main()
