"""Proves the limits by attacking them, against the real running containers.

    PYTHONPATH=. python tests/integration/limits_test.py

Each test runs code the way a compromised converter would: as the worker user, under the same sandbox
launcher and the same limits. Then it checks the attack was stopped, and that the supervisor and the
gateway carried on working.
"""

from __future__ import annotations

import json
import secrets
import subprocess
import sys
import time
from urllib.parse import quote

from tests.client import Client

WORKER = "omniconvert-worker"
GATEWAY = "omniconvert-gateway"
client = Client()
results = []

# The same limits the supervisor gives a converter (worker/main.py, Worker.limits).
LIMITS = {"core": 0, "fsize": 1610612736, "cpu": 960, "nofile": 4096, "nproc": 384}


def record(name, ok, detail):
    results.append((name, ok))
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}: {detail}", flush=True)


def in_sandbox(code: str, timeout: int = 180, limits: dict | None = None):
    """Run Python in the worker exactly as a converter runs."""
    spec = json.dumps({"limits": limits or LIMITS, "landlock": None})
    return subprocess.run(
        ["docker", "exec", "-e", f"OMNI_SANDBOX_SPEC={spec}", WORKER, "/usr/bin/python3", "-I", "-S", "/app/worker/sandbox_exec.py",
         "/usr/bin/python3", "-I", "-c", code],
        capture_output=True, text=True, timeout=timeout, check=False,
    )


def service_still_works(label):
    view, output = client.convert(b"still alive", "zip", "alive.txt", "txt", timeout=120)
    record(f"Service still converts after {label}", view["state"] == "done" and bool(output), view["state"])


def wait_online(seconds: int = 40) -> bool:
    for _ in range(seconds * 2):
        try:
            if client.http.get("/api/formats").json()["server"]["worker_online"]:
                return True
        except Exception:  # the gateway itself may be restarting
            pass
        time.sleep(0.5)
    return False


def test_stopping_and_restarting():
    print("Stopping and starting")
    for name, container in (("Worker", WORKER), ("Gateway", GATEWAY)):
        started = time.monotonic()
        subprocess.run(["docker", "stop", container], capture_output=True, check=True)
        took = time.monotonic() - started
        code = subprocess.run(["docker", "inspect", "--format", "{{.State.ExitCode}}", container], capture_output=True, text=True, check=False).stdout.strip()
        record(f"{name} stops promptly when asked, not by being killed after a timeout", took < 8 and code != "137", f"{took:.1f}s, exit code {code}")
        subprocess.run(["docker", "start", container], capture_output=True, check=True)
        record(f"Worker reconnects after the {name.lower()} restarts", wait_online(), "")
        service_still_works(f"a {name.lower()} restart")


def supervisor_code(action: str) -> str:
    return f"""
import os, signal, socket, sys, time
sup = next(int(p) for p in os.listdir('/proc') if p.isdigit() and p != '1' and b'-m\\0worker' in open(f'/proc/{{p}}/cmdline','rb').read())
{action}
"""


def restarts() -> int:
    return int(docker_state(WORKER)[2])


def test_taking_down_the_supervisor():
    print("Stopping the supervisor, as a compromised converter could without Landlock")
    for name, sig in (("killed", "SIGKILL"), ("frozen", "SIGSTOP")):
        before = restarts()
        subprocess.run(["docker", "exec", WORKER, "/usr/bin/python3", "-I", "-c", supervisor_code(f"os.kill(sup, signal.{sig})")], capture_output=True, check=False)
        for _ in range(40):
            if restarts() > before:
                break
            time.sleep(0.5)
        record(f"A {name} supervisor takes the whole container down", restarts() > before, f"restarts {before} -> {restarts()}")
        record(f"The worker comes back after being {name}", wait_online(), "")
        service_still_works(f"a {name} supervisor")


