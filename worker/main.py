"""The supervisor: takes one job at a time from the gateway and runs each conversion step in the sandbox.

This container only ever holds the job in progress. The gateway keeps the queue and the finished results,
hands over one file, and takes back one result, and everything here is wiped before the next file comes.
So a malicious file that takes over a converter has nothing else in reach: no other jobs, no job ids, no
file names.

This process parses nothing untrusted. Type detection, conversion and output checks all run inside the
sandbox, and what comes back from there is treated as hostile: bounded in size, strictly validated, and
read without following symlinks.
"""

from __future__ import annotations

import ctypes
import errno
import json
import logging
import os
import re
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from shared import link as wire
from shared.router import Catalog
from worker.engines import load_engines
from worker.engines.base import Step
from worker.sandbox_exec import landlock_abi
from worker.verify import output_matches

log = logging.getLogger("omniconvert.worker")

WORK = Path(os.environ.get("WORK_DIR", "/work"))
TMP = Path("/tmp")
ALIVE = TMP / ".alive"

APP = Path(os.environ.get("APP_DIR", "/app"))
SANDBOX = APP / "worker" / "sandbox_exec.py"
DETECT = APP / "worker" / "tools" / "detect.py"
PYTHON = "/opt/py/bin/python"

FORMAT_ID = re.compile(r"^[a-z0-9][a-z0-9._+-]{0,31}$")
MAX_OUTPUT_FILES = 10000
PR_SET_DUMPABLE = 4
# What the supervisor and a small converter need, on top of what the RAM mounts can hold.
OWN_MEMORY_BYTES = 128 * 1024 * 1024

GENERIC_ERROR = "{engine} could not convert this file. It may be damaged, password-protected, or use a feature it doesn't support."
NO_MEMORY = "This conversion needed more memory than the server has free. Try a smaller file or a simpler format."
UNEXPECTED = "This conversion failed unexpectedly. Try again, or try another format."


class JobFailed(Exception):
    """Carries a message written for the person who uploaded the file."""


class JobCancelled(Exception):
    pass


class LinkClosed(Exception):
    pass


@dataclass
class Config:
    threads: int
    timeout: int
    max_output: int
    landlock_abi: int
    log_converter_output: bool

    @property
    def isolation(self) -> str:
        return f"landlock-{self.landlock_abi}" if self.landlock_abi else "one-job-at-a-time"


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, "") or default)


def memory_limit_bytes() -> int | None:
    """The limit really in force. Docker only warns when it can't apply one, so ask the kernel."""
    for path, ceiling in (("/sys/fs/cgroup/memory.max", None), ("/sys/fs/cgroup/memory/memory.limit_in_bytes", 2**60)):
        try:
            raw = Path(path).read_text().strip()
        except OSError:
            continue
        if raw == "max":
            return None
        value = int(raw)
        if ceiling is not None and value >= ceiling:
            return None
        return value
    return None


def mount_bytes(path: Path) -> int:
    stats = os.statvfs(path)
    return stats.f_blocks * stats.f_frsize


