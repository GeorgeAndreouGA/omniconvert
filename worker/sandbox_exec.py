"""Locks down this process, then becomes the converter.

Run as:  OMNI_SANDBOX_SPEC='<json>' python3 -I -S sandbox_exec.py <program> [args...]

The spec comes through the environment and is removed before the converter starts. Any process can
read another's command line in /proc, and the spec names job folders; a process's environment is
protected by the same check as debugging it.

Everything applied here is inherited by the converter and its children and can't be undone. It needs
no privileges and no capabilities: Landlock only requires no_new_privs, which the container already
sets. It runs before any untrusted file is opened, and it imports only the standard library.

Landlock confines a job to its own folder, so one compromised conversion can't read another's files.
Kernels without it (Raspberry Pi OS) get everything else here, and the supervisor runs one job at a
time to compensate.
"""

from __future__ import annotations

import ctypes
import json
import os
import resource
import struct
import sys

# The same numbers on x86_64 and arm64.
SYS_LANDLOCK_CREATE_RULESET = 444
SYS_LANDLOCK_ADD_RULE = 445
SYS_LANDLOCK_RESTRICT_SELF = 446
LANDLOCK_CREATE_RULESET_VERSION = 1 << 0
LANDLOCK_RULE_PATH_BENEATH = 1
PR_SET_NO_NEW_PRIVS = 38

FS_EXECUTE = 1 << 0
FS_WRITE_FILE = 1 << 1
FS_READ_FILE = 1 << 2
FS_READ_DIR = 1 << 3
FS_REMOVE_DIR = 1 << 4
FS_REMOVE_FILE = 1 << 5
FS_MAKE_CHAR = 1 << 6
FS_MAKE_DIR = 1 << 7
FS_MAKE_REG = 1 << 8
FS_MAKE_SOCK = 1 << 9
FS_MAKE_FIFO = 1 << 10
FS_MAKE_BLOCK = 1 << 11
FS_MAKE_SYM = 1 << 12
FS_REFER = 1 << 13        # ABI 2
FS_TRUNCATE = 1 << 14     # ABI 3
FS_IOCTL_DEV = 1 << 15    # ABI 5

NET_BIND_TCP = 1 << 0     # ABI 4
NET_CONNECT_TCP = 1 << 1  # ABI 4
SCOPE_ABSTRACT_UNIX = 1 << 0  # ABI 6
SCOPE_SIGNAL = 1 << 1         # ABI 6

_FILE_RIGHTS = FS_EXECUTE | FS_WRITE_FILE | FS_READ_FILE | FS_TRUNCATE | FS_IOCTL_DEV


def _libc():
    return ctypes.CDLL(None, use_errno=True)


def landlock_abi() -> int:
    """The kernel's Landlock version, or 0 where it's missing or switched off."""
    try:
        libc = _libc()
        abi = libc.syscall(ctypes.c_long(SYS_LANDLOCK_CREATE_RULESET), None, ctypes.c_size_t(0),
                           ctypes.c_uint32(LANDLOCK_CREATE_RULESET_VERSION))
    except OSError:
        return 0
    return abi if abi > 0 else 0


def _all_fs_rights(abi: int) -> int:
    rights = (FS_EXECUTE | FS_WRITE_FILE | FS_READ_FILE | FS_READ_DIR | FS_REMOVE_DIR | FS_REMOVE_FILE
              | FS_MAKE_CHAR | FS_MAKE_DIR | FS_MAKE_REG | FS_MAKE_SOCK | FS_MAKE_FIFO | FS_MAKE_BLOCK | FS_MAKE_SYM)
    if abi >= 2:
        rights |= FS_REFER
    if abi >= 3:
        rights |= FS_TRUNCATE
    if abi >= 5:
        rights |= FS_IOCTL_DEV
    return rights