def test_an_impostor_gets_nothing():
    print("A process trying to take the supervisor's place")
    # Something to steal: if it were believed, this file would be handed to it next.
    blocker = client.upload(rb"\documentclass{article}\begin{document}\def\a{\a}\a\end{document}", "pdf", "endless.tex", "tex").json()["id"]
    secret = client.upload(b"the next person's file", "zip", "secret.txt", "txt").json()["id"]
    for _ in range(40):
        if client.http.get(f"/api/jobs/{blocker}").json()["state"] == "running":
            break
        time.sleep(0.5)
    impostor = supervisor_code("""
path = '/run/omniconvert/link.sock'
def welcomed():
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(1)
    try:
        s.connect(path)
        return s.recv(5) != b''
    except OSError:
        return False
    finally:
        s.close()
print('before killing:', welcomed(), flush=True)
os.kill(sup, signal.SIGKILL)
end = time.time() + 20
while time.time() < end:
    if welcomed():
        print('WELCOMED', flush=True)
        break
    time.sleep(0.002)
else:
    print('survived, and was refused every time', flush=True)
""")
    before = restarts()
    proc = subprocess.run(["docker", "exec", WORKER, "/usr/bin/python3", "-I", "-c", impostor], capture_output=True, text=True, timeout=60, check=False)
    record("Refused while the real supervisor runs", "before killing: False" in proc.stdout, proc.stdout.strip()[:60])
    record("Never welcomed after killing the supervisor", "WELCOMED" not in proc.stdout, repr(proc.stdout.strip()[-60:]) + f" exit {proc.returncode}")
    record("And it died with the container", "survived" not in proc.stdout and restarts() > before or wait_restart(before), f"restarts {before} -> {restarts()}")
    record("The worker comes back", wait_online(), "")
    view = client.wait(blocker, 30)
    record("The job that was running is reported as failed", view["state"] == "failed", str(view.get("error"))[:70])
    view = client.wait(secret, 60)
    res = client.http.get(f"/api/jobs/{secret}/download")
    record("The waiting file went to the real worker", view["state"] == "done" and res.status_code == 200, view["state"])
    client.http.delete(f"/api/jobs/{blocker}")


def wait_restart(before: int) -> bool:
    for _ in range(40):
        if restarts() > before:
            return True
        time.sleep(0.5)
    return False


def test_a_gateway_restart_forgets_everything():
    print("Restarting the gateway")
    view, _ = client.convert(b"held only in memory", "zip", "held.txt", "txt", keep=True)
    record("A finished result exists first", client.http.get(f"/api/jobs/{view['id']}").status_code == 200, "")
    subprocess.run(["docker", "restart", GATEWAY], capture_output=True, check=True)
    record("The worker comes back", wait_online(), "")
    res = client.http.get(f"/api/jobs/{view['id']}")
    record("The result is gone", res.status_code == 404, f"HTTP {res.status_code}")
    service_still_works("a gateway restart")


def in_gateway(code: str) -> str:
    return subprocess.run(["docker", "exec", GATEWAY, "python", "-I", "-c", code], capture_output=True, text=True, check=False).stdout.strip()


def in_worker(code: str) -> str:
    return subprocess.run(["docker", "exec", WORKER, "/usr/bin/python3", "-I", "-c", code], capture_output=True, text=True, check=False).stdout.strip()


