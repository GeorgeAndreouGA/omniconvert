#!/usr/bin/env python3
"""Writes docker/seccomp-gateway.json and docker/seccomp-worker.json from Docker's default profile.

    curl -fsSLO https://raw.githubusercontent.com/moby/profiles/6fe7deb1b9fb7c0397a4593480d7d22b9ee8caef/seccomp/default.json
    python3 scripts/make-seccomp.py default.json

Docker's default has to suit every container, so it allows a lot these two never need. What's taken
away is listed here so it can be read and argued with. Each profile starts from the default's plain
allow-list, keeps none of its capability-dependent rules (these containers hold no capabilities), and
accepts native 64-bit syscalls only, which removes the kernel's 32-bit compatibility layer as a target
and with it `socketcall`, the 32-bit call that would slip past the socket rules below.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

MSG_FASTOPEN = 0x20000000
SOCK_TYPE_MASK = 0xF
SOCK_STREAM = 1
AF_UNIX, AF_INET, AF_INET6 = 1, 2, 10

# Neither container needs these, and each has been a route to kernel bugs or between processes.
NEITHER = {
    # Reading or changing another process. Non-dumpable already protects the supervisor; this is a second lock.
    "ptrace", "process_vm_readv", "process_vm_writev", "process_madvise", "process_mrelease", "kcmp", "pidfd_getfd",
    # Shared state that outlives a process, so a job could leave something behind for the next one.
    "shmget", "shmat", "shmdt", "shmctl", "msgget", "msgsnd", "msgrcv", "msgctl",
    "semget", "semop", "semctl", "semtimedop", "semtimedop_time64",
    "mq_open", "mq_unlink", "mq_timedsend", "mq_timedsend_time64", "mq_timedreceive", "mq_timedreceive_time64",
    "mq_notify", "mq_getsetattr",
    # Running code with no file behind it, past the noexec mounts.
    "memfd_create", "memfd_secret",
    # Rarely used kernel interfaces with a poor record.
    "modify_ldt", "io_setup", "io_destroy", "io_submit", "io_cancel", "io_getevents", "io_pgetevents", "io_pgetevents_time64",
    "vmsplice", "tee", "userfaultfd", "adjtimex", "clock_adjtime", "clock_adjtime64", "quotactl", "quotactl_fd",
    "ioprio_set", "name_to_handle_at", "lookup_dcookie", "ustat", "sysfs", "uselib", "fanotify_mark",
    # 32-bit multiplexers. Unreachable with native-only filtering, and removed so that's plain to see.
    "socketcall", "ipc",
}

# The gateway only answers requests and stores bytes under no name. It starts nothing and links nothing.
GATEWAY_ONLY = {
    # With these gone it can't begin a network conversation at all, only answer one.
    "connect",
    "mknod", "mknodat", "symlink", "symlinkat", "link", "linkat", "rename", "renameat", "renameat2",
    "chown", "fchown", "fchownat", "lchown", "chown32", "fchown32", "lchown32",
    "setuid", "setgid", "setreuid", "setregid", "setresuid", "setresgid", "setgroups", "setfsuid", "setfsgid", "capset",
    "setuid32", "setgid32", "setreuid32", "setregid32", "setresuid32", "setresgid32", "setgroups32", "setfsuid32", "setfsgid32",
    "splice", "sendfile", "sendfile64", "copy_file_range",
    "inotify_init", "inotify_init1", "inotify_add_watch", "inotify_rm_watch",
    "landlock_create_ruleset", "landlock_add_rule", "landlock_restrict_self", "seccomp",
    # Replaced below by versions that refuse MSG_FASTOPEN, which starts a connection without connect().
    "sendto", "sendmsg", "sendmmsg",
}


def allow(*names: str, **extra) -> dict:
    return {"names": list(names), "action": "SCMP_ACT_ALLOW", **extra}


def arg(index: int, value: int, op: str = "SCMP_CMP_EQ", value_two: int | None = None) -> dict:
    rule = {"index": index, "value": value, "op": op}
    if value_two is not None:
        rule["valueTwo"] = value_two
    return rule


def base_names(default: dict) -> set[str]:
    names: set[str] = set()
    for rule in default["syscalls"]:
        if rule["action"] != "SCMP_ACT_ALLOW" or rule.get("args") or rule.get("excludes"):
            continue
        includes = rule.get("includes") or {}
        if not includes or set(includes.get("arches", [])) & {"amd64", "arm64"} and "caps" not in includes:
            names.update(rule["names"])
    return names - {"process_vm_readv", "process_vm_writev", "ptrace"}


def shared_rules(default: dict) -> list[dict]:
    rules = [r for r in default["syscalls"] if r["names"] == ["personality"]]
    rules = [allow("personality", args=r["args"]) for r in rules]
    # Threads and processes, but no new namespaces. glibc falls back to clone when clone3 says ENOSYS.
    rules.append(allow("clone", args=[arg(0, 0x7E020000, "SCMP_CMP_MASKED_EQ", 0)]))
    rules.append({"names": ["clone3"], "action": "SCMP_ACT_ERRNO", "errnoRet": 38})
    return rules


def profile(names: set[str], rules: list[dict]) -> dict:
    return {
        "defaultAction": "SCMP_ACT_ERRNO",
        "defaultErrnoRet": 1,
        "architectures": ["SCMP_ARCH_X86_64", "SCMP_ARCH_AARCH64"],
        "syscalls": [allow(*sorted(names)), *rules],
    }


def stream(family: int) -> dict:
    return allow("socket", args=[arg(0, family), arg(1, SOCK_TYPE_MASK, "SCMP_CMP_MASKED_EQ", SOCK_STREAM)])


def main() -> None:
    default = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    out = Path(__file__).resolve().parents[1] / "docker"
    names = base_names(default)
    names.discard("socket")
    names.discard("personality")
    names.discard("clone")
    shared = shared_rules(default)

    worker = profile(names - NEITHER, [*shared, allow("socket", args=[arg(0, AF_UNIX)])])
    gateway = profile(names - NEITHER - GATEWAY_ONLY, [
        *shared,
        allow("socket", args=[arg(0, AF_UNIX)]),
        stream(AF_INET),
        stream(AF_INET6),
        allow("sendto", args=[arg(3, MSG_FASTOPEN, "SCMP_CMP_MASKED_EQ", 0)]),
        allow("sendmsg", args=[arg(2, MSG_FASTOPEN, "SCMP_CMP_MASKED_EQ", 0)]),
        allow("sendmmsg", args=[arg(3, MSG_FASTOPEN, "SCMP_CMP_MASKED_EQ", 0)]),
    ])
    for name, data in (("seccomp-gateway.json", gateway), ("seccomp-worker.json", worker)):
        (out / name).write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
        print(f"{name}: {len(data['syscalls'][0]['names'])} plain syscalls, {len(data['syscalls']) - 1} conditional rules")


if __name__ == "__main__":
    main()
