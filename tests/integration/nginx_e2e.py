"""End to end through the real nginx snippet, under a subpath, with real client addresses.

    PYTHONPATH=. python tests/integration/nginx_e2e.py

It runs nginx in a throwaway container on the host's network, serving the actual
deploy/host-nginx-subpath.conf at /convert/. The port is 18443, plain HTTP, so no certificate is
needed. It then talks to it the way a browser would. Allowed and refused addresses are simulated with
the real_ip module in the test server only, so the snippet itself is exactly what ships.
"""

from __future__ import annotations

import http.client
import io
import os
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from urllib.parse import quote

import httpx

ROOT = Path(__file__).resolve().parents[2]
PORT = 18443
NAME = "omniconvert-nginx-test"
NGINX_IMAGE = os.environ.get("NGINX_IMAGE", "nginx:1.29-alpine")
results = []


def record(name, ok, detail=""):
    results.append((name, ok))
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}: {detail}", flush=True)


def config() -> str:
    snippet = (ROOT / "deploy" / "host-nginx-subpath.conf").read_text(encoding="utf-8")
    return f"""
events {{}}
http {{
    # Test only: lets this script choose the client address nginx sees, standing in for real clients.
    # The test header is on purpose not X-Real-IP, to show that a client's own X-Real-IP gets overwritten.
    set_real_ip_from 127.0.0.1;
    real_ip_header   X-Test-Client;
    server {{
        listen {PORT};
{snippet}
    }}
}}
"""


def start_nginx(folder: str):
    path = Path(folder) / "nginx.conf"
    path.write_text(config(), encoding="utf-8")
    os.chmod(folder, 0o755)
    os.chmod(path, 0o644)
    subprocess.run(["docker", "rm", "-f", NAME], capture_output=True, check=False)
    subprocess.run(["docker", "run", "-d", "--name", NAME, "--network", "host", "-v", f"{path}:/etc/nginx/nginx.conf:ro", NGINX_IMAGE],
                   check=True, capture_output=True)
    for _ in range(40):
        try:
            httpx.get(f"http://127.0.0.1:{PORT}/convert", timeout=1)
            return
        except httpx.HTTPError:
            time.sleep(0.25)
    raise RuntimeError(subprocess.run(["docker", "logs", NAME], capture_output=True, text=True, check=False).stderr)


def stop_nginx():
    subprocess.run(["docker", "rm", "-f", NAME], capture_output=True, check=False)


