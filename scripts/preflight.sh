#!/usr/bin/env bash
# Checks this machine before you start the containers. It changes nothing.
#
#   scripts/preflight.sh
set -uo pipefail

cd "$(dirname "$0")/.."
fails=0
warns=0
pass() { printf '  ok    %s\n' "$1"; }
warn() { printf '  WARN  %s\n' "$1"; warns=$((warns + 1)); }
fail() { printf '  FAIL  %s\n' "$1"; fails=$((fails + 1)); }

echo "Machine"
arch=$(uname -m)
pass "$arch, kernel $(uname -r), $(nproc) cores, $(awk '/MemTotal/ {printf "%.1f GB", $2/1048576}' /proc/meminfo) RAM"

echo "Docker"
if ! command -v docker >/dev/null || ! docker info >/dev/null 2>&1; then
    fail "Docker isn't installed or this user can't use it"
    echo; echo "$fails problem(s)."; exit 1
fi
version=$(docker version --format '{{.Server.Version}}')
major=${version%%.*}
if [ "$major" -ge 28 ] 2>/dev/null; then
    pass "Docker $version"
else
    fail "Docker $version: before 28.0, machines on the same network can reach ports published to 127.0.0.1. Upgrade Docker."
fi
docker compose version >/dev/null 2>&1 && pass "Compose $(docker compose version --short)" || fail "Docker Compose v2 is missing"

# Docker only warns when the kernel can't apply a limit, so a limit in the compose file proves nothing.
if [ "$(docker info --format '{{.MemoryLimit}}')" = "true" ]; then
    pass "Memory limits work"
else
    fail "Memory limits DON'T work on this kernel, and the worker will refuse to start. On a Raspberry Pi, add this to the single line in /boot/firmware/cmdline.txt, then reboot: cgroup_enable=memory cgroup_memory=1"
fi
# Files are held in RAM on the promise that they can't reach a disk, and swap is a disk.
if [ "$(docker info --format '{{.SwapLimit}}')" = "true" ]; then
    pass "Swap limits work, so files held in memory can't be swapped to disk"
elif [ -n "$(tail -n +2 /proc/swaps)" ]; then
    fail "This machine has swap and Docker can't forbid it, so files held in memory could reach the disk. Add swapaccount=1 to the kernel command line, or turn swap off."
else
    pass "No swap on this machine, so files held in memory can't reach a disk"
fi
[ "$(docker info --format '{{.PidsLimit}}')" = "true" ] && pass "Process limits work" || fail "Process limits don't work, so a fork bomb could take down the machine"
[ "$(docker info --format '{{.CPUCfsQuota}}')" = "true" ] && pass "CPU limits work" || warn "CPU limits aren't available"

echo "Isolation"
lsm=$(cat /sys/kernel/security/lsm 2>/dev/null || true)
case ",$lsm," in
    *,landlock,*) pass "Landlock is on: a job can reach only its own folder and read-only system files" ;;
    *) warn "No Landlock (expected on Raspberry Pi OS). It's an extra layer here: other people's files are never in the worker either way. A malicious file that takes over a converter could read more of the worker's read-only system files, and stop the worker, which restarts. See README, \"Isolation\"." ;;
esac
case ",$lsm," in
    *,apparmor,*) pass "AppArmor is on: Docker's default profile applies" ;;
    *) warn "AppArmor is off, so Docker's default profile isn't applied. Optional extra layer: add lsm=apparmor to the kernel command line and install the apparmor package." ;;
esac
if docker info --format '{{.SecurityOptions}}' | grep -q seccomp; then
    pass "seccomp is on"
else
    fail "seccomp is off"
fi

echo "Permissions"
wide_folders=$(find . -type d -perm /077 | wc -l)
wide_files=$(find . -type f -perm /077 | wc -l)
if [ "$wide_folders" -eq 0 ] && [ "$wide_files" -eq 0 ]; then
    pass "This folder is private to $(id -un)"
else
    fail "$wide_folders folder(s) and $wide_files file(s) here can be read or changed by others. Run: bash scripts/lock-permissions.sh"
fi

echo "Settings"
if [ ! -f .env ]; then
    fail ".env is missing: cp .env.example .env && chmod 600 .env"
else
    mode=$(stat -c %a .env)
    [ "$mode" = "600" ] && pass ".env is private (600)" || fail ".env has mode $mode, so others could read it. Run: chmod 600 .env"
    value() { grep -E "^$1=" .env | tail -1 | cut -d= -f2- | sed -E 's/[[:space:]]+#.*$//; s/[[:space:]]+$//'; }

    ips=$(value ALLOWED_IPS)
    if [ -z "$ips" ]; then
        fail "ALLOWED_IPS is empty, so compose won't start"
    elif [ "$ips" = "all" ]; then
        warn "ALLOWED_IPS=all lets everyone in. This tool feeds uploads to complex parsers, so list the addresses you trust."
    else
        pass "ALLOWED_IPS=$ips"
    fi

    port=$(value HOST_PORT)
    if [ -z "$port" ]; then
        fail "HOST_PORT is empty"
    elif ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE "[:.]$port\$" && ! docker ps --format '{{.Names}}' | grep -q '^omniconvert-gateway$'; then
        fail "Port $port is already in use on this machine"
    else
        pass "Port $port is available"
    fi

    # Every file lives in RAM, so the limits have to fit in this machine with room left for everything else.
    bytes_of() {
        local v=${1:-$2}
        case "$v" in
            *[gG]) echo $(( ${v%[gG]} * 1073741824 )) ;;
            *[mM]) echo $(( ${v%[mM]} * 1048576 )) ;;
            *) echo "$v" ;;
        esac
    }
    gateway=$(bytes_of "$(value GATEWAY_MEMORY)" 2300M)
    worker=$(bytes_of "$(value WORKER_MEMORY)" 4G)
    total=$(awk '/MemTotal/ {print $2 * 1024}' /proc/meminfo)
    spare=$(( (total - gateway - worker) / 1048576 ))
    if [ "$spare" -lt 512 ]; then
        fail "GATEWAY_MEMORY and WORKER_MEMORY leave only $spare MiB of this machine's RAM for everything else. Lower them in .env."
    else
        pass "At their busiest the containers leave $spare MiB of RAM for everything else"
    fi
fi

echo "Host nginx"
if command -v nginx >/dev/null; then
    pass "nginx is installed. Add deploy/host-nginx-subpath.conf to your HTTPS server block."
else
    warn "nginx isn't installed here. The app is only reachable through a reverse proxy on this machine."
fi

echo
if [ "$fails" -gt 0 ]; then
    echo "$fails problem(s) to fix, $warns warning(s)."
    exit 1
fi
echo "Ready, with $warns warning(s)."
