#!/usr/bin/env bash
# Proves the running containers are locked down, by asking Docker and the kernel and by trying things
# that must fail. Settings in a compose file prove nothing on their own: on a Raspberry Pi, Docker can
# drop a memory limit with only a warning.
#
#   docker compose up -d && scripts/verify-hardening.sh
set -uo pipefail

GATEWAY=${GATEWAY:-omniconvert-gateway}
WORKER=${WORKER:-omniconvert-worker}
fails=0
pass() { printf '  ok    %s\n' "$1"; }
fail() { printf '  FAIL  %s\n' "$1"; fails=$((fails + 1)); }
expect() { # description, expected, actual
    if [ "$2" = "$3" ]; then pass "$1"; else fail "$1 (expected '$2', got '$3')"; fi
}
inspect() { docker inspect --format "$2" "$1" 2>/dev/null; }
run_in() { local c=$1; shift; docker exec "$c" "$@" 2>&1; }
# Runs in a container with no shell tools assumed, and passes if the action is refused.
python_of() { [ "$1" = "$WORKER" ] && echo /opt/py/bin/python || echo python; }
refused() { # description, container, python snippet that must raise
    local out
    if out=$(docker exec "$2" "$(python_of "$2")" -I -c "$3" 2>&1); then
        fail "$1 (it succeeded: $out)"
    else
        pass "$1"
    fi
}

for c in "$GATEWAY" "$WORKER"; do
    [ "$(inspect "$c" '{{.State.Running}}')" = "true" ] || { echo "$c isn't running. Start it: docker compose up -d"; exit 1; }
done

