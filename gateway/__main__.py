"""Start the gateway. A bad setting stops it here, with the reason in `docker logs`."""

import os
import sys
from pathlib import Path

import uvicorn

from gateway.app import create_app
from gateway.config import ConfigError, Settings, memory_problem


def memory_limit() -> int | None:
    try:
        raw = Path("/sys/fs/cgroup/memory.max").read_text().strip()
    except OSError:
        return None
    return None if raw == "max" else int(raw)


def main() -> None:
    try:
        settings = Settings.from_env()
    except ConfigError as err:
        print(f"omniconvert-gateway: {err}", file=sys.stderr)
        sys.exit(2)

    stats = os.statvfs(settings.job_dir)
    problem = memory_problem(settings, stats.f_blocks * stats.f_frsize, memory_limit())
    if problem:
        print(f"omniconvert-gateway: {problem}", file=sys.stderr)
        sys.exit(2)

    uvicorn.run(
        create_app(settings),
        host="0.0.0.0",
        port=8080,
        # The app reads X-Real-IP itself, and only from the trusted proxy (gateway/access.py).
        proxy_headers=False,
        server_header=False,
        date_header=False,
        access_log=False,
        timeout_keep_alive=30,
        limit_concurrency=128,
        h11_max_incomplete_event_size=32768,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
