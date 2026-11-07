# OmniConvert

A self-hosted file converter for a Raspberry Pi 4 (8 GB) behind your own nginx. Drop in a file, choose a format, and download the result. Nothing is kept: files are held in memory only, never written to disk, and deleted the moment you download them.

Each conversion is shown as a route. When no single tool can do the job, it chains several: `.md` to `.pdf` runs pandoc and then WeasyPrint.

## What it can and can't convert

**It can't convert every file in the world to every other file in the world, and nothing can.**

- Closed formats with no open converter (SolidWorks `.sldprt`, Revit `.rvt`, InDesign `.indd`) and DRM-locked files can't be properly read.
- Some pairs mean nothing, such as `mp3` to `xlsx`.

**Each file is only offered formats of its own kind.** When you add a file, the page lists exactly what it can become, and nothing else can be chosen. The server refuses anything else before reading the upload, and the worker checks again against the file's real contents.

| Kind | Families | For example |
|---|---|---|
| Documents | documents, spreadsheets, slides, ebooks, mail | DOCX → PDF, EPUB → MOBI, XLSX → PDF |
| Tables and data | spreadsheets, data | CSV → XLSX, JSON → CSV, XLSX → JSON |
| Pictures | images, camera RAW, vector, diagrams, DICOM | PNG → WEBP, SVG → PNG, JPG → SVG (traced) |
| Sound and video | audio, video | MP4 → MKV, MP3 → FLAC, MP4 → MP3 |
| Subtitles, archives, 3D models, CAD, maps, fonts, science | each on its own | SRT → VTT, ISO → ZIP, STL → OBJ, DWG → DXF |

Two kinds of conversion cross over, by choice: **video → pictures** (an animated GIF, or frames in a zip), and **tracing a picture** into SVG outlines. Everything else that changes what a file is isn't offered, such as a PDF's pages as pictures, a picture as a PDF, reading a document aloud, reading text out of a picture (OCR), or a drawing turned into a 3D model.

A route has to stay inside the source's kind at every step, not just at the end, so a detour like MP3 → MKV → GIF can't sneak through. Each conversion that failed when every route was run for real is blocked too (`worker/known_bad_pairs.json`).

Every conversion is labelled honestly:

| Label | Meaning |
|---|---|
| **Exact** | Everything in your file comes across |
| **Lossy** | Some quality or layout is lost, usually for a smaller or simpler file |
| **Extract** | Only part comes across, such as the text, the sound or the file's details |
| **Package** | Your file is wrapped unchanged, for formats nothing here can open |

Any file, even an unknown one, can become an archive, base64 or a hex dump, or have its readable strings and details pulled out. A file's contents decide what it's treated as, not its name: a PNG named `.jpg` is converted as a PNG, and a text file named `.mp4` as text. Each result's type is checked before it's offered, and a file that's plainly the wrong format is discarded, not renamed to look right.

The catalog lists 915 formats. Every conversion it offers from 165 test files was run for real: 9,050 of them. 8,967 passed, across 415 target formats, and 20 more correctly reported that a file had no text to extract. The 63 that failed even after a retry are blocked, so they're no longer offered. [docs/COVERAGE.md](docs/COVERAGE.md) has the details. Formats with no test file are offered because the tool that handles them says it supports them, within the kind rule. A pass means a real file of the right type came out. Nobody looked at each result.

**Not included:**
- FreeCAD and Blender, which together add about 3.7 GB. Assimp can read some STEP and older `.blend` files, but IGES isn't supported, and complex CAD files may fail. Closed CAD formats have no route at all.
- Speech-to-text. Text-to-speech (eSpeak NG) and OCR (Tesseract, OCRmyPDF) are installed but not offered, because they turn a file into a different kind of thing.

Video encoding is software-only, because hardware encoding would need device access in the container. Expect it to be slow on a Pi 4. Raise `JOB_TIMEOUT_SECONDS` if long videos get stopped.

## Install on the Pi

You need Docker 28 or newer with Compose v2, the nginx you already run, and about 15 GB free to build the images (they end up at about 6.7 GB). Use 64-bit Raspberry Pi OS or Debian. Nothing here needs `sudo` except your nginx, and possibly one kernel setting.