for c in "$GATEWAY" "$WORKER"; do
    echo "$c: container settings"
    user=$(inspect "$c" '{{.Config.User}}')
    case "$user" in
        ""|0|root|0:*|root:*) fail "Runs as root ($user)" ;;
        *) pass "Non-root user $user" ;;
    esac
    expect "Read-only root filesystem" true "$(inspect "$c" '{{.HostConfig.ReadonlyRootfs}}')"
    expect "All capabilities dropped" "[ALL]" "$(inspect "$c" '{{.HostConfig.CapDrop}}')"
    expect "No capabilities added" "[]" "$(inspect "$c" '{{.HostConfig.CapAdd}}')"
    expect "Not privileged" false "$(inspect "$c" '{{.HostConfig.Privileged}}')"
    case "$(inspect "$c" '{{.HostConfig.SecurityOpt}}')" in
        *no-new-privileges*) pass "no-new-privileges" ;;
        *) fail "no-new-privileges is missing" ;;
    esac
    expect "No shared IPC" none "$(inspect "$c" '{{.HostConfig.IpcMode}}')"
    if inspect "$c" '{{range .Mounts}}{{.Source}} {{end}}' | grep -q docker.sock; then
        fail "The Docker socket is mounted"
    else
        pass "The Docker socket isn't mounted"
    fi

    echo "$c: what the kernel reports from inside"
    uid=$(run_in "$c" "$(python_of "$c")" -I -c "import os; print(os.getuid())")
    [ "$uid" != "0" ] && [ -n "$uid" ] && pass "Really running as uid $uid" || fail "Running as uid '$uid'"
    caps=$(run_in "$c" "$(python_of "$c")" -I -c "print([l.split()[1] for l in open('/proc/self/status') if l.startswith('CapEff')][0])")
    expect "Holds no capabilities" 0000000000000000 "$caps"
    nnp=$(run_in "$c" "$(python_of "$c")" -I -c "print([l.split()[1] for l in open('/proc/self/status') if l.startswith('NoNewPrivs')][0])")
    expect "no_new_privs is set" 1 "$nnp"
    filtering=$(run_in "$c" "$(python_of "$c")" -I -c "print([l.split()[1] for l in open('/proc/self/status') if l.startswith('Seccomp:')][0])")
    expect "A syscall filter is active" 2 "$filtering"
    groups=$(run_in "$c" "$(python_of "$c")" -I -c "import os; print(os.getgroups())")
    [ "$c" = "$GATEWAY" ] && mine=10001 || mine=10002
    expect "In its own group and no other" "[$mine]" "$groups"

    memory=$(run_in "$c" "$(python_of "$c")" -I -c "print(open('/sys/fs/cgroup/memory.max').read().strip())")
    case "$memory" in
        ''|max|*rror*) fail "No memory limit is in force ($memory)" ;;
        *) pass "Memory limit in force: $((memory / 1048576)) MiB" ;;
    esac
    swap=$(run_in "$c" "$(python_of "$c")" -I -c "print(open('/sys/fs/cgroup/memory.swap.max').read().strip())")
    expect "No swap allowed" 0 "$swap"
    pids=$(run_in "$c" "$(python_of "$c")" -I -c "print(open('/sys/fs/cgroup/pids.max').read().strip())")
    case "$pids" in
        ''|max|*rror*) fail "No process limit is in force ($pids)" ;;
        *) pass "Process limit in force: $pids" ;;
    esac
    cpu=$(run_in "$c" "$(python_of "$c")" -I -c "print(open('/sys/fs/cgroup/cpu.max').read().strip())")
    case "$cpu" in
        max*|*rror*|'') fail "No CPU limit is in force ($cpu)" ;;
        *) pass "CPU limit in force: $(echo "$cpu" | awk '{printf "%.2f", $1/$2}') cores" ;;
    esac
    core=$(run_in "$c" "$(python_of "$c")" -I -c "import resource; print(resource.getrlimit(resource.RLIMIT_CORE)[1])")
    expect "Core dumps are off" 0 "$core"
    nofile=$(run_in "$c" "$(python_of "$c")" -I -c "import resource; print(resource.getrlimit(resource.RLIMIT_NOFILE)[1])")
    expect "Open file limit" 4096 "$nofile"
    log_size=$(inspect "$c" '{{index .HostConfig.LogConfig.Config "max-size"}}')
    expect "Logs are rotated" 10m "$log_size"

    echo "$c: things that must fail"
    refused "Can't write to the root filesystem" "$c" "open('/usr/pwned','w')"
    refused "Can't write to the application" "$c" "open('/app/pwned','w')"
    refused "Can't change to root" "$c" "import os; os.setuid(0)"
    refused "Can't create a user namespace" "$c" "import ctypes,os; l=ctypes.CDLL(None,use_errno=True); assert l.unshare(0x10000000)==0, 'denied'"
    refused "Can't mount anything" "$c" "import ctypes; l=ctypes.CDLL(None,use_errno=True); assert l.mount(b'x',b'/tmp',b'tmpfs',0,None)==0, 'denied'"
    refused "Can't attach to another process" "$c" "import ctypes; l=ctypes.CDLL(None,use_errno=True); assert l.ptrace(16,1,0,0)==0, 'denied'"
    refused "Can't create a file with no name to run" "$c" "import ctypes; l=ctypes.CDLL(None,use_errno=True); assert l.memfd_create(b'x',0)>=0, 'denied'"
    refused "Can't use shared memory that outlives a process" "$c" "import ctypes; l=ctypes.CDLL(None,use_errno=True); assert l.shmget(0,4096,0o600)>=0, 'denied'"
    refused "Can't open a raw packet socket" "$c" "import socket; socket.socket(17,3)"
    refused "Can't open a kernel netlink socket" "$c" "import socket; socket.socket(16,3)"
    refused "Can't write to /dev/mqueue" "$c" "open('/dev/mqueue/x','w')"
    refused "Has no /dev/shm" "$c" "import os; os.listdir('/dev/shm')"
    suid=$(run_in "$c" "$(python_of "$c")" -I -c "
import os,stat
found=[]
for base in ['/usr','/bin','/sbin','/opt','/app','/etc','/lib','/var']:
    for r,d,f in os.walk(base):
        for n in f:
            p=os.path.join(r,n)
            try:
                if os.lstat(p).st_mode & (stat.S_ISUID|stat.S_ISGID) and stat.S_ISREG(os.lstat(p).st_mode): found.append(p)
            except OSError: pass
print(len(found), found[:3] if found else '')" | tr -d '\n ')
    expect "No setuid or setgid programs" 0 "$suid"
    # Everything this user could write to, found by trying the mount table and the permissions together.
    writable=$(run_in "$c" "$(python_of "$c")" -I -c "
import os
found=[]
for line in open('/proc/self/mountinfo'):
    fields=line.split()
    path,options=fields[4],fields[5].split(',')
    if 'ro' in options or path.startswith(('/proc','/sys','/dev/pts')):
        continue
    if os.access(path,os.W_OK):
        found.append(path)
print(' '.join(sorted(found)))")
    [ "$c" = "$GATEWAY" ] && places="/jobs /run/omniconvert" || places="/tmp /work"
    expect "Can write only to its RAM mounts" "$places" "$writable"
done

echo "Network"
binding=$(inspect "$GATEWAY" '{{range $p,$b := .NetworkSettings.Ports}}{{range $b}}{{.HostIp}} {{end}}{{end}}' | xargs)
expect "Gateway published on 127.0.0.1 only" 127.0.0.1 "$binding"
expect "Worker has no network" none "$(inspect "$WORKER" '{{.HostConfig.NetworkMode}}')"
worker_ifaces=$(run_in "$WORKER" /opt/py/bin/python -I -c "print(sorted(l.split(':')[0].strip() for l in open('/proc/net/dev') if ':' in l))")
expect "Worker has only loopback" "['lo']" "$worker_ifaces"
refused "Worker can't open a network socket at all" "$WORKER" "import socket; socket.socket(socket.AF_INET)"
refused "Worker can't open an IPv6 socket at all" "$WORKER" "import socket; socket.socket(socket.AF_INET6)"
refused "Gateway can't connect anywhere" "$GATEWAY" "import socket; socket.create_connection(('1.1.1.1',443),5)"
refused "Gateway can't connect even to itself" "$GATEWAY" "import socket; socket.create_connection(('127.0.0.1',8080),5)"
refused "Gateway can't start a connection with TCP Fast Open" "$GATEWAY" "import socket; s=socket.socket(); s.sendto(b'x',0x20000000,('1.1.1.1',443))"
refused "Gateway can't send UDP" "$GATEWAY" "import socket; socket.socket(socket.AF_INET,socket.SOCK_DGRAM)"
refused "Gateway can't ping" "$GATEWAY" "import socket; socket.socket(socket.AF_INET,socket.SOCK_DGRAM,1)"

echo "Nothing is kept"
for c in "$GATEWAY" "$WORKER"; do
    expect "$c has no host folders mounted" "" "$(inspect "$c" '{{range .Mounts}}{{if eq .Type "bind"}}{{.Source}} {{end}}{{end}}')"
    expect "$c shares only the socket" "volume:/run/omniconvert" "$(inspect "$c" '{{range .Mounts}}{{.Type}}:{{.Destination}} {{end}}' | xargs)"
    swap=$(run_in "$c" "$(python_of "$c")" -I -c "print(open('/sys/fs/cgroup/memory.swap.current').read().strip())")
    expect "$c has nothing in swap" 0 "$swap"
    expect "$c changed nothing in its image" 0 "$(docker diff "$c" 2>/dev/null | wc -l | xargs)"
done
fs_of() { run_in "$1" "$(python_of "$1")" -I -c "print([l.split(' - ')[1].split()[0] for l in open('/proc/self/mountinfo') if l.split()[4]=='$2'][-1])"; }
expect "Gateway's job store is in RAM" tmpfs "$(fs_of "$GATEWAY" /jobs)"
expect "Worker's work folder is in RAM" tmpfs "$(fs_of "$WORKER" /work)"
expect "Worker's /tmp is in RAM" tmpfs "$(fs_of "$WORKER" /tmp)"
expect "The socket folder is in RAM" tmpfs "$(fs_of "$WORKER" /run/omniconvert)"

echo "Folders"
mode_of() { run_in "$1" "$(python_of "$1")" -I -c "import os,stat; s=os.stat('$2'); print(oct(stat.S_IMODE(s.st_mode))[2:],s.st_uid,s.st_gid)"; }
expect "Job store: gateway only" "700 10001 10001" "$(mode_of "$GATEWAY" /jobs)"
expect "Work folder: worker only" "700 10002 10002" "$(mode_of "$WORKER" /work)"
expect "/tmp: worker only" "700 10002 10002" "$(mode_of "$WORKER" /tmp)"
expect "Socket folder: gateway owns it, worker's group may enter" "2750 10001 10002" "$(mode_of "$WORKER" /run/omniconvert)"
expect "Socket: nobody else" "660 10001 10002" "$(mode_of "$WORKER" /run/omniconvert/link.sock)"
for c in "$GATEWAY" "$WORKER"; do
    expect "Application in $c: read-only and root's" "555 0 0" "$(mode_of "$c" /app)"
done
refused "Worker can't see the job store" "$WORKER" "import os; os.listdir('/jobs')"
refused "Worker can't write beside the socket" "$WORKER" "open('/run/omniconvert/x','w')"
refused "Worker can't replace the socket" "$WORKER" "import os; os.unlink('/run/omniconvert/link.sock')"
refused "Gateway can't see the work folder" "$GATEWAY" "import os; os.listdir('/work')"
refused "Gateway has no shell" "$GATEWAY" "import subprocess; subprocess.run(['/bin/sh','-c','true'],check=True)"
expect "Gateway has nothing else to run" "['true']" "$(run_in "$GATEWAY" python -I -c "import os; print(sorted(os.listdir('/usr/bin') + os.listdir('/usr/sbin')))")"
refused "Worker can't read gateway settings" "$WORKER" "import os; assert 'API_TOKEN' in os.environ, 'absent'"
refused "Job store won't run programs" "$GATEWAY" "
import os,shutil,subprocess
shutil.copy('/bin/true','/jobs/t'); os.chmod('/jobs/t',0o755)
try: subprocess.run(['/jobs/t'],check=True)
finally: os.unlink('/jobs/t')"
refused "Work folder won't run programs" "$WORKER" "
import os,shutil,subprocess
shutil.copy('/bin/true','/work/t'); os.chmod('/work/t',0o755)
try: subprocess.run(['/work/t'],check=True)
finally: os.unlink('/work/t')"
refused "/tmp won't run programs" "$WORKER" "
import os,shutil,subprocess
shutil.copy('/bin/true','/tmp/t'); os.chmod('/tmp/t',0o755)
try: subprocess.run(['/tmp/t'],check=True)
finally: os.unlink('/tmp/t')"

echo
if [ "$fails" -gt 0 ]; then
    echo "$fails check(s) FAILED."
    exit 1
fi
echo "All checks passed."
