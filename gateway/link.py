"""The gateway's end of the socket to the worker: hands over one job at a time and takes back the result.

The queue and every finished result stay on this side, so a malicious file that takes over a converter
can only ever see the one file it arrived in. It has no other job to read, list, change or delete,
whatever the kernel supports.

The worker is treated as hostile. Each frame's size is checked before it's read, each message is
validated, and anything out of order ends the session.

A session lasts as long as the worker's container does. Its PID 1 holds this connection and exits when
it closes, which kills every process in the container. So when a session ends, all that could reconnect
is a dying process, and refusing anyone for a few seconds afterwards leaves it no way to pose as the
worker and be handed the next person's file.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import struct
import time
from functools import lru_cache

from pydantic import ValidationError

from gateway.config import Settings
from gateway.jobs import Failure, Hello, Job, JobStore, Output, Progress
from shared import link as wire
from shared.router import Catalog

log = logging.getLogger("omniconvert.gateway")

HELLO_TIMEOUT_SECONDS = 30
BEAT_TIMEOUT_SECONDS = 60
COOLDOWN_SECONDS = 5
DEADLINE_GRACE_SECONDS = 120
ROOM_WAIT_SECONDS = 60
REFUSAL_LOG_SECONDS = 60

STOPPED = "The converter stopped during this conversion. Try again, or try another format."
NO_ROOM = "The server's memory is full, so this result couldn't be kept. Download other results or try again in a few minutes."
UNEXPECTED = "This conversion failed unexpectedly. Try again, or try another format."


class LinkError(Exception):
    pass


async def read_frame(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    kind, length = wire.HEADER.unpack(await reader.readexactly(wire.HEADER.size))
    if length > wire.limit_for(kind):
        raise LinkError(f"frame of kind {kind} and {length} bytes refused")
    return kind, await reader.readexactly(length) if length else b""


def parse(model, payload: bytes):
    try:
        return model.model_validate_json(payload)
    except ValidationError:
        raise LinkError(f"invalid {model.__name__} message") from None


def write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def route_dict(message) -> dict | None:
    return message.route.model_dump(by_alias=True) if message.route else None


class WorkerLink:
    def __init__(self, store: JobStore, settings: Settings):
        self.store = store
        self.settings = settings
        self.cooldown = COOLDOWN_SECONDS
        self.catalog: Catalog | None = None
        self.routes = None
        self.isolation = "unknown"
        self.online = False
        self.session_open = False
        self.not_before = 0.0
        self.last_refusal_log = 0.0
        self.server = None
        self.writer: asyncio.StreamWriter | None = None
        self.ready = asyncio.Event()
        self.current: Job | None = None
        self.input_sent = False
        self.out_fd: int | None = None
        self.out_expected = 0
        self.out_received = 0
        self.out_meta: Output | None = None
        self.discarding = False

    # ---- the socket ----
    async def start(self) -> None:
        self.ready = asyncio.Event()
        # Anything still alive from before this process started gets time to die first.
        self.not_before = time.monotonic() + self.cooldown
        path = self.settings.socket_path
        self.server = await asyncio.start_unix_server(self._connection, path=path, backlog=4)
        # Group-writable only: the folder's setgid bit makes its group the worker's, and the worker
        # needs write permission on a socket to connect to it. It mounts the folder read-only.
        os.chmod(path, 0o660)

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
        if self.writer is not None:
            self.writer.close()

    def _refuse(self, writer: asyncio.StreamWriter, why: str) -> None:
        # Worth knowing about, but a process hammering the socket mustn't fill the disk with log lines.
        now = time.monotonic()
        if now - self.last_refusal_log > REFUSAL_LOG_SECONDS:
            self.last_refusal_log = now
            log.warning("refused a connection on the worker socket: %s", why)
        writer.close()

    def _peer_uid(self, writer: asyncio.StreamWriter) -> int | None:
        try:
            sock = writer.get_extra_info("socket")
            _pid, uid, _gid = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            return uid
        except (OSError, AttributeError, struct.error):
            return None

    async def _connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self.session_open:
            self._refuse(writer, "a session is already open")
            return
        if time.monotonic() < self.not_before:
            writer.close()  # normal after any restart: the worker just tries again
            return
        if self._peer_uid(writer) != self.settings.worker_uid:
            self._refuse(writer, "wrong user")
            return
        self.session_open = True
        self.writer = writer
        try:
            writer.write(wire.frame(wire.WELCOME))
            await self._session(reader, writer)
        except (LinkError, asyncio.IncompleteReadError, ConnectionError, TimeoutError, OSError) as err:
            log.warning("worker session ended: %s", err if isinstance(err, LinkError) else type(err).__name__)
        finally:
            self._end_session()
            writer.close()

    def _end_session(self) -> None:
        self.online = False
        self.session_open = False
        self.writer = None
        self.ready.clear()
        self.not_before = time.monotonic() + self.cooldown
        self._close_output()
        job, self.current = self.current, None
        if job is not None and job.state not in ("done", "failed"):
            self._fail(job, STOPPED)

    async def _session(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        kind, payload = await asyncio.wait_for(read_frame(reader), HELLO_TIMEOUT_SECONDS)
        if kind != wire.HELLO:
            raise LinkError("expected a hello")
        hello = parse(Hello, payload)
        self._accept_catalog(hello.catalog)
        self.isolation = hello.isolation
        sender = asyncio.create_task(self._dispatch(writer))
        try:
            while True:
                kind, payload = await asyncio.wait_for(read_frame(reader), BEAT_TIMEOUT_SECONDS)
                await self._on_frame(kind, payload)
                self._check_deadline()
                if sender.done():
                    sender.result()
        finally:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)

    def _accept_catalog(self, data: dict) -> None:
        # A bad one ends the session and leaves the last good copy in place.
        try:
            catalog = Catalog(data)
        except (KeyError, TypeError, ValueError, AttributeError):
            raise LinkError("catalog failed validation") from None
        self.catalog = catalog
        self.routes = lru_cache(maxsize=256)(catalog.routes_from)

    def _check_deadline(self) -> None:
        # The worker enforces the time limit itself. This is the backstop if it can't or won't.
        job = self.current
        if job is not None and job.deadline is not None and time.monotonic() > job.deadline:
            raise LinkError("a job passed its deadline with no result")

    # ---- sending ----
    async def _dispatch(self, writer: asyncio.StreamWriter) -> None:
        while True:
            await self.ready.wait()
            job = await self.store.next_queued()
            self.ready.clear()
            await self._send_input(job, writer)

    async def _send_input(self, job: Job, writer: asyncio.StreamWriter) -> None:
        self.current = job
        self.input_sent = False
        job.deadline = time.monotonic() + self.settings.job_timeout_seconds + DEADLINE_GRACE_SECONDS
        # What it's told: the target and the format hint. Never the job id, the name or the address.
        writer.write(wire.message(wire.JOB, {"to": job.to, "hint": job.hint, "size": job.size}))
        await writer.drain()
        fd, job.fd = job.fd, None
        try:
            offset = 0
            while fd is not None and offset < job.size and not job.cancelled:
                chunk = await asyncio.to_thread(os.pread, fd, wire.MAX_DATA, offset)
                if not chunk:
                    break
                writer.write(wire.frame(wire.DATA, chunk))
                await writer.drain()
                offset += len(chunk)
        finally:
            # Handed over, so this copy is freed. The worker now holds the only one.
            if fd is not None:
                os.close(fd)
        self.input_sent = True
        writer.write(wire.frame(wire.CANCEL if job.cancelled else wire.END))
        await writer.drain()

    def cancel(self, job: Job) -> None:
        job.cancelled = True
        if self.current is job and self.input_sent and self.writer is not None:
            self.writer.write(wire.frame(wire.CANCEL))

    # ---- receiving ----
    async def _on_frame(self, kind: int, payload: bytes) -> None:
        if kind == wire.BEAT:
            return
        if kind == wire.READY:
            self._job_over()
            self.online = True
            self.ready.set()
            return
        job = self.current
        if job is None:
            raise LinkError(f"kind {kind} arrived with no job running")
        if kind == wire.STATUS:
            self._on_status(job, parse(Progress, payload))
        elif kind == wire.FAILED:
            failure = parse(Failure, payload)
            self._close_output()
            self.discarding = False
            if failure.route:
                job.route = route_dict(failure)
            self._fail(job, failure.error)
        elif kind == wire.OUTPUT:
            await self._on_output(job, parse(Output, payload))
        elif kind == wire.DATA:
            await self._on_data(job, payload)
        elif kind == wire.END:
            self._on_end(job)
        else:
            raise LinkError(f"kind {kind} isn't valid from the worker")

    def _on_status(self, job: Job, progress: Progress) -> None:
        if job.state in ("done", "failed"):
            raise LinkError("status after the job ended")
        job.state = "running"
        job.step = progress.step
        job.detected = progress.detected
        if progress.route:
            job.route = route_dict(progress)

    async def _on_output(self, job: Job, output: Output) -> None:
        if self.out_fd is not None or self.discarding or job.state in ("done", "failed"):
            raise LinkError("a second output for one job")
        if output.size > self.settings.max_output_bytes:
            raise LinkError("an output past the limit")  # the worker checks this, so it's lying or broken
        if job.cancelled:
            self.discarding = True
            return
        if not await self._wait_for_room(output.size):
            self._fail(job, NO_ROOM)
            self.discarding = True
            return
        self.out_fd = self.store.new_file()
        self.out_expected = output.size
        self.out_received = 0
        self.out_meta = output

    async def _wait_for_room(self, size: int) -> bool:
        # Downloads and expiries free memory, so a full store is usually only full for a moment.
        for _ in range(ROOM_WAIT_SECONDS):
            if self.store.free_bytes() >= size + wire.MAX_DATA:
                return True
            await asyncio.sleep(1)
        return self.store.free_bytes() >= size + wire.MAX_DATA

    async def _on_data(self, job: Job, payload: bytes) -> None:
        if self.discarding:
            return
        if self.out_fd is None:
            raise LinkError("data with no output open")
        if job.cancelled:
            self._close_output()
            self.discarding = True
            return
        self.out_received += len(payload)
        if self.out_received > self.out_expected:
            raise LinkError("more output than it announced")
        try:
            await asyncio.to_thread(write_all, self.out_fd, payload)
        except OSError:
            self._close_output()
            self._fail(job, NO_ROOM)
            self.discarding = True

    def _on_end(self, job: Job) -> None:
        if self.discarding:
            self.discarding = False
            return
        if self.out_fd is None:
            raise LinkError("end with no output open")
        if self.out_received != self.out_expected:
            raise LinkError("less output than it announced")
        output, self.out_meta = self.out_meta, None
        if job.cancelled:
            self._close_output()
            return
        job.fd, self.out_fd = self.out_fd, None
        job.size = self.out_received
        job.step = output.step
        job.detected = output.detected
        job.bundle = output.bundle
        job.out_ext = output.out_ext
        if output.route:
            job.route = route_dict(output)
        self.store.finish(job, "done")

    def _job_over(self) -> None:
        job, self.current = self.current, None
        self._close_output()
        self.discarding = False
        if job is not None and job.state not in ("done", "failed"):
            self._fail(job, UNEXPECTED)

    def _close_output(self) -> None:
        fd, self.out_fd = self.out_fd, None
        if fd is not None:
            os.close(fd)

    def _fail(self, job: Job, error: str) -> None:
        job.error = error
        self.store.finish(job, "failed")