def test_nothing_is_kept():
    print("Nothing is kept")
    token = secrets.token_hex(12)
    name_mark, body_mark = f"NAMEMARK{token}", f"BODYMARK{token}"
    used = "import os; s=os.statvfs('/jobs'); print(s.f_blocks - s.f_bfree)"
    baseline = in_gateway(used)
    body = f"{body_mark}\n".encode() * 200
    view, output = client.convert(body, "zip", f"{name_mark}.txt", "txt")
    record("A real conversion ran first", view["state"] == "done" and bool(output), view["state"])
    # And one a converter chokes on, which is when tools print what they read.
    bad, _ = client.convert(f"<svg>{body_mark}".encode(), "png", f"{name_mark}.svg", "svg")
    print(f"  note  the malformed file ended as: {bad['state']}")
    # Also one that's cancelled partway, and one nobody collects.
    uncollected, _ = client.convert(body, "zip", f"{name_mark}.txt", "txt", keep=True)
    client.http.delete(f"/api/jobs/{uncollected['id']}")
    time.sleep(1)

    jobs = in_gateway("import os; print(sorted(os.listdir('/jobs')))")
    record("The job store holds no files", jobs == "['.alive']", jobs)
    after = in_gateway(used)
    record("Its memory is all back", after == baseline, f"{after} pages in use, {baseline} before")
    work = in_worker("import os; print(sorted(os.listdir('/work')))")
    record("The work folder is empty", work == "[]", work)
    tmp = in_worker("import os; print(sorted(os.listdir('/tmp')))")
    record("/tmp is empty", tmp == "['.alive']", tmp)

    for name, container in (("Gateway", GATEWAY), ("Worker", WORKER)):
        changed = subprocess.run(["docker", "diff", container], capture_output=True, text=True, check=False).stdout.strip()
        record(f"{name} wrote nothing to its image", changed == "", changed[:80])
        logs = subprocess.run(["docker", "logs", container], capture_output=True, text=True, errors="replace", check=False)
        said = logs.stdout + logs.stderr
        record(f"{name}'s log doesn't hold the file's name or contents", name_mark not in said and body_mark not in said, f"{len(said)} bytes of log")
        python = "python" if container == GATEWAY else "/usr/bin/python3"
        swap = subprocess.run(["docker", "exec", container, python, "-I", "-c", "print(open('/sys/fs/cgroup/memory.swap.current').read().strip())"], capture_output=True, text=True, check=False).stdout.strip()
        record(f"{name} has nothing in swap", swap == "0", swap)

    # The real test: look for the markers where Docker keeps things on this machine's disk.
    if subprocess.run(["sudo", "-n", "true"], capture_output=True, check=False).returncode != 0:
        print("  note  no passwordless sudo, so Docker's own folders weren't searched")
        return
    folders = [f for f in ("/var/lib/docker", "/var/lib/containerd", "/var/log", "/var/tmp")
               if subprocess.run(["sudo", "-n", "test", "-e", f], check=False).returncode == 0]
    # The pattern goes in on stdin, because sudo writes its command line to the system journal, and the
    # search would otherwise find its own footprints there.
    command = ["sudo", "-n", "timeout", "400", "grep", "-rlaE", "-D", "skip", "-f", "-"]
    # A search that can't find anything proves nothing, so first it has to find something known to be there.
    control = subprocess.run([*command, "/var/lib/docker/containers"], input="ready: [0-9]+ engines\n", capture_output=True, text=True, check=False).stdout.strip()
    record("Control: the search finds the worker's own log line on disk", "-json.log" in control, control[:60].split("/")[-1])
    found = subprocess.run([*command, *folders], input=f"{name_mark}\n{body_mark}\n", capture_output=True, text=True, check=False)
    record(f"Neither marker is anywhere on disk: {', '.join(folders)}", found.stdout.strip() == "" and found.returncode == 1, found.stdout.strip()[:120] or f"grep exit {found.returncode}")


def test_orphans_are_reaped():
    print("Orphaned processes")
    code = """
import os, time
if os.fork() == 0:
    if os.fork() == 0:
        os._exit(0)
    os._exit(0)
os.wait()
time.sleep(1)
zombies = 0
for p in os.listdir('/proc'):
    if p.isdigit():
        try:
            if open(f'/proc/{p}/stat').read().rsplit(')', 1)[1].split()[0] == 'Z':
                zombies += 1
        except OSError:
            pass
print(zombies)
"""
    out = subprocess.run(["docker", "exec", WORKER, "/usr/bin/python3", "-I", "-c", code], capture_output=True, text=True, check=False).stdout.strip()
    record("PID 1 reaps orphans, leaving no zombies", out == "0", f"{out} zombies")


def docker_state(container):
    out = subprocess.run(["docker", "inspect", "--format", "{{.State.Running}} {{.State.OOMKilled}} {{.RestartCount}}", container],
                         capture_output=True, text=True, check=False).stdout.split()
    return out


def worker_pids() -> int:
    out = subprocess.run(["docker", "exec", WORKER, "/usr/bin/python3", "-I", "-c", "print(open('/sys/fs/cgroup/pids.current').read().strip())"],
                         capture_output=True, text=True, check=False).stdout.strip()
    return int(out) if out.isdigit() else -1


def sweep():
    """Clear anything an attack left running. Run from outside the supervisor, so unlike its own
    sweep, this has to spare the supervisor by name."""
    code = """
import os, signal
me = os.getpid()
for p in os.listdir('/proc'):
    if not p.isdigit() or int(p) in (1, me):
        continue
    try:
        if b'-m\\0worker' not in open(f'/proc/{p}/cmdline', 'rb').read():
            os.kill(int(p), signal.SIGKILL)
    except OSError:
        pass
"""
    subprocess.run(["docker", "exec", WORKER, "/usr/bin/python3", "-I", "-c", code], capture_output=True, check=False)
    time.sleep(1)


FORK_BOMB = """
import os, time
count = 0
try:
    while True:
        if os.fork() == 0:
            time.sleep(25)
            os._exit(0)
        count += 1
except OSError as err:
    print('stopped at', count, err)
"""

MEMORY_HOG = """
blocks = []
try:
    while True:
        blocks.append(bytearray(b'x' * (64 * 1024 * 1024)))
except MemoryError:
    print('memory error at', len(blocks) * 64, 'MiB')
"""

