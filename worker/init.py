"""PID 1 in the worker: connects to the gateway, starts the supervisor, and ends everything if either goes.

It replaces Docker's `init: true` for two reasons.

Converters run as the same user as PID 1, and PID 1 is the one process the sweep between jobs can't kill.
On a kernel without Landlock, a compromised converter could attach to an ordinary init and inject code
into it, staying hidden across jobs to watch the ones that follow. Marked non-dumpable, nothing with this
user id can attach to it or read its memory.

It also makes sure nothing but a fresh supervisor ever speaks for this container. The connection is made
here, before any converter can exist, and PID 1 keeps a copy of it. When the supervisor dies or is frozen,
or the gateway hangs up, PID 1 exits, and the kernel kills every process in the container with it. So a
converter can't stop the supervisor and then connect in its place to be handed the next person's file.
"""

import ctypes
import os
import select
import signal
import socket
import sys
import time

PR_SET_DUMPABLE = 4
SOCKET_PATH = "/run/omniconvert/link.sock"  # shared/link.py. This runs with -I -S, so it can't import that.
WELCOME = b"\x0c\x00\x00\x00\x00"
TICK_MS = 200


def connect() -> socket.socket:
    """Waits for the gateway. A refusal is just the socket closing, for instance just after a restart."""
    while True:
        conn = socket.socket(socket.AF_UNIX)
        try:
            conn.settimeout(5)
            conn.connect(SOCKET_PATH)
            if conn.recv(len(WELCOME), socket.MSG_WAITALL) == WELCOME:
                conn.settimeout(None)
                return conn
        except OSError:
            pass
        conn.close()
        time.sleep(1)


def main():
    if ctypes.CDLL(None, use_errno=True).prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
        sys.exit("omniconvert-worker: couldn't protect PID 1 from its own converters")

    child = None

    def forward(signum, _frame):
        if child is None:
            sys.exit(0)  # asked to stop while still waiting for the gateway
        try:
            os.kill(child, signum)
        except ProcessLookupError:
            pass

    # PID 1 ignores any signal it has no handler for, so `docker stop` would otherwise wait and kill it.
    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)

    conn = connect()

    child = os.fork()
    if child == 0:
        # exec resets dumpable, so the supervisor sets it again for itself.
        os.set_inheritable(conn.fileno(), True)
        os.execve("/opt/py/bin/python", ["/opt/py/bin/python", "-m", "worker"],
                  {**os.environ, "OMNI_LINK_FD": str(conn.fileno())})

    hangup = select.poll()
    hangup.register(conn, 0)  # no events asked for: a hangup is always reported
    while True:
        while True:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG | os.WUNTRACED)
            except ChildProcessError:
                sys.exit(1)
            if pid == 0:
                break
            if pid == child:
                if os.WIFSTOPPED(status):
                    sys.exit("omniconvert-worker: the supervisor was frozen; restarting the container")
                code = os.waitstatus_to_exitcode(status)
                sys.exit(128 - code if code < 0 else code)  # killed by a signal: the usual 128 + signal
        if hangup.poll(TICK_MS):
            sys.exit("omniconvert-worker: the gateway hung up; restarting the container")


main()
