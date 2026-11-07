"""The real page in a real browser, through the real nginx snippet, under a subpath.

    SAMPLES=dir PYTHONPATH=. python tests/integration/ui_e2e.py [screenshot dir]

Needs Playwright and a Chromium, and ALLOWED_IPS must include 127.0.0.1. The strict CSP is enforced here
as a browser would, so a wrong script hash or a blocked request shows up as the page failing or as a
console error.

The browser is allowed by its real address, with no extra header. Playwright's extra_http_headers
aren't sent on the requests Chromium makes for downloads, so a test client chosen by header would see
its downloads refused, which looks just like a broken download.
"""

from __future__ import annotations

import io
import os
import re
import sys
import tempfile
import zipfile
from pathlib import Path

from playwright.sync_api import sync_playwright

from tests.integration.nginx_e2e import PORT, start_nginx, stop_nginx

SAMPLES = Path(os.environ["SAMPLES"])
BASE = f"http://127.0.0.1:{PORT}/convert/"
CHROMIUM = os.environ.get("CHROMIUM", "/usr/bin/chromium")
results = []


def record(name, ok, detail=""):
    results.append((name, ok))
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}: {detail}", flush=True)


def trip(page, name):
    return page.locator(".trip", has=page.locator(".name", has_text=name))


def run(pw, shots: Path | None):
    browser = pw.chromium.launch(executable_path=CHROMIUM, args=["--no-sandbox"])
    problems: list[str] = []

    def on_console(message):
        if message.type not in ("error", "warning"):
            return
        where = message.location.get("url", "")
        # After a download the page asks for the job's status and is told 404, which is how it learns the
        # file is gone. Chromium logs any 404. Only that exact request is excused.
        if "status of 404" in message.text and re.search(r"/api/jobs/[A-Za-z0-9_-]{43}$", where):
            return
        problems.append(f"{message.text} [{where}]")

    def watch(page):
        page.on("console", on_console)
        page.on("pageerror", lambda e: problems.append(str(e)))
        # A poll cut off by a reload is expected, and so is the navigation that turns into a download.
        page.on("requestfailed", lambda r: problems.append(f"request failed: {r.method} {r.url[-40:]} {r.failure}") if r.failure != "net::ERR_ABORTED" else None)

    outside: list[str] = []
    ctx = browser.new_context(viewport={"width": 1200, "height": 900}, accept_downloads=True, color_scheme="light")
    page = ctx.new_page()
    watch(page)
    page.on("request", lambda r: outside.append(r.url) if not r.url.startswith((f"http://127.0.0.1:{PORT}/", "data:", "blob:")) else None)

    print("Page load")
    response = page.goto(BASE)
    record("Page loads", response.status == 200, str(response.status))
    page.wait_for_selector("#facts .online", timeout=15000)
    record("Script ran under the CSP", page.locator("#drop-limits").inner_text() != "", page.locator("#drop-limits").inner_text()[:80])
    record("Reports the converter online", "Converter online" in page.locator("#facts").inner_text(), page.locator("#facts").inner_text().replace("\n", " "))
    record("No preview notice on a real server", page.locator("#preview-note").is_hidden())
    record("No example rows on a real server", page.locator(".tag").count() == 0)
    record("Empty state is shown", page.locator(".empty").is_visible(), page.locator(".empty").inner_text()[:60])
    fonts = page.evaluate('[...document.fonts].filter(f => f.status === "loaded").map(f => f.family).join(", ")')
    record("Embedded fonts load under the CSP", "Overpass" in fonts, fonts)
    record("Footer shows the real format count", "formats installed" in page.locator("#foot").inner_text(), page.locator("#foot").inner_text()[:50])
    if shots:
        page.screenshot(path=str(shots / "live-empty.png"), full_page=True)

    print("One conversion, start to finish")
    page.set_input_files("#file-input", str(SAMPLES / "sample.png"))
    page.wait_for_selector(".picker .opt", timeout=15000)
    record("Format list comes from the server", page.locator(".picker-note").is_visible(), page.locator(".picker-note").inner_text())
    record("The signature is read in the browser", "89 50 4E 47" in trip(page, "sample.png").inner_text())
    page.fill(".picker input", "webp")
    page.keyboard.press("Enter")
    page.wait_for_selector(".route-box")
    record("Picking a format shows the route", "WEBP" in page.locator(".route-box").inner_text().upper(), page.locator(".route-box").inner_text().replace("\n", " ")[:60])
    page.locator('[data-focus-key="convert"]').click()
    page.wait_for_selector('.pill[data-state="done"]', timeout=200000)
    record("Job reaches Done", True, trip(page, "sample.png").locator(".pill").inner_text())
    if shots:
        page.screenshot(path=str(shots / "live-done.png"), full_page=True)

    print("A reload keeps a result nobody has downloaded")
    page.reload()
    page.wait_for_selector(".trip .name", timeout=15000)
    record("Finished job is still listed", trip(page, "sample.png").count() == 1)
    record("Still offers the download", page.locator('a[data-focus-key="download"]').count() == 1)

    print("Downloading deletes it")
    with page.expect_download(timeout=30000) as info:
        page.locator('a[data-focus-key="download"]').click()
    download = info.value
    path = download.path()
    data = Path(path).read_bytes()
    record("Download arrives with the right name", download.suggested_filename == "sample.webp", download.suggested_filename)
    record("Download is really a WebP", data[:4] == b"RIFF" and data[8:12] == b"WEBP", f"{len(data)} bytes")
    page.wait_for_selector('.pill:has-text("Downloaded")', timeout=15000)
    record("The page says it's gone from the server", "Nothing is left on the server" in trip(page, "sample.png").inner_text(), trip(page, "sample.png").locator(".trip-note").inner_text())
    record("It no longer offers a dead link", page.locator('a[data-focus-key="download"]').count() == 0)
    page.locator('[data-focus-key="remove"]').click()

    print("Deleting by hand")
    page.set_input_files("#file-input", str(SAMPLES / "sample.png"))
    page.wait_for_selector(".picker .opt", timeout=15000)
    page.fill(".picker input", "webp")
    page.locator('.opt:has(.ext:text-is("webp"))').first.click()
    page.wait_for_selector(".route-box")
    page.locator('[data-focus-key="convert"]').click()
    page.wait_for_selector('.pill[data-state="done"]', timeout=200000)
    page.locator('[data-focus-key="delete"]').click()
    page.wait_for_selector('.pill:has-text("Deleted")', timeout=15000)
    record("Delete works", True)
    page.locator('[data-focus-key="remove"]').click()

    print("A chained route, with a Greek name")
    with tempfile.TemporaryDirectory() as folder:
        named = Path(folder) / "Τιμολόγιο 2026.md"
        named.write_bytes((SAMPLES / "sample.md").read_bytes())
        page.set_input_files("#file-input", str(named))
        page.wait_for_selector(".picker .opt", timeout=15000)
        page.fill(".picker input", "pdf")
        page.locator('.opt:has(.ext:text-is("pdf"))').first.click()
        page.wait_for_selector(".route-box")
        stops = page.locator(".route .stop").count()
        record("Shows a route with more than one step", stops >= 3, page.locator(".route-box").inner_text().replace("\n", " ")[:80])
        page.locator('[data-focus-key="convert"]').click()
        page.wait_for_selector('.pill[data-state="done"]', timeout=200000)
        with page.expect_download(timeout=30000) as info:
            page.locator('a[data-focus-key="download"]').click()
        record("Greek file name survives", info.value.suggested_filename == "Τιμολόγιο 2026.pdf", info.value.suggested_filename)
        page.wait_for_selector('.pill:has-text("Downloaded")', timeout=15000)
        page.locator('[data-focus-key="remove"]').click()

        print("A file with the wrong name")
        fake = Path(folder) / "photo.jpg"
        fake.write_bytes((SAMPLES / "sample.png").read_bytes())
        page.set_input_files("#file-input", str(fake))
        page.wait_for_selector(".trip-note.warn")
        record("Warns that it's really a PNG", "PNG" in page.locator(".trip-note.warn").inner_text(), page.locator(".trip-note.warn").inner_text())
        page.locator('[data-focus-key="remove"]').click()

        print("A hostile file name can't become markup")
        hostile = Path(folder) / "<img src=x onerror=alert(1)>.txt"
        hostile.write_bytes(b"hello")
        page.set_input_files("#file-input", str(hostile))
        page.wait_for_selector(".trip .name")
        record("Shown as text", page.locator(".trip .name img").count() == 0 and "<img" in page.locator(".trip .name").first.inner_text(), page.locator(".trip .name").first.inner_text()[:50])
        page.locator('[data-focus-key="remove"]').click()

        print("A closed format")
        closed = Path(folder) / "bracket.sldprt"
        closed.write_bytes((SAMPLES / "sample.sldprt").read_bytes())
        page.set_input_files("#file-input", str(closed))
        page.wait_for_selector(".picker .opt", timeout=15000)
        page.wait_for_selector('.trip-note:has-text("Nothing here can open")', timeout=15000)
        grades = set(page.locator(".picker .grade").all_inner_texts())
        record("Only honest options are offered", grades <= {"PACKAGE", "EXTRACT", "Package", "Extract"}, str(sorted(grades)))
        page.fill(".picker input", "zip")
        page.keyboard.press("Enter")
        page.locator('[data-focus-key="convert"]').click()
        page.wait_for_selector('.pill[data-state="done"]', timeout=200000)
        with page.expect_download(timeout=30000) as info:
            page.locator('a[data-focus-key="download"]').click()
        data = Path(info.value.path()).read_bytes()
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            record("Packaged unchanged", archive.read(archive.namelist()[0]) == (SAMPLES / "sample.sldprt").read_bytes(), str(archive.namelist()))
        page.wait_for_selector('.pill:has-text("Downloaded")', timeout=15000)
        page.locator('[data-focus-key="remove"]').click()

    print("Nothing went wrong along the way")
    real = [p for p in problems if "favicon" not in p]
    record("No CSP violations or script errors", not real, " || ".join(real)[:700])
    record("No request left this server", not outside, "; ".join(outside)[:200])
    ctx.close()

    print("Phone, dark")
    ctx = browser.new_context(viewport={"width": 400, "height": 860}, color_scheme="dark")
    page = ctx.new_page()
    page.goto(BASE)
    page.wait_for_selector("#facts .online", timeout=15000)
    page.set_input_files("#file-input", str(SAMPLES / "sample.png"))
    page.wait_for_selector(".picker .opt", timeout=15000)
    over = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
    record("No sideways scrolling at phone width", over == 0, f"{over}px")
    if shots:
        page.screenshot(path=str(shots / "live-phone-dark.png"), full_page=True)
    ctx.close()

    print("A refused address")
    ctx = browser.new_context(extra_http_headers={"X-Test-Client": "198.51.100.9"})
    page = ctx.new_page()
    response = page.goto(BASE)
    record("Gets a 403, not the app", response.status == 403, page.locator("body").inner_text()[:70])
    ctx.close()
    browser.close()


def main():
    shots = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    with tempfile.TemporaryDirectory(prefix="omni-nginx-") as folder:
        try:
            start_nginx(folder)
            with sync_playwright() as pw:
                run(pw, shots)
        finally:
            stop_nginx()
    failed = [name for name, ok in results if not ok]
    print(f"\n{len(results) - len(failed)} of {len(results)} checks passed.")
    for name in failed:
        print(f"  FAILED: {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