CPU_HOG = """
import multiprocessing, os, time
def spin():
    end = time.time() + 12
    while time.time() < end:
        pass
procs = [multiprocessing.Process(target=spin) for _ in range(os.cpu_count())]
for p in procs: p.start()
for p in procs: p.join()
"""

BIG_FILE = """
try:
    with open('/work/big', 'wb') as fh:
        while True:
            fh.write(b'x' * (1024 * 1024))
except OSError as err:
    print('write failed:', err)
"""


# What a converter under full attacker control tries first. It runs under the same rules as a real job.
SNOOP = """
import ctypes, json, mmap, os, platform, socket, stat
report = {}
libc = ctypes.CDLL(None, use_errno=True)
def attempt(name, fn):
    try:
        report[name] = ('ALLOWED', str(fn())[:80])
    except Exception as err:
        report[name] = ('denied', type(err).__name__)
def call(fn, *args):
    result = fn(*args)
    if result == -1:
        raise PermissionError(ctypes.get_errno())
    return result
def find_marker(*, control=False):
    # Its own folder by name as well: under Landlock it can't list /work, so a walk from there stops short.
    for top in ('/work', '/work/snoop', '/tmp', '/run', '/dev', '/var/tmp'):
        for root, dirs, files in os.walk(top):
            for name in files:
                path = os.path.join(root, name)
                try:
                    # Regular files only: reading a device like /dev/ptmx would just wait forever.
                    if (name == 'control') == control and stat.S_ISREG(os.lstat(path).st_mode) and b'MARKER' in open(path, 'rb').read(4096):
                        return path
                except OSError:
                    pass
    raise LookupError('nowhere')
def speak_as_worker():
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(3)
    s.connect('/run/omniconvert/link.sock')
    if s.recv(5) == b'':
        raise ConnectionRefusedError('hung up')
    return 'welcomed'
def compat_syscall():
    # A 32-bit getpid through the old interface, to go around every rule written for 64-bit calls.
    if platform.machine() != 'x86_64':
        raise LookupError('not x86_64')
    code = bytes([0xb8, 0x14, 0, 0, 0, 0xcd, 0x80, 0xc3])
    buf = mmap.mmap(-1, 4096, prot=mmap.PROT_READ | mmap.PROT_WRITE | mmap.PROT_EXEC)
    buf.write(code)
    fn = ctypes.CFUNCTYPE(ctypes.c_int)(ctypes.addressof(ctypes.c_char.from_buffer(buf)))
    pid = os.fork()
    if pid == 0:
        os._exit(0 if fn() > 0 else 1)
    if os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1]) != 0:
        raise PermissionError('refused')
    return 'ran'
attempt('find its own control file the same way', lambda: find_marker(control=True))
attempt('find the other job file anywhere it can read', find_marker)
attempt('see the job store', lambda: os.listdir('/jobs'))
attempt('write beside the socket', lambda: open('/run/omniconvert/pwned', 'w'))
attempt('replace the socket', lambda: os.unlink('/run/omniconvert/link.sock'))
attempt('speak to the gateway as the worker', speak_as_worker)
attempt('write to the application', lambda: open('/app/worker/main.py', 'a'))
attempt('write to /etc', lambda: open('/etc/pwned', 'w'))
attempt('write to /tmp', lambda: open('/tmp/pwned-snoop', 'w'))
attempt('open a network socket', lambda: socket.socket(socket.AF_INET))
attempt('open an IPv6 socket', lambda: socket.socket(socket.AF_INET6))
attempt('open a netlink socket', lambda: socket.socket(16, 3))
attempt('open a raw packet socket', lambda: socket.socket(17, 3))
attempt('reach the internet', lambda: socket.create_connection(('1.1.1.1', 443), 2))
attempt('reach the host', lambda: socket.create_connection(('172.31.90.1', 22), 2))
attempt('create a file with no name', lambda: call(libc.memfd_create, b'x', 0))
attempt('create shared memory that outlives it', lambda: call(libc.shmget, 0, 4096, 0o600))
attempt('create a message queue', lambda: open('/dev/mqueue/x', 'w'))
attempt('use a 32-bit syscall', compat_syscall)
attempt('find secrets in its environment', lambda: [k for k in os.environ if 'TOKEN' in k or 'SECRET' in k or 'ALLOWED_IPS' in k] or (_ for _ in ()).throw(LookupError()))
def supervisor():
    return next(int(p) for p in os.listdir('/proc') if p.isdigit() and p != '1' and b'-m\\0worker' in open(f'/proc/{p}/cmdline','rb').read())
attempt('signal the supervisor', lambda: os.kill(supervisor(), 0))
def attach(pid):
    if libc.ptrace(16, pid, 0, 0) != 0:  # PTRACE_ATTACH
        raise PermissionError(ctypes.get_errno())
    libc.ptrace(17, pid, 0, 0)  # PTRACE_DETACH
    return 'attached'
def poke_memory(pid):
    # Opening it is the permission check; a first read at address 0 fails either way.
    fd = os.open(f'/proc/{pid}/mem', os.O_RDWR)
    os.close(fd)
    return 'opened'
attempt('read the supervisor environment', lambda: open(f'/proc/{supervisor()}/environ').read(10))
attempt('open the supervisor memory', lambda: poke_memory(supervisor()))
attempt('attach a debugger to the supervisor', lambda: attach(supervisor()))
attempt('read the supervisor open files', lambda: os.listdir(f'/proc/{supervisor()}/fd'))
# PID 1 is the one process the sweep between jobs can't kill, so it would be the place to hide.
attempt('read the environment of PID 1', lambda: open('/proc/1/environ').read(10))
attempt('open the memory of PID 1', lambda: poke_memory(1))
attempt('attach a debugger to PID 1', lambda: attach(1))
attempt('read the open files of PID 1', lambda: os.listdir('/proc/1/fd'))
print(json.dumps(report))
"""


