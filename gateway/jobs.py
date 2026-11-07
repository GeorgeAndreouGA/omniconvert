"""Jobs, held in memory and nowhere else.

An upload or a result is an unnamed file (O_TMPFILE) in a RAM-only mount. It has no path, so nothing can
list it or open it by name, and closing its descriptor frees it. If this process dies, the kernel closes
them all. There's no folder of jobs, no metadata on disk, and nothing to clean up after a restart,
because a restart is the cleanup.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

log = logging.getLogger("omniconvert.gateway")

JOB_ID = re.compile(r"^[A-Za-z0-9_-]{43}$")
FORMAT_ID = re.compile(r"^[a-z0-9][a-z0-9._+-]{0,31}$")
ALIVE_NAME = ".alive"


def new_job_id() -> str:
    return secrets.token_urlsafe(32)


# ---- what the worker may say, which is checked strictly: it runs the hostile parsers ----
class Step(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")
    engine: str = Field(max_length=40)
    from_: str = Field(alias="from", max_length=32)
    to: str = Field(max_length=32)


class Route(BaseModel):
    grade: Literal["exact", "lossy", "extract", "package"]
    steps: list[Step] = Field(max_length=8)


class Hello(BaseModel):
    model_config = ConfigDict(extra="ignore")
    isolation: str = Field(default="unknown", max_length=32)
    catalog: dict


class Progress(BaseModel):
    model_config = ConfigDict(extra="ignore")
    step: int = Field(default=0, ge=0, le=8)
    route: Route | None = None
    detected: str | None = Field(default=None, max_length=32)


class Failure(BaseModel):
    model_config = ConfigDict(extra="ignore")
    error: str = Field(max_length=500)
    route: Route | None = None


class Output(Progress):
    size: int = Field(ge=0)
    bundle: bool = False
    out_ext: str = Field(max_length=32, pattern=FORMAT_ID.pattern)


@dataclass(eq=False)
class Job:
    id: str
    filename: str
    to: str
    hint: str | None
    owner: str
    size: int
    fd: int | None  # the input until the worker has it, then the result once it's done
    state: Literal["queued", "running", "done", "failed"] = "queued"
    step: int = 0
    route: dict | None = None
    detected: str | None = None
    error: str | None = None
    out_ext: str | None = None
    bundle: bool = False
    expires_at: float | None = None
    deadline: float | None = None
    cancelled: bool = False


class JobStore:
    def __init__(self, root: Path, retention_seconds: int):
        self.root = root
        self.retention_seconds = retention_seconds
        self.jobs: dict[str, Job] = {}
        self.queue: list[str] = []
        self.uploading: dict[str, str] = {}
        self.arrived = asyncio.Event()

    def prepare(self) -> None:
        os.umask(0o077)
        self.arrived = asyncio.Event()
        # Fails here rather than on the first upload if the mount can't hold unnamed files.
        os.close(self.new_file())
        self.touch_alive()

    def new_file(self) -> int:
        return os.open(self.root, os.O_TMPFILE | os.O_RDWR | os.O_CLOEXEC, 0o600)

    def free_bytes(self) -> int:
        stats = os.statvfs(self.root)
        return stats.f_bavail * stats.f_frsize

    def touch_alive(self) -> None:
        """The container's health check reads this, since nothing here can open a connection to test."""
        fd = os.open(self.root / ALIVE_NAME, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            os.utime(fd)
        finally:
            os.close(fd)

    # ---- the line ----
    def add(self, job: Job) -> None:
        self.jobs[job.id] = job
        self.queue.append(job.id)
        self.arrived.set()

    async def next_queued(self) -> Job:
        while True:
            while self.queue:
                job = self.jobs.get(self.queue.pop(0))
                if job is not None:
                    return job
            self.arrived.clear()
            await self.arrived.wait()

    def position(self, job: Job) -> int:
        try:
            return self.queue.index(job.id) + 1
        except ValueError:
            return 1  # already being handed to the worker

    def waiting(self) -> int:
        return len(self.queue) + len(self.uploading)

    def jobs_for(self, client: str) -> int:
        unfinished = sum(1 for job in self.jobs.values() if job.owner == client and job.state in ("queued", "running"))
        return unfinished + sum(1 for owner in self.uploading.values() if owner == client)

    # ---- letting go ----
    def finish(self, job: Job, state: Literal["done", "failed"]) -> None:
        job.state = state
        job.expires_at = time.monotonic() + self.retention_seconds

    def drop(self, job: Job) -> None:
        """Forget a job and free its memory. Safe to call twice."""
        if self.jobs.get(job.id) is job:
            del self.jobs[job.id]
        if job.id in self.queue:
            self.queue.remove(job.id)
        fd, job.fd = job.fd, None
        if fd is not None:
            os.close(fd)

    def expire(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        for job in list(self.jobs.values()):
            if job.expires_at is not None and now >= job.expires_at:
                self.drop(job)
