"""Export every table of an Access database to its own CSV. Runs inside the sandbox."""

import subprocess
import sys
from pathlib import Path


def main():
    source, out_dir = sys.argv[1:3]
    listing = subprocess.run(["mdb-tables", "-1", source], capture_output=True, text=True, timeout=120, check=True, stdin=subprocess.DEVNULL)
    tables = [line for line in listing.stdout.splitlines() if line.strip()]
    if not tables:
        print("no tables found", file=sys.stderr)
        sys.exit(1)
    for index, table in enumerate(tables):
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in table)[:60] or f"table{index}"
        with open(Path(out_dir) / f"{safe}.csv", "wb") as out:
            subprocess.run(["mdb-export", source, table], stdout=out, check=True, stdin=subprocess.DEVNULL)


main()