def apply_landlock(rules: dict) -> int:
    """rules: {"rx": [paths], "ro": [paths], "rw": [paths]}. Fails closed: any error stops the job."""
    abi = landlock_abi()
    if abi <= 0:
        raise OSError("Landlock is not available")
    libc = _libc()
    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl(no_new_privs) failed")

    handled = _all_fs_rights(abi)
    net = (NET_BIND_TCP | NET_CONNECT_TCP) if abi >= 4 else 0
    scoped = (SCOPE_ABSTRACT_UNIX | SCOPE_SIGNAL) if abi >= 6 else 0
    if abi >= 6:
        attr = struct.pack("=QQQ", handled, net, scoped)
    elif abi >= 4:
        attr = struct.pack("=QQ", handled, net)
    else:
        attr = struct.pack("=Q", handled)
    buf = ctypes.create_string_buffer(attr, len(attr))
    ruleset = libc.syscall(ctypes.c_long(SYS_LANDLOCK_CREATE_RULESET), buf, ctypes.c_size_t(len(attr)), ctypes.c_uint32(0))
    if ruleset < 0:
        raise OSError(ctypes.get_errno(), "landlock_create_ruleset failed")

    read = FS_READ_FILE | FS_READ_DIR
    grants = {
        "rx": read | FS_EXECUTE,
        "ro": read,
        "rw": handled,
        # Sockets only, for LibreOffice's IPC pipe in /tmp: no ordinary file can be created or read.
        "sock": FS_READ_DIR | FS_MAKE_SOCK | FS_REMOVE_FILE,
        # For C's tmpfile(), which ignores TMPDIR and always uses /tmp: Cairo's PostScript output and
        # FontForge need it. It works without the right to create a named file, because it gets an
        # anonymous one. So nothing is visible there, and jobs still can't pass files to each other.
        "tmpfile": FS_READ_DIR | FS_READ_FILE | FS_WRITE_FILE | FS_TRUNCATE,
    }
    for kind, rights in grants.items():
        for path in rules.get(kind, []):
            try:
                fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            except FileNotFoundError:
                continue
            try:
                allowed = rights & _all_fs_rights(abi)
                if not os.path.isdir(path):
                    allowed &= _FILE_RIGHTS
                rule = struct.pack("=Qi", allowed, fd)
                rule_buf = ctypes.create_string_buffer(rule, len(rule))
                if libc.syscall(ctypes.c_long(SYS_LANDLOCK_ADD_RULE), ctypes.c_int(ruleset),
                                ctypes.c_int(LANDLOCK_RULE_PATH_BENEATH), rule_buf, ctypes.c_uint32(0)) != 0:
                    raise OSError(ctypes.get_errno(), f"landlock_add_rule failed for {path}")
            finally:
                os.close(fd)

    if libc.syscall(ctypes.c_long(SYS_LANDLOCK_RESTRICT_SELF), ctypes.c_int(ruleset), ctypes.c_uint32(0)) != 0:
        raise OSError(ctypes.get_errno(), "landlock_restrict_self failed")
    os.close(ruleset)
    return abi


def apply_limits(limits: dict) -> None:
    names = {
        "cpu": resource.RLIMIT_CPU,
        "fsize": resource.RLIMIT_FSIZE,
        "as": resource.RLIMIT_AS,
        "nofile": resource.RLIMIT_NOFILE,
        "nproc": resource.RLIMIT_NPROC,
        "core": resource.RLIMIT_CORE,
    }
    for name, value in limits.items():
        resource.setrlimit(names[name], (value, value))


def main() -> None:
    spec = json.loads(os.environ.pop("OMNI_SANDBOX_SPEC"))
    argv = sys.argv[1:]
    os.umask(0o077)
    # If memory runs out, the kernel kills the converter, not the supervisor.
    try:
        with open("/proc/self/oom_score_adj", "w") as fh:
            fh.write("1000")
    except OSError:
        pass
    apply_limits(spec.get("limits", {}))
    if spec.get("landlock"):
        apply_landlock(spec["landlock"])
    os.execvp(argv[0], argv)


if __name__ == "__main__":
    try:
        main()
    except Exception as err:  # the supervisor reads a non-zero exit as a failed step
        print(f"sandbox: {err}", file=sys.stderr)
        sys.exit(126)
