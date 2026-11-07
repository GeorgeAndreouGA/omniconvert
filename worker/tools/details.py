"""A file's details as JSON, for any file at all. Runs inside the sandbox.

ExifTool stops with an error on types it doesn't know, but the size and the checksum are always true,
so there's always an answer. Fields about this server's copy of the file are left out: its path,
permissions and dates would only describe the job folder, not your file.
"""

import hashlib
import json
import os
import subprocess
import sys

DROP = {"SourceFile", "Directory", "FileName", "FilePermissions", "FileModifyDate", "FileAccessDate", "FileInodeChangeDate",
        "FileSize", "ExifToolVersion", "Error", "Warning"}


def main():
    source, output = sys.argv[1:3]
    digest = hashlib.sha256()
    with open(source, "rb") as fh:
        while chunk := fh.read(1024 * 1024):
            digest.update(chunk)
    details = {"SizeBytes": os.stat(source).st_size, "SHA256": digest.hexdigest()}
    try:
        out = subprocess.run(["exiftool", "-json", "-api", "LargeFileSupport=1", source], capture_output=True,
                             timeout=300, check=False, stdin=subprocess.DEVNULL).stdout
        found = json.loads(out)[0]
        details.update({k: v for k, v in found.items() if k not in DROP})
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        pass
    with open(output, "w", encoding="utf-8") as fh:
        json.dump(details, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


main()
