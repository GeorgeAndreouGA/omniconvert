"""Runs inside the sandbox: reports what a file looks like. The supervisor makes the decision.

Parsing a hostile file happens here, not in the supervisor. Magika is slow to start, so it runs only
when libmagic gives an answer too generic to act on.
"""

import json
import subprocess
import sys

GENERIC = {
    "application/octet-stream", "text/plain", "application/zip", "application/x-ole-storage",
    "application/cdfv2", "inode/x-empty", "application/xml", "text/xml", "application/json",
}


def file_says(flag, path):
    try:
        # -L because a step's input can be a symlink. Without it, those would be "inode/symlink".
        out = subprocess.run(["file", "-L", "--brief", flag, path], capture_output=True, timeout=30, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.decode("ascii", "replace").strip()


def main():
    path = sys.argv[1]
    result = {"mime": file_says("--mime-type", path).lower(), "exts": [], "magika": None}
    exts = file_says("--extension", path)
    if exts and exts != "???":
        result["exts"] = [e.strip().lower() for e in exts.split("/") if e.strip() and e.strip() != "???"]

    if result["mime"] in GENERIC or not result["mime"]:
        try:
            from pathlib import Path

            from magika import Magika

            found = Magika().identify_path(Path(path))
            label = str(found.output.label).lower()
            if label not in ("unknown", "undefined", "empty", "txt", "text"):
                result["magika"] = label
        except Exception:
            pass

    print(json.dumps(result))


main()