**This hasn't been run on a real Pi yet.** All the tests so far ran on an x86_64 PC, with Landlock on and off. Every package and Python library is confirmed to exist for arm64, but the arm64 build, its speed and its memory use are unproven until you run steps 1 to 4.

```bash
# 1. Copy this folder to the Pi, then:
cd omniconvert
bash scripts/lock-permissions.sh                  # makes the folder private to you; copying doesn't keep this
cp .env.example .env && chmod 600 .env
nano .env                                         # set ALLOWED_IPS

# 2. Check the machine. It changes nothing, and tells you what to fix.
scripts/preflight.sh
```

Preflight will probably report two things on Raspberry Pi OS:

- **Memory limits don't work.** Raspberry Pi OS can ship with the memory cgroup off, and Docker then ignores memory limits with only a warning. The worker refuses to start in that state, on purpose. To fix it, add `cgroup_enable=memory cgroup_memory=1` to the single line in `/boot/firmware/cmdline.txt` and reboot.
- **No Landlock.** That's expected on Raspberry Pi OS; see [Isolation](#isolation).

```bash
# 3. Build and start. The worker image takes a long time to build on a Pi, so leave it running.
docker compose build
docker compose up -d

# 4. Prove the lockdown and the limits are really in force here.
scripts/verify-hardening.sh
```

**5. Add the nginx snippet.** Paste `deploy/host-nginx-subpath.conf` into your HTTPS server block and change `/convert/` to the subpath you want, then:

```bash
sudo nginx -t && sudo systemctl reload nginx
```

Open `https://your-domain/convert/`.

If `client_max_body_size` in the snippet doesn't match `MAX_UPLOAD_BYTES`, large uploads will fail with a 413.

## Settings

All in `.env`, and `.env.example` explains each one. Apply a change with `docker compose up -d`; `docker compose restart` keeps the old values.

| Setting | Default | |
|---|---|---|
| `ALLOWED_IPS` | required | Comma-separated addresses or CIDR ranges. `all` must be written out |
| `HOST_PORT` | `8090` | The port on `127.0.0.1` your nginx proxies to |
| `API_TOKEN` | off | Also require a token on top of the allowed IP |
| `BASE_PATH` | empty | Only if your nginx passes the subpath through instead of stripping it |
| `MAX_UPLOAD_BYTES` | 1 GiB | |
| `MAX_OUTPUT_BYTES` | 1.5 GiB | For any single result |
| `JOB_TIMEOUT_SECONDS` | 1800 | |
| `RETENTION_MINUTES` | 10 | How long a result nobody downloads is held. Downloading deletes it at once |
| `LOG_CONVERTER_OUTPUT` | off | Puts what converters print in the worker's log, to see why a file fails. That text can quote the file, so turn it off again |

### Memory decides how large a file can be

Everything is in RAM, so a file has to fit in RAM, and at its peak a conversion needs room for the input, the result, and whatever the converter uses while working. These are the defaults, which leave about 1.7 GB of a Pi 4's 8 GB for everything else when the service is at its busiest:

| | Holds | Setting | Default |
|---|---|---|---|
| Gateway's job store | Waiting uploads and finished results | `JOB_STORE_SIZE` | 2 GB |
| Worker's work folder | The job in progress: input, temporary files, result | `WORKER_WORK_SIZE` | 3 GB |
| Worker's `/tmp` | For libraries that ignore `TMPDIR` | `WORKER_TMP_SIZE` | 256 MB |

A store that's full refuses new uploads with a clear message and frees up as results are downloaded or expire. A job that runs out of room fails on its own and takes nothing else with it. To convert larger files, raise these, `MAX_UPLOAD_BYTES`, `MAX_OUTPUT_BYTES` and `client_max_body_size` together. Each container refuses to start if its files could outgrow its memory limit, because then a large file would get it killed instead of refused.

### Docker limits

Defaults suit a Pi 4 with 8 GB, leaving room for your other apps.

