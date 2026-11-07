"""The HTTP surface. Every request passes the gate in `Gate` before anything else runs."""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import time
from contextlib import asynccontextmanager
from urllib.parse import unquote

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.requests import ClientDisconnect

from gateway.access import RateLimiter, real_client
from gateway.config import Settings
from gateway.jobs import FORMAT_ID, JOB_ID, Job, JobStore, new_job_id
from gateway.link import WorkerLink, write_all
from gateway.responses import DOWNLOAD_CSP, Page, content_disposition, output_name, sanitize_filename, security_headers

log = logging.getLogger("omniconvert.gateway")

DOWNLOAD_CHUNK = 256 * 1024
HOUSEKEEPING_SECONDS = 5
GONE = "This file is no longer on the server. Files are deleted once they're downloaded, or after a few minutes if nobody does."
MEMORY_FULL = "The server's memory is full right now. Try again once other files have been downloaded."


def error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status, headers={"cache-control": "no-store"})


class Gate:
    """Pure ASGI, so uploads and downloads stream straight through it instead of being buffered.

    In order: undo an unstripped subpath, then check the IP allow-list, the rate limit, the origin of
    the request and the optional token.
    """

    def __init__(self, app, settings: Settings):
        self.app = app
        self.settings = settings
        self.limiter = RateLimiter(settings.rate_limit_per_minute)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        base = self.settings.base_path
        if base and (scope["path"] == base or scope["path"].startswith(base + "/")):
            scope = {**scope, "path": scope["path"][len(base):] or "/"}

        headers = {k: v for k, v in scope["headers"]}
        peer = scope["client"][0] if scope.get("client") else None

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                existing = message.get("headers", [])
                has_csp = any(k.lower() == b"content-security-policy" for k, _ in existing)
                message = {**message, "headers": [*existing, *security_headers(has_csp)]}
            await send(message)

        async def refuse(status: int, text: str):
            await error(status, text)(scope, receive, send_with_headers)

        x_real_ip = headers.get(b"x-real-ip", b"").decode("latin-1") or None
        client = real_client(peer, x_real_ip, self.settings.trusted_proxies)

        if not self.settings.allow.allows(client):
            log.warning("refused client %s: not on the allow-list", client)
            await refuse(403, "This address is not allowed to use the server. Ask whoever runs it to add your IP address.")
            return
        if not self.limiter.allow(client or "unknown"):
            await refuse(429, "Too many requests at once. Wait a moment and try again.")
            return

        path = scope["path"]
        if path.startswith("/api/"):
            # A page on another site can't drive this API from an allowed visitor's browser.
            site = headers.get(b"sec-fetch-site", b"").decode("latin-1")
            if site and site not in ("same-origin", "none"):
                await refuse(403, "Requests from other sites are not accepted.")
                return
            # The download link itself is the credential: an unguessable job id. A plain link
            # can't carry a header, and a browser would otherwise hold the whole file in memory.
            if self.settings.api_token and not path.endswith("/download"):
                sent = headers.get(b"authorization", b"").decode("latin-1")
                if not hmac.compare_digest(sent.encode(), f"Bearer {self.settings.api_token}".encode()):
                    await refuse(401, "This server needs an access token.")
                    return

        scope = {**scope, "omniconvert_client": client}
        await self.app(scope, receive, send_with_headers)