def load_config() -> Config:
    if os.geteuid() == 0:
        sys.exit("omniconvert-worker: refusing to run as root")

    limit = memory_limit_bytes()
    if limit is None and os.environ.get("WORKER_ALLOW_NO_MEMORY_LIMIT") != "1":
        sys.exit(
            "omniconvert-worker: no memory limit is in force, so a runaway conversion could take down "
            "the whole machine. On a Raspberry Pi add `cgroup_enable=memory cgroup_memory=1` to "
            "/boot/firmware/cmdline.txt and reboot. scripts/preflight.sh checks this."
        )

    # Everything a job writes is in memory. If the mounts could outgrow the limit, a large job would get
    # the supervisor killed instead of a clean "out of memory" for that one file.
    mounts = mount_bytes(WORK) + mount_bytes(TMP)
    if limit is not None and limit < mounts + OWN_MEMORY_BYTES:
        sys.exit(f"omniconvert-worker: WORKER_MEMORY ({limit // 1024**2} MiB) must be at least WORKER_WORK_SIZE plus "
                 f"WORKER_TMP_SIZE ({mounts // 1024**2} MiB) plus {OWN_MEMORY_BYTES // 1024**2} MiB. Change them in .env.")

    mode = os.environ.get("WORKER_LANDLOCK", "auto").lower()
    abi = landlock_abi()
    if mode == "off":
        abi = 0
    elif mode == "required" and abi == 0:
        sys.exit("omniconvert-worker: WORKER_LANDLOCK=required, but this kernel has no Landlock")
    elif mode not in ("auto", "required"):
        sys.exit(f"omniconvert-worker: WORKER_LANDLOCK must be auto, required or off, got {mode!r}")

    return Config(
        threads=max(1, _env_int("WORKER_THREADS", 3)),
        timeout=max(1, _env_int("JOB_TIMEOUT_SECONDS", 1800)),
        max_output=_env_int("MAX_OUTPUT_BYTES", 1536 * 1024**2),
        landlock_abi=abi,
        # Off by default: what a converter prints can quote the file it's reading, and logs go to disk.
        log_converter_output=os.environ.get("LOG_CONVERTER_OUTPUT") == "1",
    )


def remove_entry(entry: os.DirEntry) -> None:
    """A folder or a file. rmtree quietly does nothing to a file, and a hostile job can leave either."""
    if entry.is_dir(follow_symlinks=False):
        shutil.rmtree(entry.path, ignore_errors=True)
    else:
        try:
            os.unlink(entry.path)
        except OSError:
            pass


def clean_tail(raw: bytes) -> str:
    text = raw[-2000:].decode("utf-8", "replace")
    return "".join(c if c.isprintable() or c == "\n" else "?" for c in text)


class Link:
    """The socket to the gateway. Frames are written whole, so threads can't interleave them."""

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.lock = threading.Lock()

    def send(self, kind: int, payload: bytes = b"") -> None:
        data = wire.frame(kind, payload)
        with self.lock:
            self.sock.sendall(data)

    def send_message(self, kind: int, data: dict) -> None:
        self.send(kind, json.dumps(data).encode())

    def _read(self, size: int) -> bytes:
        parts = []
        while size > 0:
            part = self.sock.recv(min(size, wire.MAX_DATA))
            if not part:
                raise LinkClosed()
            parts.append(part)
            size -= len(part)
        return b"".join(parts)

    def recv(self) -> tuple[int, bytes]:
        kind, length = wire.HEADER.unpack(self._read(wire.HEADER.size))
        if length > wire.limit_for(kind):
            raise LinkClosed()
        return kind, self._read(length) if length else b""


@dataclass
class Job:
    number: int
    to: str
    hint: str | None
    cancel: threading.Event = field(default_factory=threading.Event)
    # Random, so nothing about the folder says anything about the job.
    work_name: str = field(default_factory=lambda: secrets.token_hex(16))
    route: dict | None = None
    input_fd: int | None = None
    refused: str | None = None
    running: bool = False


