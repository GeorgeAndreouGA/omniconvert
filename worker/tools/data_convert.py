"""Tables between formats. Runs inside the sandbox.

Pickle is never read: pandas and NumPy both support it, and loading one runs arbitrary code. So there
is no read_pickle here, read_hdf is avoided for the same reason, and np.load has allow_pickle=False.
"""

import json
import sqlite3
import sys
from pathlib import Path

import pandas as pd


def read_tables(path, fmt):
    """A list of (name, DataFrame)."""
    if fmt == "csv":
        return [("data", pd.read_csv(path, dtype_backend="numpy_nullable"))]
    if fmt == "tsv":
        return [("data", pd.read_csv(path, sep="\t", dtype_backend="numpy_nullable"))]
    if fmt in ("xlsx", "xls", "xlsb", "ods"):
        # Calamine is one memory-safe reader for all four, and it never evaluates anything.
        return list(pd.read_excel(path, sheet_name=None, engine="calamine").items())
    if fmt == "json":
        # Real JSON is often nested, or a single object. Flattened, it still makes a useful table.
        with open(path, encoding="utf-8") as fh:
            loaded = json.load(fh)
        return [("data", pd.json_normalize(loaded if isinstance(loaded, list) else [loaded]))]
    if fmt == "jsonl":
        return [("data", pd.read_json(path, lines=True))]
    if fmt == "parquet":
        return [("data", pd.read_parquet(path))]
    if fmt == "feather":
        return [("data", pd.read_feather(path))]
    if fmt == "orc":
        return [("data", pd.read_orc(path))]
    if fmt == "dta":
        return [("data", pd.read_stata(path))]
    if fmt == "sas7bdat":
        return [("data", pd.read_sas(path))]
    if fmt == "sav":
        import pyreadstat
        return [("data", pyreadstat.read_sav(path)[0])]
    if fmt == "sqlite":
        # Read-only and immutable: nothing in the file can write anything or load an extension.
        conn = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
        conn.execute("PRAGMA trusted_schema=OFF")
        names = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        tables = []
        for name in names:
            quoted = '"' + name.replace('"', '""') + '"'
            tables.append((name, pd.read_sql_query(f"SELECT * FROM {quoted}", conn)))
        conn.close()
        return tables
    if fmt == "mat":
        from scipy.io import loadmat
        found = loadmat(path)
        tables = []
        for name, value in found.items():
            if name.startswith("__"):
                continue
            tables.append((name, _array_frame(value)))
        return tables
    if fmt == "npy":
        import numpy as np
        return [("data", _array_frame(np.load(path, allow_pickle=False)))]
    if fmt == "hdf5":
        import h5py
        tables = []
        with h5py.File(path, "r") as store:
            def visit(name, node):
                if isinstance(node, h5py.Dataset) and node.ndim <= 2:
                    tables.append((name.replace("/", "_"), _array_frame(node[()])))
            store.visititems(visit)
        return tables
    raise ValueError(fmt)


def _array_frame(value):
    import numpy as np
    array = np.squeeze(np.asarray(value))
    if array.ndim == 0:
        array = array.reshape(1)
    if array.ndim > 2:
        array = array.reshape(array.shape[0], -1)
    return pd.DataFrame(array)


def safe_name(name):
    cleaned = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(name))[:60]
    return cleaned or "table"


def write_one(frame, path, fmt):
    if fmt == "csv":
        frame.to_csv(path, index=False)
    elif fmt == "tsv":
        frame.to_csv(path, sep="\t", index=False)
    elif fmt == "json":
        frame.to_json(path, orient="records", indent=2, force_ascii=False, date_format="iso")
    elif fmt == "jsonl":
        frame.to_json(path, orient="records", lines=True, force_ascii=False, date_format="iso")
    elif fmt in ("parquet", "feather", "orc"):
        # These formats need text column names.
        frame = frame.copy()
        frame.columns = [str(c) for c in frame.columns]
        {"parquet": frame.to_parquet, "feather": frame.to_feather, "orc": frame.to_orc}[fmt](path)
    elif fmt == "dta":
        frame = frame.copy()
        frame.columns = [f"v{c}" if not str(c)[:1].isalpha() else str(c) for c in frame.columns]
        frame.to_stata(path, write_index=False, version=118)
    else:
        raise ValueError(fmt)


def main():
    source, output, src_fmt, dst_fmt, out_dir = sys.argv[1:6]
    tables = read_tables(source, src_fmt)
    if not tables:
        print("no tables found", file=sys.stderr)
        sys.exit(1)

    if dst_fmt == "xlsx":
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            used = set()
            for name, frame in tables:
                sheet = safe_name(name)[:31]
                while sheet in used:
                    sheet = (sheet[:28] + f"_{len(used)}")[:31]
                used.add(sheet)
                frame.to_excel(writer, sheet_name=sheet, index=False)
        return
    if dst_fmt == "sqlite":
        conn = sqlite3.connect(output)
        for name, frame in tables:
            frame.to_sql(safe_name(name), conn, index=False)
        conn.close()
        return
    if len(tables) == 1:
        write_one(tables[0][1], output, dst_fmt)
        return
    # Several sheets or tables and a one-table format: one file each, returned as a zip.
    for name, frame in tables:
        write_one(frame, Path(out_dir) / f"{safe_name(name)}.{Path(output).suffix.lstrip('.')}", dst_fmt)


main()