def create_app(settings: Settings) -> FastAPI:
    store = JobStore(settings.job_dir, settings.retention_minutes * 60)
    link = WorkerLink(store, settings)
    page = Page(settings.page_path)

    async def housekeeping():
        while True:
            try:
                store.expire()
                store.touch_alive()
            except Exception:  # a bad pass must never stop expiry
                log.exception("housekeeping failed")
            await asyncio.sleep(HOUSEKEEPING_SECONDS)

    @asynccontextmanager
    async def lifespan(_app):
        store.prepare()
        await link.start()
        task = asyncio.create_task(housekeeping())
        yield
        task.cancel()
        await link.stop()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.store = store
    app.state.link = link

    @app.get("/")
    async def index():
        return Response(page.html, media_type="text/html; charset=utf-8", headers={
            "cache-control": "no-store",
            "content-security-policy": page.csp,
        })

    @app.get("/api/formats")
    async def formats():
        catalog = link.catalog
        return JSONResponse({
            "limits": {
                "max_upload_bytes": settings.max_upload_bytes,
                "retention_minutes": settings.retention_minutes,
            },
            "server": {"worker_online": link.online, "isolation": link.isolation},
            "formats": catalog.public_formats() if catalog else [],
            "families": catalog.families if catalog else {},
        }, headers={"cache-control": "no-store"})

    @app.get("/api/routes")
    async def routes(request: Request):
        source = request.query_params.get("from", "unknown")
        if not FORMAT_ID.match(source):
            return error(400, "That format name isn't valid.")
        catalog = link.catalog
        if catalog is None:
            return error(503, "The converter is starting up. Wait a minute and try again.")
        found = await asyncio.to_thread(link.routes, source if source in catalog.formats else "unknown")
        return JSONResponse({"routes": found}, headers={"cache-control": "no-store"})

    @app.put("/api/jobs")
    async def create_job(request: Request):
        client = request.scope.get("omniconvert_client") or "unknown"
        to = request.query_params.get("to", "")
        hint = request.query_params.get("from") or None
        declared = request.headers.get("content-length")
        try:
            declared_size = int(declared) if declared is not None else 0
        except ValueError:
            return error(400, "The upload's size header isn't valid.")
        if declared_size > settings.max_upload_bytes:
            return error(413, f"This file is over the {settings.max_upload_bytes // 1024**2} MiB upload limit.")
        if store.free_bytes() < declared_size + 1:
            return error(507, MEMORY_FULL)

        catalog = link.catalog
        if catalog is None or not link.online:
            return error(503, "The converter is busy or restarting. Wait a minute and try again.")
        # The target is only ever matched against the catalog, never used as a path or an argument.
        if not FORMAT_ID.match(to) or not catalog.is_writable(to):
            return error(400, "That output format isn't available.")
        if hint is not None and not (FORMAT_ID.match(hint) and hint in catalog.formats):
            hint = None

        if store.waiting() >= settings.max_queue:
            return error(503, "The line is full right now. Try again once some files have finished.")
        if store.jobs_for(client) >= settings.max_jobs_per_ip:
            return error(429, "You have too many files in progress. Wait for some to finish.")

        # In a header, not the URL: web servers write URLs to their access logs, on disk.
        filename = sanitize_filename(unquote(request.headers.get("x-filename", ""), errors="replace"))
        if hint is None:
            hint = catalog.format_for_name(filename)
        # Refused before a byte is read. The page only offers what's possible, so this is a request
        # for something it was never offered. The worker checks again against the real contents.
        source = hint if hint in catalog.formats else "unknown"
        if not any(route["to"] == to for route in await asyncio.to_thread(link.routes, source)):
            if source == "unknown":
                return error(400, "This file's type wasn't recognised, so it can only be packaged, encoded, or have its details pulled out.")
            return error(400, f"{source.upper()} files can't be converted to {to.upper()}. Choose another format.")

        job_id = new_job_id()
        try:
            fd = store.new_file()
        except OSError:
            return error(507, MEMORY_FULL)
        store.uploading[job_id] = client
        total = 0
        keep = False
        try:
            async for chunk in request.stream():
                if not chunk:
                    continue
                total += len(chunk)
                if total > settings.max_upload_bytes:
                    return error(413, f"This file is over the {settings.max_upload_bytes // 1024**2} MiB upload limit.")
                await asyncio.to_thread(write_all, fd, chunk)
            if total == 0:
                return error(400, "That file is empty.")
            keep = True
        except ClientDisconnect:
            return Response(status_code=499)
        except OSError:
            log.warning("an upload stopped because the job store is full")
            return error(507, MEMORY_FULL)
        finally:
            store.uploading.pop(job_id, None)
            if not keep:
                os.close(fd)

        store.add(Job(id=job_id, filename=filename, to=to, hint=hint, owner=client, size=total, fd=fd))
        return JSONResponse({"id": job_id, "queue_position": len(store.queue)}, status_code=202, headers={"cache-control": "no-store"})

    def find(job_id: str) -> Job | None:
        return store.jobs.get(job_id) if JOB_ID.match(job_id) else None

    def job_view(job: Job) -> dict:
        if job.state == "queued":
            return {"id": job.id, "state": "queued", "queue_position": store.position(job)}
        view = {"id": job.id, "state": job.state, "step": job.step, "route": job.route, "detected": job.detected}
        if job.state == "failed":
            view["error"] = job.error or "This conversion failed."
        if job.state == "done":
            view["expires_in"] = max(1, int((job.expires_at or 0) - time.monotonic()))
            view["output"] = {"name": output_name(job.filename, job.out_ext or job.to, job.bundle), "size": job.size}
        return view

    @app.get("/api/jobs/{job_id}")
    async def job_status(job_id: str):
        job = find(job_id)
        if job is None:
            return error(404, GONE)
        return JSONResponse(job_view(job), headers={"cache-control": "no-store"})

    @app.get("/api/jobs/{job_id}/download")
    async def job_download(job_id: str):
        job = find(job_id)
        if job is None or job.state != "done" or job.fd is None:
            return error(404, GONE)

        async def body():
            source = job.fd
            if source is None:  # deleted between the check above and the first byte
                return
            # Its own descriptor, so a delete during this download can't pull the file out from under it.
            fd = os.dup(source)
            offset = 0
            try:
                while offset < job.size:
                    chunk = await asyncio.to_thread(os.pread, fd, DOWNLOAD_CHUNK, offset)
                    if not chunk:
                        return
                    offset += len(chunk)
                    yield chunk
                # All of it went out, so it's the person's now and nothing is kept. A download that
                # breaks partway never gets here, so it can be tried again until the result expires.
                store.drop(job)
            finally:
                os.close(fd)

        # Never rendered in the browser. Behind a subpath this shares an origin with everything
        # else on the domain, so an HTML or SVG result shown inline could attack those apps.
        return StreamingResponse(body(), media_type="application/octet-stream", headers={
            "content-disposition": content_disposition(output_name(job.filename, job.out_ext or job.to, job.bundle)),
            "content-length": str(job.size),
            "cache-control": "no-store",
            "content-security-policy": DOWNLOAD_CSP,
        })

    @app.delete("/api/jobs/{job_id}")
    async def job_delete(job_id: str):
        job = find(job_id)
        if job is None:
            return error(404, GONE)
        link.cancel(job)
        store.drop(job)
        return Response(status_code=204, headers={"cache-control": "no-store"})

    @app.exception_handler(404)
    async def not_found(_request, _exc):
        return error(404, "Not found.")

    @app.exception_handler(405)
    async def bad_method(_request, _exc):
        return error(405, "Method not allowed.")

    return Gate(app, settings)