def main():
    base = f"http://127.0.0.1:{PORT}"
    allowed = httpx.Client(base_url=base, headers={"X-Test-Client": "203.0.113.7"}, timeout=120)
    refused = httpx.Client(base_url=base, headers={"X-Test-Client": "198.51.100.9"}, timeout=30)

    print("Subpath")
    res = allowed.get("/convert")
    record("/convert redirects to /convert/", res.status_code == 301 and res.headers.get("location", "").endswith("/convert/"), f"{res.status_code} {res.headers.get('location')}")
    res = allowed.get("/convert/")
    record("Page loads at the subpath", res.status_code == 200 and "OmniConvert" in res.text, str(res.status_code))
    record("It's the live version", 'name="omniconvert-live"' in res.text)
    record("HSTS added by nginx", "strict-transport-security" in res.headers)
    record("CSP sent once", len(res.headers.get_list("content-security-policy")) == 1)
    record("Page has no root-relative links", 'href="/' not in res.text and 'src="/' not in res.text)
    res = allowed.get("/convert/api/formats")
    data = res.json()
    record("API works at the subpath", res.status_code == 200 and data["server"]["worker_online"], f"{len(data['formats'])} formats, isolation {data['server']['isolation']}")
    record("Other paths on the domain aren't this app", allowed.get("/api/formats").status_code == 404)

    print("Allow-list, with addresses nginx really saw")
    record("Allowed address gets in", allowed.get("/convert/").status_code == 200)
    res = refused.get("/convert/")
    record("Other address is refused", res.status_code == 403, res.text[:60])
    record("Refused on the API too", refused.get("/convert/api/formats").status_code == 403)
    res = refused.get("/convert/", headers={"X-Real-IP": "203.0.113.7"})
    record("A client can't spoof X-Real-IP past nginx", res.status_code == 403, str(res.status_code))
    res = refused.get("/convert/", headers={"X-Forwarded-For": "203.0.113.7"})
    record("A client can't spoof X-Forwarded-For", res.status_code == 403, str(res.status_code))
    direct = httpx.get(f"http://127.0.0.1:{os.environ.get('HOST_PORT', '8090')}/", timeout=10)
    record("Straight to the port with no header is refused", direct.status_code == 403, str(direct.status_code))

    print("Whole job through nginx")
    private_name = "Τιμολόγιο-private-name <final>.txt"
    res = allowed.put("/convert/api/jobs", params={"to": "zip", "from": "txt"}, content=b"hello through nginx", headers={"X-Filename": quote(private_name, safe="")})
    record("Upload accepted", res.status_code == 202, str(res.status_code))
    job_id = res.json()["id"]
    view = {}
    for _ in range(120):
        view = allowed.get(f"/convert/api/jobs/{job_id}").json()
        if view["state"] in ("done", "failed"):
            break
        time.sleep(0.25)
    record("Job finishes", view["state"] == "done", str(view.get("error") or view["output"]))
    res = allowed.get(f"/convert/api/jobs/{job_id}/download")
    record("Download works", res.status_code == 200)
    record("Offered as an attachment", res.headers["content-disposition"].startswith("attachment;"), res.headers["content-disposition"][:70])
    record("Never rendered in the browser", res.headers["content-type"] == "application/octet-stream" and "sandbox" in res.headers["content-security-policy"])
    with zipfile.ZipFile(io.BytesIO(res.content)) as archive:
        record("Result is correct", archive.read(archive.namelist()[0]) == b"hello through nginx", str(archive.namelist()))
    record("The download deleted it", allowed.get(f"/convert/api/jobs/{job_id}/download").status_code == 404)
    record("Its status is gone too", allowed.get(f"/convert/api/jobs/{job_id}").status_code == 404)
    logs = subprocess.run(["docker", "logs", NAME], capture_output=True, text=True, check=False)
    said = logs.stdout + logs.stderr
    record("nginx logged neither the job's address nor the file's name", job_id not in said and "private-name" not in said and quote(private_name, safe="") not in said, f"{len(said)} bytes of log")

    print("Deleting by hand")
    res = allowed.put("/convert/api/jobs", params={"to": "zip", "from": "txt"}, content=b"delete me", headers={"X-Filename": "a.txt"})
    job_id = res.json()["id"]
    for _ in range(120):
        view = allowed.get(f"/convert/api/jobs/{job_id}").json()
        if view["state"] in ("done", "failed"):
            break
        time.sleep(0.25)
    record("Delete works", allowed.delete(f"/convert/api/jobs/{job_id}").status_code == 204)
    record("Gone afterwards", allowed.get(f"/convert/api/jobs/{job_id}/download").status_code == 404)

    print("Large upload")
    size = 300 * 1024 * 1024

    def stream():
        chunk = b"\0" * (1024 * 1024)
        for _ in range(size // len(chunk)):
            yield chunk

    started = time.monotonic()
    res = allowed.put("/convert/api/jobs", params={"to": "zst"}, content=stream(), headers={"Content-Length": str(size), "X-Filename": "big.bin"})
    took = time.monotonic() - started
    record("300 MiB goes through", res.status_code == 202, f"HTTP {res.status_code} in {took:.1f}s ({size / 1048576 / max(took, 0.01):.0f} MiB/s through the 0.5 CPU gateway)")
    if res.status_code == 202:
        job_id = res.json()["id"]
        for _ in range(240):
            view = allowed.get(f"/convert/api/jobs/{job_id}").json()
            if view["state"] in ("done", "failed"):
                break
            time.sleep(0.5)
        record("Large job finishes", view["state"] == "done", str(view.get("error") or view.get("output")))
        allowed.delete(f"/convert/api/jobs/{job_id}")
    # Only the headers are sent: nginx should answer from the declared size, before any body.
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=15)
    conn.putrequest("PUT", "/convert/api/jobs?to=zip")
    conn.putheader("X-Test-Client", "203.0.113.7")
    conn.putheader("Content-Length", str(2 * 1024**3))
    conn.endheaders()
    status = conn.getresponse().status
    conn.close()
    record("Past the limit is refused by nginx at once", status == 413, str(status))

    failed = [name for name, ok in results if not ok]
    print(f"\n{len(results) - len(failed)} of {len(results)} checks passed.")
    for name in failed:
        print(f"  FAILED: {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="omni-nginx-") as folder:
        try:
            start_nginx(folder)
            code = main()
        finally:
            stop_nginx()
    sys.exit(code)