| Limit | Gateway | Worker | Setting |
|---|---|---|---|
| CPU | 0.5 | 3.0 | `GATEWAY_CPUS`, `WORKER_CPUS` |
| Memory | 2.3 GB | 4 GB | `GATEWAY_MEMORY`, `WORKER_MEMORY`. Files in RAM count toward these |
| Swap | none | none | Fixed, so files in memory can't reach a disk |
| Processes | 64 | 512 | `GATEWAY_PIDS`, `WORKER_PIDS` |
| Open files | 4096 | 4096 | |
| Core dumps | off | off | |
| CPU priority when contended | 512 | 256 | The worker gives way to your other apps |
| Killed first if the host runs out of memory | no | yes | |
| Logs | 10 MB × 3 | 10 MB × 3 | |

Idle, they use far less than these limits: about 70 MB between them on x86_64. The limits are only reached while files are actually held.

Each converter also runs with its own limits inside the worker: a time limit, a file size limit, a process limit below the container's, no core dumps, and a raised OOM score. If memory runs out, the converter dies and the supervisor survives.

## Security

A converter feeds untrusted files to very complex parsers, so this assumes one will eventually be compromised and limits what that can reach. The closest existing project has had [three path-traversal CVEs](https://osv.dev/vulnerability/CVE-2026-59976) in about a year, so patching alone isn't enough. A Trivy scan on 2026-11-07 found no HIGH or CRITICAL issues with a fix available, but 596 in the worker image with no fix yet from Debian, 18 of them CRITICAL (in Pillow, Assimp, OpenEXR, libhts and others), and 47 in the gateway image, none of them CRITICAL. That's normal for this many parsers, and it's what the isolation below is for.

```
browser -> host nginx (TLS, subpath) -> 127.0.0.1:8090 -> gateway <- one Unix socket -> worker
                                                          holds the queue               holds one job
                                                          and results, in RAM           NO network at all
                                                          parses no files               all the converters
```

### Nothing is kept

| | |
|---|---|
| **Where files are** | In RAM, in mounts that vanish with their container. The memory limits forbid swap, so they can't reach a disk that way either |
| **An upload** | An unnamed file with no path, freed the moment the worker has it |
| **A result** | An unnamed file, freed the moment all of it has been downloaded. A download that breaks partway leaves it there to try again |
| **A result nobody collects** | Deleted after `RETENTION_MINUTES` (10) |
| **A restart or power cut** | Everything is gone. There's nothing to clean up |
| **Logs** | The one thing that does go to disk, so they hold no file names and no converter output. The nginx snippet turns off the access log, and file names travel in a header rather than the URL so they couldn't land in one anyway |

The tests look for this directly: they convert a file with a unique marker in its name and its contents, then search Docker's own folders on this machine's disk for those markers.

**The one thing shared: why there's still a `volumes:` line.** The containers have to pass files somehow, and the worker has no network, so that's one Unix socket, which has to live on a mount both can see. It's 64 KB of RAM holding that socket and nothing else: no file ever passes through it as a file, it's only a pipe. The worker mounts it read-only. The alternative is giving the worker a network to talk over, which would be a far worse trade.

### What each container can do

**Worker**
- `network_mode: none`, and its syscall filter allows no network sockets of any kind, so a compromised converter can't send anything anywhere or reach your LAN.
- Holds exactly one job. The queue and every finished result stay in the gateway, so a compromised converter has no other file to read, change or delete, and no job ID to steal. Each file arrives with only its target format, never its name, its owner or its ID.
- Everything it can write to is wiped and every process killed between jobs, before the next file arrives.
- Holds no secrets. `API_TOKEN` is passed only to the gateway.
- Converters can't attach to its two long-lived processes or read their memory, so nothing can hide there between jobs.
- If the supervisor is killed or frozen, or the gateway hangs up, PID 1 exits and the kernel kills everything in the container. So a converter can't remove the supervisor and connect in its place to be handed the next person's file. That's tested.

**Gateway**
- Can't start a network connection at all, only answer one: `connect` and TCP Fast Open are denied, as are UDP and ping. It holds everyone's files while they wait, so this is what stops a compromised gateway from sending them anywhere.
- Its image has Python and nothing else to run: no shell, Perl, `awk`, `sed`, `find` or `tar`.
- Parses no user files.
- Treats the worker as hostile: every message from it has its size checked before it's read and is strictly validated, and anything out of order ends the session. No file from the worker is ever opened by name, so there's nothing to plant a symlink in.

**Both**
- Non-root (`10001` and `10002`) from the first instruction, with no root setup step, each in its own group, sharing no user or group with the other
- Read-only root filesystem, and `noexec,nosuid,nodev` on everything writable
- `cap_drop: ALL` and `no-new-privileges`
- No shared memory folder or message queues, so a job has nowhere to leave something for the next
- A syscall filter for each container, narrower than Docker's default ([`scripts/make-seccomp.py`](scripts/make-seccomp.py) lists every removal and why). As well as the above it removes the 32-bit syscall layer, debugging another process, netlink and raw packet sockets, memory with no file behind it, and shared memory that outlives a process
- Setuid bits stripped, no folder left world-writable, and package managers removed
- Base images pinned by digest, Python packages pinned by hash, and downloads checked against checksums
- The Docker socket is never mounted

### Who can touch what

| Path | Owner : mode | Gateway | Worker |
|---|---|---|---|
| This folder on the host | you : dirs `700`, files `600` | none | none |
| `.env` | you : `600` | via compose only | none |
| `/app` in the images | `root`, dirs `555`, files `444` | read | read |
| `/jobs` (RAM) | `10001` : `700` | read/write | doesn't exist |
| `/work`, `/tmp` (RAM) | `10002` : `700` | don't exist | read/write |
| `/run/omniconvert` (RAM, 64 KB) | `10001:10002` : `2750` | read/write | enter, read-only mount |
| `/run/omniconvert/link.sock` | `10001:10002` : `660` | listens | connects |

Those four RAM mounts are all either container can write to, and `scripts/verify-hardening.sh` checks that from inside each one. There are no host folders mounted at all.

**Access**
- Published on `127.0.0.1` only. A refused client gets `403` before anything else runs.
- `X-Real-IP` is trusted only from the fixed Docker gateway address, and the snippet overwrites whatever the client sent. `X-Forwarded-For` is never read. A request with no header (such as `curl` straight at the port) is judged by the proxy's own address, so it's refused. To test by hand: `curl -H 'X-Real-IP: <allowed ip>' http://127.0.0.1:8090/`.
- Per-client rate limit and job limit.
- Job IDs are 256 random bits. Knowing a job's ID is what lets you download it, so the worker is never told one.
- Requests from other sites are refused, and there's no CORS and no cookies.

**Web**
- Results are always downloads (`attachment`, `application/octet-stream`, `nosniff`, `CSP: sandbox`). On a subpath you share an origin with your other apps, so an HTML or SVG result shown inline could attack them.
- The page's CSP names its exact script and styles by hash. It loads nothing from anywhere else.
- Uploads are a raw streamed `PUT`, so there's no multipart parser to attack.

**Per-format guardrails** catch the known tricks where a file format's own features read local files or run commands:

| Tool | Guardrail |
|---|---|
| ffmpeg | Only `file` and `pipe` protocols. Playlists are stopped at detection, and their demuxers are refused even if a file gets past that |
| ImageMagick | No MSL, MVG, URL or `@file`, plus resource limits. The policy is a second layer only, since it has been bypassed before |
| Ghostscript | `-dSAFER` |
| LibreOffice | Private profile per job, macros disabled, link updates off |
| pandoc, TeX | `--sandbox`; no shell escape and `openin_any=p` |
| WeasyPrint | Fetches nothing outside the page |
| GDAL | Input driver pinned; VRT refused |
| qemu-img | Images that refer to other files are refused |
| Archives | Measured while extracting (headers can lie), with size, ratio and count limits. Links that leave the tree are removed |
| Data files | Pickle is never loaded; safe YAML loader; `defusedxml` |

### Isolation

The worker runs one job at a time and holds nothing else, on every kernel, so a job has no other file to reach. Landlock adds a second wall inside the container, and the worker detects it at startup:

| Kernel | What happens |
|---|---|
| Raspberry Pi OS | Everything above |
| Debian or Ubuntu kernel (has Landlock) | The same, and each converter is also locked to its own folder and a short list of read-only system folders, and can't signal the supervisor |

Landlock is a kernel sandbox that needs no privileges. It isn't in the Raspberry Pi 6.12 kernel config, and it can't be turned on without building a custom kernel.

**What's left on Raspberry Pi OS.** This only applies if a malicious file gains code execution inside a converter, which takes an unpatched bug being exploited. That code runs as the worker user, for as long as its job runs. Without Landlock it can read any file in the image that isn't private to root, which is all public software, and it can stop the worker, which restarts by itself and fails that job. It can't reach any network, your LAN or the host, see any other job or file name, read the gateway's settings, change the system files, attach to the supervisor to watch later jobs, or stay behind once its job ends.

**What's left everywhere.** A converter sees the one file it was given, so an exploit can still alter *that* result. And all of this rests on the Linux kernel: a kernel bug reached through the syscalls still allowed could break out of any container. The filters shrink that surface; they can't remove it. gVisor is the next step if you want more.

Optional extras if you want to go further:
- Turn on AppArmor by adding `lsm=apparmor` to the kernel command line and installing the `apparmor` package, so Docker's default profile applies.
- Run the worker under gVisor. It isn't tested here.

## Keeping it updated

```bash
docker compose build --no-cache && docker compose up -d    # weekly, to pick up Debian security fixes
scripts/scan.sh                                            # fails on fixable HIGH or CRITICAL issues
```

`--no-cache` matters: the base images are pinned, and a plain rebuild reuses the cached install step and changes nothing. With it, the install step runs again and `apt-get upgrade` brings everything up to date. It takes as long as the first build, so on a Pi, leave it running. Fixes for these parsers really do appear this often: a scan one day after the first build found six new fixes for ImageMagick.

To move to a newer base image altogether, update the digests in the Dockerfiles with `docker buildx imagetools inspect debian:trixie-slim`.

## If something's wrong

| Symptom | Cause |
|---|---|
| Compose stops with `set it in .env` | A required setting is empty. See `.env.example` |
| Everyone gets `403` | The address isn't in `ALLOWED_IPS`, or your nginx isn't sending `X-Real-IP`. `docker logs omniconvert-gateway` shows the address it saw. With no nginx, going straight to `127.0.0.1`, it sees `172.31.90.1` and not your machine's IP, so that's the one to allow (see `.env.example`) |
| `preflight.sh` says the folder isn't private | Run `scripts/lock-permissions.sh` |
| Worker or gateway keeps restarting | `docker logs` says why, usually the memory limit (see Install) or memory settings that don't add up |
| "No readable text was found" | Correct for a file with no text in it, such as most audio |
| Upload fails with `413` | `client_max_body_size` in nginx, or `MAX_UPLOAD_BYTES` |
| "Converter offline" | The worker isn't running or is starting up. `docker logs omniconvert-worker` says why |
| Long video stops at 30 minutes | Raise `JOB_TIMEOUT_SECONDS` |
| "The server's memory is full" | Too many files waiting or uncollected. It clears as they're downloaded or expire. See [Memory](#memory-decides-how-large-a-file-can-be) |
| "This conversion needed more memory" | The file needs more room than `WORKER_WORK_SIZE` or `WORKER_MEMORY` allow |
| A file keeps failing and you need to know why | Set `LOG_CONVERTER_OUTPUT=1`, `docker compose up -d`, try it, read `docker logs omniconvert-worker`, and turn it off again |
| A download link says the file is gone | It was already downloaded, or it expired. Results exist once |

## Testing

```bash
# Unit tests, on any machine:
python -m venv .venv && .venv/bin/pip install fastapi uvicorn pydantic pytest httpx
PYTHONPATH=. .venv/bin/pytest tests/unit

# Against the running service, started with ALLOWED_IPS=203.0.113.0/24,127.0.0.1 and RETENTION_MINUTES=120
# in a throwaway env file:
SAMPLES=tests/out PYTHONPATH=. python tests/attacks/run_attacks.py     # hostile files, with controls
PYTHONPATH=. python tests/integration/limits_test.py                   # nothing kept, compromised converter, fork bomb, memory hog...
PYTHONPATH=. python tests/integration/nginx_e2e.py                     # the real nginx snippet under a subpath
SAMPLES=tests/out PYTHONPATH=. python tests/integration/ui_e2e.py      # the page in a real browser (needs Playwright)
```

Results of the last run of version 1.2.0, on x86_64, with Docker 29.8:

| Test | Landlock off (as on Raspberry Pi OS) | Landlock on |
|---|---|---|
| Lockdown checks | 107 of 107 | 107 of 107 |
| Hostile files, with controls | 103 of 103 | 103 of 103 |
| Limits and a compromised converter | 109 of 109 | 111 of 111 |
| The real nginx snippet under a subpath | 28 of 28 | 28 of 28 |
| The page in Chromium through nginx | 29 of 29 | 29 of 29 |
| Real conversions | every one offered: 8,967 of 9,050, the 63 failures now blocked | 2,578 of 2,578 (a sample) |

The 554 unit tests also pass. The lockdown, limits and nginx checks, and the sample with Landlock on, ran on the 1.2.0 build just before the last routing fixes, which change only which route is picked, not what those checks cover. The attack and browser tests ran again on the final build. Of the 9,050 conversions, 20 more correctly reported that a file had no text to extract, and the 63 failures were limits of a tool or of the test file, such as an Open Babel crash on one `mol2` file. None came from the sandbox.

Controls run a harmless file through each engine first, because an attack that "fails" on an engine that fails on everything proves nothing. Each attack also has to show it reached the engine it targets.

To regenerate the coverage report:

```bash
docker run --rm --network none --user "$(id -u):$(id -g)" -e HOME=/tmp --tmpfs /tmp:mode=1777 \
    -v "$PWD/tests/fixtures:/fx:ro" -v "$PWD/tests/out:/out" omniconvert-worker:1.2.0 \
    /opt/py/bin/python /fx/make_fixtures.py /out
PYTHONPATH=. python scripts/coverage.py --samples tests/out              # a sample, in about half an hour
PYTHONPATH=. python scripts/coverage.py --samples tests/out --all --state state.jsonl --pairs pairs.json
                                                                       # every route, in hours; stop and rerun to resume
```

Targets that never work are written to `worker/known_bad_routes.json`. With `--all`, each single conversion that still fails after a retry goes to the `--pairs` file, which belongs in `worker/known_bad_pairs.json`. The next build leaves both out of the catalog.

## Layout

```
.env.example                      all settings, explained
docker-compose.yml                the two containers, their limits and lockdown
deploy/host-nginx-subpath.conf    paste into your nginx
docker/                           Dockerfiles, syscall filters, ImageMagick policy, pinned Python dependencies
gateway/                          HTTP, allow-list, upload, queue, results in memory. Parses no user files
shared/link.py                    the protocol on the socket between the two containers
worker/main.py                    the supervisor: runs one job at a time, parses nothing untrusted
worker/init.py                    PID 1: converters can't attach to it, and it ends the container if the link breaks
worker/sandbox_exec.py            applies limits and Landlock, then becomes the converter
worker/engines/                   what each tool can do, and the exact command that does it
worker/tools/                     small helpers that run inside the sandbox
shared/router.py                  finds the route between two formats, keeping each file to its own kind
ui/page.html                      the whole interface, in one file
scripts/                          permissions, preflight, hardening check, scan, coverage, syscall filter generator
```

## Credits

Built on FFmpeg, ImageMagick, libvips, LibreOffice, pandoc, Calibre, Ghostscript, Poppler, Inkscape, GDAL, Assimp, Open Babel, LibreDWG, Apache Tika, ExifTool, Tesseract and many others, under their own licenses. Several are GPL, so the worker image as a whole is subject to their terms when you distribute it. Overpass and Overpass Mono fonts are under the SIL Open Font License.
