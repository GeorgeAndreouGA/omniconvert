"""The syscall filters are small enough to be wrong by one line, so what they promise is pinned here."""

import json
from pathlib import Path

import pytest

DOCKER = Path(__file__).resolve().parents[2] / "docker"
GATEWAY = json.loads((DOCKER / "seccomp-gateway.json").read_text())
WORKER = json.loads((DOCKER / "seccomp-worker.json").read_text())
AF_UNIX, AF_INET, AF_INET6 = 1, 2, 10


def plain(profile):
    return set(profile["syscalls"][0]["names"])


def rules_for(profile, name):
    return [rule for rule in profile["syscalls"] if name in rule["names"] and rule.get("args")]


def socket_families(profile):
    return {arg["value"] for rule in rules_for(profile, "socket") for arg in rule["args"] if arg["index"] == 0}


@pytest.mark.parametrize("profile", [GATEWAY, WORKER], ids=["gateway", "worker"])
def test_denied_by_default_and_native_64_bit_only(profile):
    assert profile["defaultAction"] == "SCMP_ACT_ERRNO"
    assert profile["architectures"] == ["SCMP_ARCH_X86_64", "SCMP_ARCH_AARCH64"]
    assert "archMap" not in profile


@pytest.mark.parametrize("profile", [GATEWAY, WORKER], ids=["gateway", "worker"])
@pytest.mark.parametrize("name", [
    "ptrace", "process_vm_readv", "process_vm_writev", "pidfd_getfd", "kcmp", "memfd_create", "shmget", "msgget", "semget",
    "mq_open", "io_uring_setup", "userfaultfd", "bpf", "perf_event_open", "keyctl", "add_key", "request_key", "unshare", "setns",
    "mount", "umount2", "pivot_root", "chroot", "reboot", "init_module", "finit_module", "kexec_load", "modify_ldt", "io_submit",
    "vmsplice", "socketcall", "ipc",
])
def test_never_allowed(profile, name):
    assert not [rule for rule in profile["syscalls"] if name in rule["names"] and rule["action"] == "SCMP_ACT_ALLOW"]


@pytest.mark.parametrize("profile", [GATEWAY, WORKER], ids=["gateway", "worker"])
def test_no_new_namespaces(profile):
    assert "clone" not in plain(profile)
    [rule] = rules_for(profile, "clone")
    assert rule["args"] == [{"index": 0, "value": 0x7E020000, "op": "SCMP_CMP_MASKED_EQ", "valueTwo": 0}]
    [clone3] = [r for r in profile["syscalls"] if "clone3" in r["names"]]
    assert clone3["action"] == "SCMP_ACT_ERRNO" and clone3["errnoRet"] == 38


def test_the_worker_can_only_open_local_sockets():
    assert "socket" not in plain(WORKER)
    assert socket_families(WORKER) == {AF_UNIX}


def test_the_gateway_can_only_answer_connections():
    assert "connect" not in plain(GATEWAY)
    assert "socket" not in plain(GATEWAY)
    assert socket_families(GATEWAY) == {AF_UNIX, AF_INET, AF_INET6}
    for family in (AF_INET, AF_INET6):
        [rule] = [r for r in rules_for(GATEWAY, "socket") if r["args"][0]["value"] == family]
        assert rule["args"][1] == {"index": 1, "value": 15, "op": "SCMP_CMP_MASKED_EQ", "valueTwo": 1}  # SOCK_STREAM


@pytest.mark.parametrize("name,flags_at", [("sendto", 3), ("sendmsg", 2), ("sendmmsg", 3)])
def test_the_gateway_cannot_start_a_connection_with_fast_open(name, flags_at):
    assert name not in plain(GATEWAY)
    [rule] = rules_for(GATEWAY, name)
    assert rule["args"] == [{"index": flags_at, "value": 0x20000000, "op": "SCMP_CMP_MASKED_EQ", "valueTwo": 0}]


# arm64 has no open(), stat(), pipe() or the like: only the *at and newer forms. Nothing on x86_64 would
# notice one of these going missing, and the Raspberry Pi wouldn't start.
@pytest.mark.parametrize("profile", [GATEWAY, WORKER], ids=["gateway", "worker"])
@pytest.mark.parametrize("name", [
    "openat", "openat2", "close", "read", "write", "pread64", "pwrite64", "lseek", "newfstatat", "fstat", "statx", "faccessat", "faccessat2",
    "getdents64", "dup3", "pipe2", "fcntl", "ioctl", "mmap", "munmap", "mprotect", "brk", "madvise", "mremap", "futex", "rseq", "set_tid_address",
    "set_robust_list", "rt_sigaction", "rt_sigprocmask", "rt_sigreturn", "sigaltstack", "execve", "exit", "exit_group", "wait4", "waitid", "kill",
    "tgkill", "getpid", "gettid", "getuid", "getrandom", "prlimit64", "prctl", "sched_getaffinity", "clock_gettime", "clock_nanosleep",
    "epoll_create1", "epoll_ctl", "epoll_pwait", "eventfd2", "ppoll", "pselect6", "unlinkat", "mkdirat", "fchmod", "fchmodat", "utimensat",
    "ftruncate", "bind", "listen", "accept4", "getsockopt", "setsockopt", "getsockname", "getpeername", "recvfrom", "recvmsg", "shutdown", "socketpair",
    "close_range", "getcwd", "chdir", "fchdir", "readlinkat", "statfs", "fstatfs", "uname", "umask",
])
def test_what_arm64_needs_is_there(profile, name):
    assert name in plain(profile), name


@pytest.mark.parametrize("name", ["connect", "landlock_create_ruleset", "landlock_add_rule", "landlock_restrict_self", "symlinkat", "linkat", "renameat2", "fchownat", "sendto", "sendmsg", "splice", "copy_file_range", "sendfile", "seccomp"])
def test_what_converters_need_is_there(name):
    assert name in plain(WORKER), name