def test_compromised_converter():
    print("A fully compromised converter")
    # Something real to go after: a finished result from another job. A tar isn't compressed, so the marker
    # would be there to find if the file were in reach.
    view, _ = client.convert(b"MARKER somebody else's result", "tar", "other.txt", "txt", keep=True)
    record("The other job finished", view["state"] == "done", view["state"])
    isolation = client.http.get("/api/formats").json()["server"]["isolation"]
    landlock = isolation.startswith("landlock")
    rules = None
    job = "/work/snoop"
    in_worker(f"import os; os.makedirs('{job}', exist_ok=True); open('{job}/control','w').write('MARKER')")
    if landlock:
        rules = {"rx": ["/usr", "/bin", "/sbin", "/lib", "/lib64", "/opt"],
                 "ro": ["/etc", "/app", "/proc", "/sys/fs/cgroup", "/sys/devices/system/cpu", "/var/lib", "/var/cache", "/dev/urandom", "/dev/random", "/dev/zero"],
                 "rw": [job, "/dev/null"], "tmpfile": ["/tmp"]}
    spec = json.dumps({"limits": LIMITS, "landlock": rules})
    proc = subprocess.run(["docker", "exec", "-e", f"OMNI_SANDBOX_SPEC={spec}", WORKER, "/usr/bin/python3", "-I", "-S", "/app/worker/sandbox_exec.py",
                           "/usr/bin/python3", "-I", "-c", SNOOP], capture_output=True, text=True, timeout=60, check=False)
    try:
        report = json.loads(proc.stdout)
    except ValueError:
        record("Probe ran", False, proc.stderr[-200:])
        return
    print(f"  isolation here: {isolation}")
    record("Control: the search finds a marker that is in reach", report["find its own control file the same way"][0] == "ALLOWED", report["find its own control file the same way"][1])
    always_denied = ["find the other job file anywhere it can read", "see the job store", "write beside the socket", "replace the socket",
                     "speak to the gateway as the worker", "write to the application", "write to /etc",
                     "open a network socket", "open an IPv6 socket", "open a netlink socket", "open a raw packet socket",
                     "reach the internet", "reach the host", "create a file with no name", "create shared memory that outlives it",
                     "create a message queue", "use a 32-bit syscall", "find secrets in its environment",
                     "read the supervisor environment", "open the supervisor memory", "attach a debugger to the supervisor", "read the supervisor open files",
                     "read the environment of PID 1", "open the memory of PID 1", "attach a debugger to PID 1", "read the open files of PID 1"]
    only_with_landlock = ["write to /tmp", "signal the supervisor"]
    for name in always_denied:
        record(f"Can't {name}", report[name][0] == "denied", report[name][1])
    for name in only_with_landlock:
        if landlock:
            record(f"Can't {name}", report[name][0] == "denied", report[name][1])
        else:
            print(f"  note  Without Landlock: {name}: {report[name][0]}")
    # A denied 32-bit call proves nothing if this kernel has no 32-bit layer at all.
    control = SNOOP.split("attempt('find the other job")[0] + "print(compat_syscall())"
    done = subprocess.run(["docker", "run", "--rm", "--network", "none", "--entrypoint", "/usr/bin/python3", "omniconvert-worker:1.2.0", "-I", "-c", control],
                          capture_output=True, text=True, timeout=60, check=False)
    if done.stdout.strip() == "ran":
        record("Control: the same 32-bit call works under Docker's default filter", True, "ran")
    else:
        print(f"  note  this kernel refuses 32-bit calls with any filter, so that check proves nothing here: {done.stderr.strip()[-80:]}")
    res = client.http.get(f"/api/jobs/{view['id']}/download")
    record("The other job's result is untouched", res.status_code == 200 and len(res.content) > 0, f"{len(res.content)} bytes")
    in_worker(f"import shutil,os; shutil.rmtree('{job}',True); os.path.exists('/tmp/pwned-snoop') and os.unlink('/tmp/pwned-snoop')")


