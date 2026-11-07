"""What an engine is: what it can do, and the exact command that does it.

A command is always an argument list, never a shell string. File paths in it are fixed names built by
the supervisor from the job id and the catalog, so nothing the user typed can reach a command line.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

PY = "/opt/py/bin/python"
SYS_PY = "/usr/bin/python3"
TOOLS = "/app/worker/tools"


@dataclass
class Step:
    src: Path
    src_fmt: str
    dst_fmt: str
    src_ext: str
    out_ext: str
    out: Path
    work: Path
    home: Path
    tmp: Path
    threads: int
    max_bytes: int

    @property
    def output(self) -> Path:
        return self.out / f"output.{self.out_ext}"


class Engine:
    name = ""
    binary: str | None = None
    # Capture the tool's stdout as the output file.
    stdout = False
    # Hard cap on address space. Unset for anything that reserves large virtual ranges (the JVM,
    # QtWebEngine), which the container's memory limit covers instead.
    address_space: int | None = None
    # Extra Landlock grants, on top of the job's own folder.
    extra_rules: dict | None = None

    def available(self) -> bool:
        return self.binary is None or shutil.which(self.binary) is not None

    def describe(self, db) -> list[dict]:
        """Called once at image build. Returns catalog groups."""
        raise NotImplementedError

    def export_data(self) -> dict:
        """Anything learned at build time that commands() needs later."""
        return {}

    def load_data(self, data: dict) -> None:
        pass

    def env(self, step: Step) -> dict:
        return {}

    def prepare(self, step: Step) -> None:
        """Runs in the supervisor before commands(). May only write inside step.work, which is new."""

    def select(self, files: list[Path]) -> list[Path]:
        """Which of the files the tool left in the output folder are the actual result."""
        return files

    def commands(self, step: Step) -> list[list[str]]:
        raise NotImplementedError


def group(engine: str, reads: list[str], writes: list[str], grade: str = "auto", *,
          terminal: bool = False, priority: float = 0.0) -> dict:
    return {
        "engine": engine,
        "reads": sorted(set(reads)) if "*" not in reads else ["*"],
        "writes": sorted(set(writes)),
        "grade": grade,
        "terminal": terminal,
        "priority": priority,
    }


class Simple(Engine):
    """An engine fully described by a table, for tools with no way to list what they support.

    table: (reads, writes, grade[, options]). Entries are file extensions.
    """

    def __init__(self, name, binary, table, argv, *, stdout=False, env=None, address_space=None, priority=0.0, keep=None, prepare=None):
        self.keep = keep
        self._prepare = prepare
        self.name = name
        self.binary = binary
        self.table = table
        self._argv = argv
        self.stdout = stdout
        self._env = env
        self.address_space = address_space
        self.priority = priority

    def describe(self, db):
        groups = []
        for row in self.table:
            reads, writes, grade = row[0], row[1], row[2]
            options = row[3] if len(row) > 3 else {}
            read_ids = ["*"] if reads == ["*"] else db.ids(reads)
            groups.append(group(self.name, read_ids, db.ids(writes), grade,
                                terminal=options.get("terminal", False),
                                priority=options.get("priority", self.priority)))
        return groups

    def env(self, step):
        return self._env(step) if self._env else {}

    def select(self, files):
        return [f for f in files if f.suffix in self.keep] if self.keep else files

    def prepare(self, step):
        if self._prepare:
            self._prepare(step)

    def commands(self, step):
        result = self._argv(step)
        return result if result and isinstance(result[0], (list, tuple)) else [result]
