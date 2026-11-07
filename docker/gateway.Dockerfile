# The gateway image: Python and three pinned libraries. No converter, no shell tools it doesn't need.
#
# To refresh the base image, update the digest: docker buildx imagetools inspect python:3.13-slim-trixie
ARG PYTHON_IMAGE=python:3.13-slim-trixie@sha256:ae14d4ce8771606477a81446e7ef429edd6762305fc50c84507c1041fa8285d1
FROM ${PYTHON_IMAGE}

LABEL org.opencontainers.image.title="OmniConvert gateway"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# The base image is pinned, so this is what brings in security fixes published since. A rebuild only runs
# it with --no-cache; otherwise this layer comes from the cache and nothing changes.
RUN apt-get update \
 && apt-get upgrade -y \
 && rm -rf /var/lib/apt/lists/* /var/cache/apt/*

COPY docker/requirements-gateway.txt /tmp/requirements.txt
RUN pip install --require-hashes --only-binary=:all: -r /tmp/requirements.txt && rm /tmp/requirements.txt

COPY gateway /app/gateway
COPY shared /app/shared
COPY ui/page.html /app/ui/page.html

# Its own user and group, sharing nothing with the worker. No login shell, no home. Application files
# are root-owned and read-only for this user. Setuid bits are removed, no folder is left world-writable,
# and what could install anything is gone.
RUN groupadd -g 10001 gateway \
 && useradd -u 10001 -g 10001 -M -d /nonexistent -s /usr/sbin/nologin gateway \
 && mkdir -p /jobs /run/omniconvert \
 && python -m compileall -q /app \
 && chown -R root:root /app \
 && find /app -type d -exec chmod 555 {} + \
 && find /app -type f -exec chmod 444 {} + \
 && pip uninstall -y pip setuptools wheel >/dev/null 2>&1 || true
RUN find / -xdev -type f -perm /6000 -exec chmod a-s {} + 2>/dev/null || true \
 && find / -xdev -type d -perm -0002 -exec chmod o-w {} + 2>/dev/null || true \
 && rm -f /usr/bin/apt /usr/bin/apt-get /usr/bin/apt-cache /usr/bin/apt-config /usr/bin/dpkg /usr/bin/dpkg-deb /usr/bin/dpkg-query
# Last, because it removes the shell and the tools this line runs with. Python is all this container ever
# runs, so a compromised gateway finds nothing else to run: no shell, Perl, awk, sed, find or tar.
RUN find /usr/bin /usr/sbin -mindepth 1 ! -type d ! -name true -delete

USER 10001:10001
WORKDIR /app
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["python", "-I", "/app/gateway/healthcheck.py"]
CMD ["python", "-m", "gateway"]
