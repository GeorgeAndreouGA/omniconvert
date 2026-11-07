"""A small client for the real HTTP API, used by the end-to-end, attack and coverage tests."""

from __future__ import annotations

import os
import time
from urllib.parse import quote

import httpx

BASE = os.environ.get("BASE", "http://127.0.0.1:8090")
# What the host nginx would add for a client on the allow-list.
ALLOWED = {"X-Real-IP": os.environ.get("TEST_CLIENT_IP", "203.0.113.7")}


class Client:
    def __init__(self, base: str = BASE, headers: dict | None = None):
        self.http = httpx.Client(base_url=base, headers=ALLOWED if headers is None else headers, timeout=120, follow_redirects=False)

    def upload(self, data: bytes, to: str, filename: str = "file.bin", hint: str | None = None) -> httpx.Response:
        params = {"to": to}
        if hint:
            params["from"] = hint
        return self.http.put("/api/jobs", params=params, content=data,
                             headers={"Content-Type": "application/octet-stream", "X-Filename": quote(filename, safe="")})

    def wait(self, job_id: str, timeout: float = 300) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            res = self.http.get(f"/api/jobs/{job_id}")
            if res.status_code == 429:  # our own polling hit the rate limit; that isn't the job's fault
                time.sleep(2)
                continue
            if res.status_code != 200:
                return {"state": "gone", "status": res.status_code}
            view = res.json()
            if view["state"] in ("done", "failed"):
                return view
            time.sleep(0.25)
        return {"state": "timeout"}

    def convert(self, data: bytes, to: str, filename: str = "file.bin", hint: str | None = None, timeout: float = 300, keep: bool = False):
        """Returns (view, output bytes or None). Waits for room in the line if it's full. Downloading is
        what deletes a result, so keep=True leaves it on the server, undownloaded."""
        res = None
        for _ in range(240):
            res = self.upload(data, to, filename, hint)
            if res.status_code not in (429, 503):
                break
            time.sleep(1)
        if res.status_code != 202:
            return {"state": "rejected", "status": res.status_code, "error": res.text[:300]}, None
        job_id = res.json()["id"]
        view = self.wait(job_id, timeout)
        output = None
        if not keep:
            if view["state"] == "done":
                output = self.http.get(f"/api/jobs/{job_id}/download").content
            self.http.delete(f"/api/jobs/{job_id}")
        view["id"] = job_id
        return view, output
