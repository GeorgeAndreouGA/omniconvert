"""Settings from the environment. Anything invalid stops the gateway instead of weakening it."""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass
from pathlib import Path

from gateway.access import AllowList
from shared.link import SOCKET_PATH

# What the gateway needs for itself, on top of the files it holds in memory.
OWN_MEMORY_BYTES = 128 * 1024 * 1024


class ConfigError(Exception):
    pass


def _int(env: dict, name: str, default: int, minimum: int = 0) -> int:
    raw = env.get(name, "").strip() or str(default)
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a whole number, got {raw!r}") from None
    if value < minimum:
        raise ConfigError(f"{name} must be at least {minimum}, got {value}")
    return value


@dataclass(frozen=True)
class Settings:
    allow: AllowList
    trusted_proxies: frozenset[str]
    api_token: str
    base_path: str
    max_upload_bytes: int
    max_output_bytes: int
    job_timeout_seconds: int
    retention_minutes: int
    max_queue: int
    max_jobs_per_ip: int
    rate_limit_per_minute: int
    job_dir: Path
    socket_path: str
    worker_uid: int
    page_path: Path

    @classmethod
    def from_env(cls, env: dict | None = None) -> "Settings":
        env = os.environ if env is None else env

        try:
            allow = AllowList.parse(env.get("ALLOWED_IPS", ""))
        except ValueError as err:
            raise ConfigError(f"ALLOWED_IPS: {err}") from None

        proxies = set()
        for item in env.get("TRUSTED_PROXIES", "").split(","):
            item = item.strip()
            if not item:
                continue
            try:
                proxies.add(str(ipaddress.ip_address(item)))
            except ValueError:
                raise ConfigError(f"TRUSTED_PROXIES: {item!r} is not an IP address") from None

        base_path = env.get("BASE_PATH", "").strip().rstrip("/")
        if base_path and not re.fullmatch(r"(/[A-Za-z0-9._~-]+)+", base_path):
            raise ConfigError(f"BASE_PATH must look like /convert, got {base_path!r}")

        token = env.get("API_TOKEN", "").strip()
        if token and len(token) < 16:
            raise ConfigError("API_TOKEN is too short; use at least 16 characters (openssl rand -hex 32)")

        return cls(
            allow=allow,
            trusted_proxies=frozenset(proxies),
            api_token=token,
            base_path=base_path,
            max_upload_bytes=_int(env, "MAX_UPLOAD_BYTES", 1024**3, 1),
            max_output_bytes=_int(env, "MAX_OUTPUT_BYTES", 1536 * 1024**2, 1),
            job_timeout_seconds=_int(env, "JOB_TIMEOUT_SECONDS", 1800, 1),
            retention_minutes=_int(env, "RETENTION_MINUTES", 10, 1),
            max_queue=_int(env, "MAX_QUEUE", 20, 1),
            max_jobs_per_ip=_int(env, "MAX_JOBS_PER_IP", 10, 1),
            rate_limit_per_minute=_int(env, "RATE_LIMIT_PER_MINUTE", 600, 1),
            job_dir=Path(env.get("JOB_DIR", "/jobs")),
            socket_path=env.get("LINK_SOCKET", SOCKET_PATH),
            worker_uid=_int(env, "WORKER_UID", 10002, 1),
            page_path=Path(env.get("PAGE_PATH", "/app/ui/page.html")),
        )


def memory_problem(settings: Settings, store_bytes: int, memory_limit: int | None) -> str | None:
    """Files in the RAM mount count against this container's memory limit. If the mount could outgrow the
    limit, a large upload would get the gateway killed instead of a clean "memory is full"."""
    if memory_limit is not None and memory_limit < store_bytes + OWN_MEMORY_BYTES:
        return (f"GATEWAY_MEMORY ({memory_limit // 1024**2} MiB) must be at least JOB_STORE_SIZE "
                f"({store_bytes // 1024**2} MiB) plus {OWN_MEMORY_BYTES // 1024**2} MiB. Raise it or lower JOB_STORE_SIZE in .env.")
    if settings.max_upload_bytes > store_bytes:
        return f"MAX_UPLOAD_BYTES is larger than JOB_STORE_SIZE ({store_bytes // 1024**2} MiB), so that upload could never fit."
    if settings.max_output_bytes > store_bytes:
        return f"MAX_OUTPUT_BYTES is larger than JOB_STORE_SIZE ({store_bytes // 1024**2} MiB), so that result could never fit."
    return None