def test_nothing_survives_a_job():
    print("A process that tries to outlive its job")
    # Double-forked into its own session, so killing the job's process group doesn't reach it.
    hide = """
import os, sys, time
if os.fork():
    os._exit(0)
os.setsid()
if os.fork():
    os._exit(0)
for fd in (0, 1, 2):
    os.close(fd)
os.environ.pop('X', None)
time.sleep(300)
"""
    in_sandbox(hide, timeout=30)
    count = """
import os
n = 0
for p in os.listdir('/proc'):
    if p.isdigit():
        try:
            if int(p) != os.getpid() and b'time.sleep(300)' in open(f'/proc/{p}/cmdline', 'rb').read():
                n += 1
        except OSError:
            pass
print(n)
"""
    def stragglers():
        return subprocess.run(["docker", "exec", WORKER, "/usr/bin/python3", "-I", "-c", count], capture_output=True, text=True, check=False).stdout.strip()
    alive = stragglers()
    record("It really is hiding before the next job", alive == "1", f"{alive} process")
    # And files, in every place it can write.
    in_sandbox("for path in ('/tmp/left-behind', '/work/left-behind'):\n    open(path, 'w').write('for the next job')", timeout=30)
    def left_files():
        return in_worker("import os; print(sum(os.path.exists(p) for p in ('/tmp/left-behind', '/work/left-behind')))")
    record("It really has left files behind", left_files() == "2", left_files())
    view, _ = client.convert(b"next job", "zip", "next.txt", "txt", timeout=60)
    time.sleep(1)
    record("The next job swept it away", stragglers() == "0", f"{stragglers()} left; job {view['state']}")
    record("And the files are gone", left_files() == "0", f"{left_files()} left")


def test_fork_bomb():
    print("Fork bomb")
    before = docker_state(WORKER)
    proc = in_sandbox(FORK_BOMB)
    peak = worker_pids()
    record("Stopped by the per-job process limit", "stopped at" in proc.stdout, proc.stdout.strip()[:80] or proc.stderr.strip()[:80])
    limit = int(subprocess.run(["docker", "exec", WORKER, "/usr/bin/python3", "-I", "-c", "print(open('/sys/fs/cgroup/pids.max').read())"], capture_output=True, text=True, check=False).stdout)
    record("Container stayed under its pids limit", 0 < peak <= limit, f"{peak} of {limit}")
    sweep()
    record("Worker didn't restart", docker_state(WORKER)[2] == before[2], docker_state(WORKER)[2])
    service_still_works("a fork bomb")


def test_memory_hog():
    print("Memory hog")
    before = docker_state(WORKER)
    proc = in_sandbox(MEMORY_HOG, timeout=240)
    killed = proc.returncode in (137, -9)
    record("Killed by the container's memory limit", killed or "memory error" in proc.stdout, f"exit {proc.returncode} {proc.stdout.strip()[:60]}")
    time.sleep(2)
    state = docker_state(WORKER)
    record("Supervisor survived (the converter died first)", state[0] == "true" and state[2] == before[2], f"running={state[0]} restarts={state[2]}")
    sweep()
    service_still_works("a memory hog")


def test_memory_hog_real_job():
    print("Memory, through a real job")
    # A huge blank image makes ImageMagick try to allocate far past its own limit.
    svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="30000" height="30000"><rect width="30000" height="30000" fill="red"/></svg>'
    view, _ = client.convert(svg, "bmp", "huge.svg", "svg", timeout=280)
    record("A job asking for a huge image is stopped, not fatal", view["state"] in ("failed", "done"), f"{view['state']}: {str(view.get('error'))[:80]}")
    service_still_works("a huge image")