class Worker:
    def __init__(self, config: Config, link: Link):
        self.config = config
        self.link = link
        self.engines = load_engines(APP / "engine_data.json")
        self.catalog_data = json.loads((APP / "catalog.json").read_text(encoding="utf-8"))
        self.catalog = Catalog(self.catalog_data)
        self.ext_to_id = {}
        for fmt in self.catalog.formats.values():
            for ext in fmt["exts"]:
                self.ext_to_id.setdefault(ext, fmt["id"])
        self.job: Job | None = None
        self.jobs_seen = 0

    # ---- staying alive ----
    def beat_forever(self) -> None:
        # On its own thread, so a long job can't make the worker look dead.
        while True:
            try:
                self.link.send(wire.BEAT)
            except OSError:
                os._exit(1)  # the gateway is gone, and PID 1 is taking the container down
            try:
                # For Docker's health check. Never through a link or into a FIFO a job left here.
                fd = os.open(ALIVE, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0o600)
                try:
                    os.utime(fd)
                finally:
                    os.close(fd)
            except OSError:
                pass
            time.sleep(2)

    # ---- nothing survives a job ----
    def clean_slate(self) -> None:
        """Every process gone first, so nothing can write behind the wipe, then every writable place emptied."""
        for _ in range(40):
            try:
                os.kill(-1, signal.SIGKILL)  # skips this process and PID 1
            except ProcessLookupError:
                break
            except PermissionError:
                break
            time.sleep(0.05)
        for folder in (WORK, TMP):
            try:
                with os.scandir(folder) as entries:
                    for entry in list(entries):
                        if entry.path != str(ALIVE):
                            remove_entry(entry)
            except OSError:
                log.exception("couldn't empty a work folder")

    # ---- the sandbox ----
    def limits(self, engine_as: int | None) -> dict:
        limits = {
            "core": 0,
            "fsize": self.config.max_output,
            "cpu": self.config.timeout * self.config.threads + 60,
            "nofile": 4096,
            # Well under the container's pids limit, so a fork bomb hits this first and the
            # supervisor can still start processes and clean up.
            "nproc": 384,
        }
        if engine_as:
            limits["as"] = engine_as
        return limits

    def landlock_rules(self, workdir: Path, extra: dict | None = None) -> dict | None:
        if not self.config.landlock_abi:
            return None
        return {
            **(extra or {}),
            "rx": ["/usr", "/bin", "/sbin", "/lib", "/lib64", "/opt"],
            "ro": ["/etc", "/app", "/proc", "/sys/fs/cgroup", "/sys/devices/system/cpu", "/var/lib", "/var/cache",
                   "/dev/urandom", "/dev/random", "/dev/zero"],
            "rw": [str(workdir), "/dev/null"],
            "tmpfile": [str(TMP)],
        }

    def run_sandboxed(self, job: Job, argv: list[str], cwd: Path, workdir: Path, *,
                      env: dict | None = None, stdout_path: Path | None = None,
                      address_space: int | None = None, extra_rules: dict | None = None,
                      deadline: float, capture: bool = False) -> tuple[int, bytes, bytes]:
        spec = {"limits": self.limits(address_space), "landlock": self.landlock_rules(workdir, extra_rules)}
        home = workdir / "home"
        tmp = workdir / "tmp"
        clean_env = {
            "PATH": "/opt/libredwg/bin:/opt/py/bin:/usr/local/bin:/usr/bin:/bin",
            "HOME": str(home),
            "TMPDIR": str(tmp),
            "TEMP": str(tmp),
            "TMP": str(tmp),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "XDG_CACHE_HOME": str(home / ".cache"),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_DATA_HOME": str(home / ".local"),
            "XDG_RUNTIME_DIR": str(tmp),
            "OMP_NUM_THREADS": str(self.config.threads),
            "OMP_THREAD_LIMIT": str(self.config.threads),
            **(env or {}),
        }
        # This process is outside the sandbox, and earlier steps could write anywhere in workdir. So
        # nothing here may follow a symlink a job planted, or the job could use this process to
        # write outside its own folder: new names only, and never through a link.
        err_path = workdir / f"stderr-{secrets.token_hex(8)}.log"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        out_target = subprocess.DEVNULL
        out_fd = None
        if stdout_path is not None:
            out_fd = os.open(stdout_path, flags, 0o600)
            out_target = out_fd
        elif capture:
            out_target = subprocess.PIPE
        err_fd = os.open(err_path, flags, 0o600)
        try:
            proc = subprocess.Popen(
                ["/usr/bin/python3", "-I", "-S", str(SANDBOX), *argv],
                cwd=cwd, env={**clean_env, "OMNI_SANDBOX_SPEC": json.dumps(spec)},
                stdin=subprocess.DEVNULL, stdout=out_target, stderr=err_fd,
                start_new_session=True, close_fds=True,
            )
        finally:
            os.close(err_fd)
            if out_fd is not None:
                os.close(out_fd)

        out_bytes = b""
        try:
            while True:
                if capture:
                    # Detection replies are tiny, so reading the pipe can't fill up and block this.
                    try:
                        out_bytes, _ = proc.communicate(timeout=0.25)
                        break
                    except subprocess.TimeoutExpired:
                        pass
                else:
                    try:
                        proc.wait(timeout=0.25)
                        break
                    except subprocess.TimeoutExpired:
                        pass
                if job.cancel.is_set():
                    raise JobCancelled()
                if time.monotonic() > deadline:
                    raise JobFailed(f"This conversion took longer than the {self.config.timeout // 60} minute limit and was stopped.")
        finally:
            self.kill_job_processes(proc)
        stderr = b""
        try:
            fd = os.open(err_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
            with os.fdopen(fd, "rb") as fh:
                size = os.fstat(fh.fileno()).st_size
                fh.seek(max(0, size - 4000))
                stderr = fh.read(4000)
            err_path.unlink(missing_ok=True)
        except OSError:
            pass
        return proc.returncode, out_bytes[:65536], stderr

    def kill_job_processes(self, proc: subprocess.Popen) -> None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        # A hostile process can leave its session to survive that. Only one job runs here, so nothing
        # else in the container should be alive. On Linux kill(-1) skips both the caller and PID 1.
        try:
            os.kill(-1, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    def note_failure(self, job: Job, what: str, code: int, stderr: bytes) -> None:
        log.info("job %d: %s exited %s", job.number, what, code)
        if self.config.log_converter_output:
            log.info("job %d: %s said: %s", job.number, what, clean_tail(stderr))

    # ---- deciding what the file really is ----
    def detect(self, job: Job, source: Path, workdir: Path, deadline: float) -> str:
        code, out, err = self.run_sandboxed(job, [PYTHON, "-I", str(DETECT), str(source)], workdir, workdir,
                                            deadline=min(deadline, time.monotonic() + 120), capture=True)
        info = {}
        if code == 0:
            try:
                parsed = json.loads(out)
                if isinstance(parsed, dict):
                    info = parsed
            except ValueError:
                pass
        else:
            self.note_failure(job, "detection", code, err)
        return resolve_source(job.hint, info, self.catalog, self.ext_to_id)

    # ---- taking a job in ----
    def start_receiving(self, payload: bytes) -> None:
        self.jobs_seen += 1
        data = json.loads(payload)
        to, hint = data.get("to"), data.get("hint")
        job = Job(number=self.jobs_seen, to=to if isinstance(to, str) else "", hint=hint if isinstance(hint, str) and FORMAT_ID.match(hint) else None)
        self.job = job
        if not FORMAT_ID.match(job.to):
            job.refused = "This job's target format isn't valid."
            return
        workdir = WORK / job.work_name
        try:
            workdir.mkdir(mode=0o700)
            for name in ("home", "tmp", "in"):
                (workdir / name).mkdir(mode=0o700)
            for name in (".cache", ".config", ".local"):
                (workdir / "home" / name).mkdir(mode=0o700)
            job.input_fd = os.open(workdir / "in" / "input", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        except OSError:
            log.exception("job %d: couldn't prepare its folder", job.number)
            job.refused = UNEXPECTED

    def receive(self, payload: bytes) -> None:
        job = self.job
        if job is None or job.input_fd is None:
            return
        try:
            view = memoryview(payload)
            while view:
                view = view[os.write(job.input_fd, view):]
        except OSError as err:
            os.close(job.input_fd)
            job.input_fd = None
            job.refused = NO_MEMORY if err.errno == errno.ENOSPC else UNEXPECTED
            shutil.rmtree(WORK / job.work_name, ignore_errors=True)  # gives the memory back now

    def finish_receiving(self) -> None:
        job = self.job
        if job is None:
            return
        if job.input_fd is not None:
            os.fchmod(job.input_fd, 0o400)
            os.close(job.input_fd)
            job.input_fd = None
        if job.refused:
            self.link.send_message(wire.FAILED, {"error": job.refused})
            self.job_over()
            return
        job.running = True
        threading.Thread(target=self.run_job, args=(job,), daemon=True, name="job").start()

    def cancel_current(self) -> None:
        job = self.job
        if job is None:
            return
        if job.running:
            job.cancel.set()
            return
        # Still arriving: there's no conversion to stop.
        if job.input_fd is not None:
            os.close(job.input_fd)
            job.input_fd = None
        self.job_over()

    def job_over(self) -> None:
        self.job = None
        self.clean_slate()
        self.link.send(wire.READY)

    # ---- running a job ----
    def run_job(self, job: Job) -> None:
        workdir = WORK / job.work_name
        started = time.monotonic()
        failure = None
        try:
            self.convert(job, workdir, started + self.config.timeout)
        except JobCancelled:
            log.info("job %d cancelled", job.number)
        except JobFailed as err:
            log.info("job %d failed", job.number)
            failure = str(err)[:480]
        except OSError as err:
            log.warning("job %d: error %s", job.number, errno.errorcode.get(err.errno, err.errno))
            failure = NO_MEMORY if err.errno == errno.ENOSPC else UNEXPECTED
        except Exception as err:
            log.error("job %d crashed: %s", job.number, type(err).__name__)
            failure = UNEXPECTED
        try:
            if failure:
                self.link.send_message(wire.FAILED, {"error": failure, "route": job.route})
            log.info("job %d took %.1fs", job.number, time.monotonic() - started)
            self.job_over()
        except OSError:
            os._exit(1)  # the gateway is gone, and PID 1 is taking the container down

    def convert(self, job: Job, workdir: Path, deadline: float) -> None:
        # Every tool opens the input through neutral names like these, and its arguments say nothing else.
        neutral = workdir / "in" / "input"
        source = self.detect(job, neutral, workdir, deadline)
        route = self.catalog.route(source, job.to)
        if route is None:
            wanted = self.catalog.formats.get(job.to, {}).get("name", job.to)
            if source == "unknown":
                raise JobFailed(f"This file's type wasn't recognised, so it can't be converted to {wanted}. You can still package it, encode it, or pull out its details.")
            found = self.catalog.formats.get(source, {}).get("name", "this format")
            raise JobFailed(f"This file's contents are {found}, which can't be converted to {wanted}. Choose another format.")

        steps = route["steps"]
        public_route = {"grade": route["grade"], "steps": steps}
        job.route = public_route
        current = workdir / "in" / f"input.{self.ext_of(source)}"
        os.symlink("input", current)

        final_files: list[Path] = []
        for index, planned in enumerate(steps):
            if job.cancel.is_set():
                raise JobCancelled()
            self.link.send_message(wire.STATUS, {"step": index, "route": public_route, "detected": source})
            engine = self.engines.get(planned["engine"])
            if engine is None:
                raise JobFailed(f"{planned['engine']} isn't installed on this server. Choose another format.")

            # A random name and no `parents=True`: an earlier step can't have prepared this folder
            # as a symlink for the supervisor to write through.
            stepdir = workdir / f"s{index}-{secrets.token_hex(8)}"
            stepdir.mkdir(mode=0o700)
            out = stepdir / "out"
            out.mkdir(mode=0o700)
            step = Step(
                src=current,
                src_fmt=planned["from"],
                dst_fmt=planned["to"],
                src_ext=self.ext_of(planned["from"]),
                out_ext=self.ext_of(planned["to"]),
                out=out,
                work=stepdir,
                home=workdir / "home",
                tmp=workdir / "tmp",
                threads=self.config.threads,
                max_bytes=self.config.max_output,
            )
            engine.prepare(step)
            for command in engine.commands(step):
                code, _o, stderr = self.run_sandboxed(
                    job, [str(a) for a in command], stepdir, workdir,
                    env=engine.env(step),
                    stdout_path=step.output if engine.stdout else None,
                    address_space=engine.address_space,
                    extra_rules=engine.extra_rules,
                    deadline=deadline,
                )
                if code != 0:
                    self.note_failure(job, engine.name, code, stderr)
                    raise JobFailed(explain_failure(engine.name, code, stderr))

            files = engine.select(self.collect(out))
            if not files:
                raise JobFailed(GENERIC_ERROR.format(engine=engine.name))
            if index < len(steps) - 1:
                if len(files) != 1:
                    raise JobFailed(f"{engine.name} produced several files here, so this route can't continue. Choose another format.")
                current = files[0]
            else:
                final_files = files

        self.deliver(job, workdir, final_files, public_route, source, deadline)

    def ext_of(self, fid: str) -> str:
        fmt = self.catalog.formats.get(fid, {})
        return fmt.get("out_ext") or (fmt.get("exts") or [fid])[0] if fid != "unknown" else "bin"

    @staticmethod
    def collect(out: Path) -> list[Path]:
        """Regular files only. By now the job's processes are dead, so nothing can swap them."""
        found: list[Path] = []
        for root, dirs, files in os.walk(out, followlinks=False):
            dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
            for name in files:
                path = Path(root) / name
                if stat.S_ISREG(os.lstat(path).st_mode) and os.lstat(path).st_size > 0:
                    found.append(path)
                if len(found) > MAX_OUTPUT_FILES:
                    raise JobFailed("This conversion produced too many files and was stopped.")
        return sorted(found)

    def bundle(self, files: list[Path], base: Path, target: Path) -> None:
        """Zip several outputs into one download."""
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True) as archive:
            total = 0
            for path in files:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
                with os.fdopen(fd, "rb") as src, archive.open(path.relative_to(base).as_posix(), "w", force_zip64=True) as dst:
                    while chunk := src.read(1024 * 1024):
                        total += len(chunk)
                        if total > self.config.max_output:
                            raise JobFailed("This conversion produced too much data and was stopped.")
                        dst.write(chunk)

    def deliver(self, job: Job, workdir: Path, files: list[Path], route: dict, source: str, deadline: float) -> None:
        final = files[0]
        is_bundle = len(files) > 1
        if is_bundle:
            holding = workdir / f"bundle-{secrets.token_hex(8)}"
            holding.mkdir(mode=0o700)
            final = holding / "bundle.zip"
            self.bundle(files, Path(os.path.commonpath([f.parent for f in files])), final)
        else:
            mime = self.check_type(job, final, workdir, deadline)
            if not output_matches(job.to, mime):
                log.warning("job %d: output for %s has type %s", job.number, job.to, mime)
                wanted = self.catalog.formats.get(job.to, {}).get("name", job.to)
                raise JobFailed(f"The conversion produced a file that isn't really {wanted}, so it was discarded. Try another format.")

        fd = os.open(final, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(fd, "rb") as fh:
            info = os.fstat(fh.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise JobFailed(GENERIC_ERROR.format(engine=route["steps"][-1]["engine"]))
            if info.st_size > self.config.max_output:
                raise JobFailed("The result was too large and the conversion was stopped.")
            self.link.send_message(wire.OUTPUT, {
                "size": info.st_size, "bundle": is_bundle, "out_ext": self.ext_of(job.to),
                "step": len(route["steps"]), "route": route, "detected": source,
            })
            left = info.st_size
            while left > 0:
                chunk = fh.read(min(left, wire.MAX_DATA))
                if not chunk:
                    raise JobFailed(UNEXPECTED)  # the gateway hears this before any END, and drops the partial copy
                self.link.send(wire.DATA, chunk)
                left -= len(chunk)
            self.link.send(wire.END)

    def check_type(self, job: Job, path: Path, workdir: Path, deadline: float) -> str:
        code, out, _err = self.run_sandboxed(job, ["file", "--brief", "--mime-type", str(path)], workdir, workdir,
                                             deadline=min(deadline, time.monotonic() + 60), capture=True)
        if code != 0:
            return ""
        return out.decode("ascii", "replace").strip()[:100]

    # ---- the main loop ----
    def run(self) -> None:
        os.umask(0o077)
        self.clean_slate()
        self.link.send_message(wire.HELLO, {"isolation": self.config.isolation, "catalog": self.catalog_data})
        log.info("ready: %d engines, %d formats, isolation: %s, memory limit: %s MiB",
                 len(self.engines), len(self.catalog.formats), self.config.isolation, (memory_limit_bytes() or 0) // 1024**2)
        threading.Thread(target=self.beat_forever, daemon=True, name="heartbeat").start()
        self.link.send(wire.READY)
        while True:
            kind, payload = self.link.recv()
            if kind == wire.JOB:
                self.start_receiving(payload)
            elif kind == wire.DATA:
                self.receive(payload)
            elif kind == wire.END:
                self.finish_receiving()
            elif kind == wire.CANCEL:
                self.cancel_current()


# Types that only say what container or kind of text a file is. Many formats are built on these.
_CONTAINER_MIMES = {
    "application/zip", "application/x-ole-storage", "application/cdfv2", "application/encrypted",
    "application/json", "application/xml", "application/gzip", "application/x-gzip", "application/vnd.sqlite3",
    "application/x-sqlite3", "application/x-tar", "application/x-bzip2", "application/x-xz", "application/zstd",
    "application/vnd.microsoft.portable-executable", "application/x-dosexec", "application/x-bgzf",
}
# These have a signature of their own, so they're never really a zip or plain text under another name.
_OWN_SIGNATURE_FAMILIES = {"image", "raw", "audio", "video"}
_PLAYLIST_MIMES = {"audio/x-mpegurl", "application/vnd.apple.mpegurl", "application/x-mpegurl", "audio/mpegurl", "application/dash+xml", "application/sdp", "audio/x-scpls"}
# Within these families a container often holds several names for the same thing (.m4v and .mp4).
_TOLERANT_FAMILIES = {"video", "audio", "media", "archive", "document", "spreadsheet", "slides", "ebook", "data", "font", "raw"}
_MIME_FORMATS = {
    "text/plain": "txt", "application/zip": "zip", "application/pdf": "pdf", "application/json": "json",
    "text/html": "html", "text/xml": "xml", "application/xml": "xml", "text/csv": "csv", "image/svg+xml": "svg",
    "application/x-7z-compressed": "7z", "application/x-tar": "tar", "application/gzip": "gz",
    "application/x-bzip2": "bz2", "application/x-xz": "xz", "application/zstd": "zst",
    "application/vnd.sqlite3": "sqlite", "application/x-sqlite3": "sqlite",
    "application/x-rar": "rar", "image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp",
    "image/tiff": "tiff", "image/bmp": "bmp", "image/heic": "heic", "image/avif": "avif", "image/jxl": "jxl",
    "audio/mpeg": "mp3", "audio/flac": "flac", "audio/x-wav": "wav", "audio/ogg": "ogg", "video/mp4": "mp4",
    "video/x-matroska": "mkv", "video/webm": "webm", "video/quicktime": "mov", "video/x-msvideo": "avi",
    "application/epub+zip": "epub", "message/rfc822": "eml", "application/rtf": "rtf", "text/rtf": "rtf",
}


def _could_be(hint: str, family: str, mime: str) -> bool:
    """Could a file whose contents read as this type really be the format its name says?"""
    if not mime or mime in ("application/octet-stream", "inode/x-empty"):
        return True  # libmagic has no opinion, so the name is all there is
    if mime == "image/tiff":
        return family in ("raw", "map")  # camera RAW and GeoTIFF are built on TIFF
    if mime == "application/pdf":
        return hint == "ai"  # modern Illustrator files are PDFs
    if mime == "application/postscript":
        return hint in ("ps", "eps", "ai")
    if mime in _CONTAINER_MIMES or mime.startswith("text/"):
        # A .docx really is a zip, and a .csv really is text. A .png or .mp4 never is: that's a file
        # pretending, which is how a playlist or a script reaches a parser it shouldn't.
        return family not in _OWN_SIGNATURE_FAMILIES and hint != "pdf"
    return False


def _kin(family: str) -> str:
    # One container serves both sound and video: .mka and .mkv are the same format to libmagic.
    return "media" if family in ("audio", "video", "media") else family


def resolve_source(hint: str | None, info: dict, catalog: Catalog, ext_to_id: dict[str, str]) -> str:
    """Pick the format to treat the file as. Content wins over the file name, except where the
    content only names a container, such as a .docx being a zip."""
    formats = catalog.formats
    if hint not in formats:
        hint = None
    mime = info.get("mime") if isinstance(info.get("mime"), str) else ""
    mime = mime.lower()

    candidates: list[str] = []
    exts = info.get("exts") if isinstance(info.get("exts"), list) else []
    for ext in exts[:12]:
        if isinstance(ext, str) and ext.lower() in ext_to_id:
            candidates.append(ext_to_id[ext.lower()])
    if mime in _MIME_FORMATS and _MIME_FORMATS[mime] in formats:
        candidates.append(_MIME_FORMATS[mime])
    label = info.get("magika")
    if isinstance(label, str):
        label = label.lower()
        found = label if label in formats else ext_to_id.get(label)
        if found:
            # Magika only runs when libmagic's answer was too generic, so it's the better guess.
            candidates.insert(0, found)

    # A playlist is a list of other files to open. It's never media, whatever it's called.
    if mime in _PLAYLIST_MIMES:
        return "unknown"
    if hint:
        if hint in candidates or _could_be(hint, formats[hint]["family"], mime):
            return hint
        family = _kin(formats[hint]["family"])
        # Not for a PDF: it's in the same family as a .docx, but one is never the other.
        if family in _TOLERANT_FAMILIES and any(_kin(formats[c]["family"]) == family and c != "pdf" for c in candidates):
            return hint
    if candidates:
        return candidates[0]
    return hint or "unknown"


def explain_failure(engine: str, code: int, stderr: bytes) -> str:
    """A cause in plain words where the signal or the tool's output makes it clear. The tool's raw
    output is never passed on, because it can quote the file or reveal paths."""
    text = stderr[-4000:].decode("utf-8", "replace").lower()
    if code == 77:  # our archive tool: expands past the limits
        return "This archive expands to too much data or too many files, so it was refused."
    if code == 79:  # our text tools: there's simply nothing to extract
        return "No readable text was found in this file."
    if code == 78:  # our disk image tool: refers to files outside itself
        return "This disk image refers to other files, so it was refused."
    if code in (-signal.SIGKILL, 137):
        return "This conversion ran out of memory and was stopped. Try a smaller file or a simpler format."
    if code in (-signal.SIGXFSZ, 153):
        return "The result was too large and the conversion was stopped."
    if code in (-signal.SIGXCPU, 152):
        return "This conversion used too much processing time and was stopped."
    if "password" in text or "encrypted" in text or "decrypt" in text:
        return "This file is password-protected. Remove the password and upload it again."
    if "no space left" in text:
        return NO_MEMORY
    return GENERIC_ERROR.format(engine=engine)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # Converters run as this same user. Without this, one that's been taken over could attach to this
    # process and stay hidden in it, surviving the sweep between jobs to watch the ones that follow.
    # It's undone on exec, so it applies to this process only and not to the tools it starts.
    if ctypes.CDLL(None, use_errno=True).prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
        sys.exit("omniconvert-worker: couldn't protect the supervisor from its own converters")
    config = load_config()
    fd = int(os.environ.pop("OMNI_LINK_FD"))
    os.set_inheritable(fd, False)
    worker = Worker(config, Link(socket.socket(fileno=fd)))
    try:
        worker.run()
    except (LinkClosed, OSError):
        # The gateway is gone. PID 1 saw it too and is taking the whole container down.
        sys.exit(0)