def test_cpu_limit():
    print("CPU")
    subprocess.Popen(["docker", "exec", "-e", f"OMNI_SANDBOX_SPEC={json.dumps({'limits': LIMITS})}", WORKER, "/usr/bin/python3", "-I", "-S",
                      "/app/worker/sandbox_exec.py", "/usr/bin/python3", "-I", "-c", CPU_HOG],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(6)
    out = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{.CPUPerc}}", WORKER], capture_output=True, text=True, check=False).stdout.strip()
    used = float(out.rstrip("%") or 0)
    limit = subprocess.run(["docker", "exec", WORKER, "/usr/bin/python3", "-I", "-c", "a,b=open('/sys/fs/cgroup/cpu.max').read().split(); print(int(a)/int(b))"],
                           capture_output=True, text=True, check=False).stdout.strip()
    allowed = float(limit) * 100
    record("Held at the CPU limit", 0 < used <= allowed * 1.15, f"{used:.0f}% with a limit of {allowed:.0f}%")
    time.sleep(8)
    sweep()


def test_timeout():
    print("Job timeout")
    # A real conversion that never ends: a TeX macro that calls itself forever, using no memory.
    tex = rb"\documentclass{article}\begin{document}\def\a{\a}\a\end{document}"
    timeout = int(subprocess.run(["docker", "exec", WORKER, "/usr/bin/python3", "-I", "-c", "import os; print(os.environ['JOB_TIMEOUT_SECONDS'])"],
                                 capture_output=True, text=True, check=False).stdout)
    started = time.monotonic()
    view, _ = client.convert(tex, "pdf", "endless.tex", "tex", timeout=timeout + 60)
    took = time.monotonic() - started
    record("Stopped at the time limit", view["state"] == "failed" and "limit" in str(view.get("error")), f"after {took:.0f}s of {timeout}s: {str(view.get('error'))[:70]}")
    service_still_works("a job that never ends")


def test_work_folder_full():
    print("Filling the work folder")
    before = docker_state(WORKER)
    # Unlimited file size here, to reach the mount's own hard limit.
    limits = {**LIMITS, "fsize": 2**62}
    proc = in_sandbox(BIG_FILE, timeout=280, limits=limits)
    record("Stopped by the mount's fixed size", "No space left" in proc.stdout, f"exit {proc.returncode}: {proc.stdout.strip()[:70]} {proc.stderr.strip()[-120:]}")
    res = client.upload(b"x" * 1024, "zip", "a.txt", "txt")
    record("A new upload is still accepted: the gateway's memory is separate", res.status_code == 202, f"HTTP {res.status_code}")
    alive = client.http.get("/api/formats").status_code
    record("Gateway still answers", alive == 200, f"HTTP {alive}")
    state = docker_state(WORKER)
    # Compared with before, because Docker's out-of-memory flag stays set once anything has been killed.
    record("Supervisor survived it, and memory held", state == before and state[0] == "true", f"running={state[0]} oom={state[1]} restarts={state[2]}")
    online = client.http.get("/api/formats").json()["server"]["worker_online"]
    record("Worker still reports itself online while full", online is True, str(online))
    view = client.wait(res.json()["id"], 60)
    record("A job arriving then is cleaned up after, whatever it manages", view["state"] in ("done", "failed"), f"{view['state']}: {str(view.get('error'))[:60]}")
    client.http.delete(f"/api/jobs/{res.json()['id']}")
    left = in_worker("import os; print(os.path.exists('/work/big'))")
    record("Everything was wiped between jobs, freeing the memory", left == "False", left)
    service_still_works("a full work folder")


def test_job_store_full():
    print("Filling the gateway's memory")
    before = docker_state(GATEWAY)
    used = "import os; s=os.statvfs('/jobs'); print(s.f_blocks - s.f_bfree)"
    baseline = in_gateway(used)
    # Hold the worker, so uploads stay in the gateway.
    blocker = client.upload(rb"\documentclass{article}\begin{document}\def\a{\a}\a\end{document}", "pdf", "endless.tex", "tex").json()["id"]
    size = 512 * 1024 * 1024

    def stream():
        chunk = b"\0" * (1024 * 1024)
        for _ in range(size // len(chunk)):
            yield chunk

    held, last = [], None
    for _ in range(6):
        last = client.http.put("/api/jobs", params={"to": "zst"}, content=stream(), headers={"Content-Length": str(size), "X-Filename": quote("big.bin")}, timeout=300)
        if last.status_code != 202:
            break
        held.append(last.json()["id"])
    record("Refused cleanly once full", last.status_code == 507, f"HTTP {last.status_code} after {len(held)} x 512 MiB")
    print(f"  note  {str(last.json().get('error'))[:90]}")

    # Under the upload limit, but more than can be left of the store's 2 GB after what's already held.
    def no_length():
        for _ in range(900):
            yield b"\0" * (1024 * 1024)

    res = client.http.put("/api/jobs", params={"to": "zst"}, content=no_length(), headers={"X-Filename": "big.bin"}, timeout=300)
    record("An upload that hides its size is stopped too", res.status_code == 507, f"HTTP {res.status_code}")
    if res.status_code == 202:
        held.append(res.json()["id"])
    state = docker_state(GATEWAY)
    record("Gateway wasn't killed for it", state == before and state[0] == "true", f"running={state[0]} oom={state[1]} restarts={state[2]}")
    for job_id in [*held, blocker]:
        client.http.delete(f"/api/jobs/{job_id}")
    time.sleep(1)
    after = in_gateway(used)
    record("Deleting them gave all the memory back", after == baseline, f"{after} pages in use, {baseline} before")
    service_still_works("a full job store")


def test_largest_allowed_file():
    print("The largest file the default limits allow")
    formats = client.http.get("/api/formats").json()
    limit = formats["limits"]["max_upload_bytes"]
    used = "import os; s=os.statvfs('/jobs'); print(s.f_blocks - s.f_bfree)"
    chunk = secrets.token_bytes(1024 * 1024)
    routes = {r["to"] for r in client.http.get("/api/routes", params={"from": "unknown"}).json()["routes"]}

    def stream():
        for _ in range(limit // len(chunk)):
            yield chunk

    # A result about the same size, and one a third larger, which is the most room a job here can need.
    targets = [t for t in ("tar", "base64", "b64") if t in routes]
    record("There are large-result routes to try", len(targets) >= 2, str(targets))
    for target in targets:
        baseline = in_gateway(used)
        states = docker_state(GATEWAY), docker_state(WORKER)
        started = time.monotonic()
        res = client.http.put("/api/jobs", params={"to": target}, content=stream(), headers={"Content-Length": str(limit), "X-Filename": "largest.bin"}, timeout=300)
        uploaded = time.monotonic() - started
        record(f"{limit // 1048576} MiB accepted, to {target}", res.status_code == 202, f"HTTP {res.status_code} in {uploaded:.1f}s")
        if res.status_code != 202:
            continue
        job_id = res.json()["id"]
        view = client.wait(job_id, 280)
        record(f"It converts to {target}", view["state"] == "done", f"{view['state']}: {str(view.get('error') or view.get('output'))[:80]} after {time.monotonic() - started:.0f}s")
        got = 0
        if view["state"] == "done":
            with client.http.stream("GET", f"/api/jobs/{job_id}/download", timeout=300) as download:
                for part in download.iter_bytes():
                    got += len(part)
        record("All of it downloads", got == (view.get("output") or {}).get("size", -1) and got >= limit, f"{got // 1048576} MiB")
        record("Then it's gone", client.http.get(f"/api/jobs/{job_id}").status_code == 404, "")
        record("Neither container was killed", (docker_state(GATEWAY), docker_state(WORKER)) == states, f"{docker_state(GATEWAY)} {docker_state(WORKER)}")
        after = in_gateway(used)
        record("All the gateway's memory came back", after == baseline, f"{after} pages, {baseline} before")


def test_output_size_limit():
    print("Single file size")
    proc = in_sandbox("open('/work/toobig','wb').write(b'x'*5000000)", limits={**LIMITS, "fsize": 1000000})
    record("A file past the limit is stopped", proc.returncode != 0, f"exit {proc.returncode}")
    in_worker("import os; os.path.exists('/work/toobig') and os.unlink('/work/toobig')")


def main():
    selected = sys.argv[1:]
    tests = [test_nothing_is_kept, test_compromised_converter, test_nothing_survives_a_job, test_stopping_and_restarting, test_taking_down_the_supervisor,
             test_an_impostor_gets_nothing, test_a_gateway_restart_forgets_everything, test_orphans_are_reaped, test_fork_bomb, test_memory_hog,
             test_memory_hog_real_job, test_cpu_limit, test_output_size_limit, test_timeout, test_work_folder_full, test_job_store_full, test_largest_allowed_file]
    for test in tests:
        if not selected or test.__name__.removeprefix("test_") in selected:
            test()
    failed = [name for name, ok in results if not ok]
    print(f"\n{len(results) - len(failed)} of {len(results)} checks passed.")
    for name in failed:
        print(f"  FAILED: {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
