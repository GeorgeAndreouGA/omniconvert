# The worker image: every converter. It's large because that's where the formats come from.
#
#   docker compose build                     # on the Raspberry Pi itself
ARG DEBIAN_IMAGE=debian:trixie-slim@sha256:f6b08b09a05725f9431502d3b5714f5c4576e82c1a84fa1debe0c24a7e0c3ec4

# ---------- LibreDWG: not packaged by Debian, so built from a checksummed source tarball ----------
FROM ${DEBIAN_IMAGE} AS build-libredwg
ARG LIBREDWG_VERSION=0.14
ARG LIBREDWG_SHA256=62ebb73b984f865960f20ed26619ea5f8789d5e3fd088fa40a2598384da81275
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential curl ca-certificates xz-utils pkg-config python3 \
 && curl -fsSL -o /tmp/libredwg.tar.xz "https://ftp.gnu.org/gnu/libredwg/libredwg-${LIBREDWG_VERSION}.tar.xz" \
 && echo "${LIBREDWG_SHA256}  /tmp/libredwg.tar.xz" | sha256sum -c - \
 && mkdir /tmp/src && tar -xf /tmp/libredwg.tar.xz -C /tmp/src --strip-components=1 \
 && cd /tmp/src \
 && ./configure --prefix=/opt/libredwg --disable-bindings --disable-static --disable-docs --disable-dependency-tracking \
 && make -j"$(nproc)" \
 && make install

# ---------- Apache Tika: one checksummed jar ----------
FROM ${DEBIAN_IMAGE} AS fetch-tika
ARG TIKA_VERSION=3.3.3
ARG TIKA_SHA512=3c6e62e864e32af234832ec6d928bbc58692d3ddd60564cd8c80dee7e161653d6efcba373b6cc9468c778010bea92bdee5ca75ab24f01923480bec1b29b573fd
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && mkdir -p /opt/tika \
 && curl -fsSL -o /opt/tika/tika-app.jar "https://archive.apache.org/dist/tika/${TIKA_VERSION}/tika-app-${TIKA_VERSION}.jar" \
 && echo "${TIKA_SHA512}  /opt/tika/tika-app.jar" | sha512sum -c -

# ---------- pandoc: the official build has its data files embedded, which --sandbox needs ----------
FROM ${DEBIAN_IMAGE} AS fetch-pandoc
ARG PANDOC_VERSION=3.13
ARG PANDOC_SHA256_AMD64=38aa33b325a2bc96a4c39409631212db55702f564250bcdbead180b3bf646389
ARG PANDOC_SHA256_ARM64=21e1d4ba09bd0b334590d204580fd4f6629b21be4ab6a32b26cc98982e9e0c68
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && arch="$(dpkg --print-architecture)" \
 && case "$arch" in amd64) sum="$PANDOC_SHA256_AMD64" ;; arm64) sum="$PANDOC_SHA256_ARM64" ;; *) echo "no pandoc for $arch"; exit 1 ;; esac \
 && curl -fsSL -o /tmp/pandoc.tar.gz "https://github.com/jgm/pandoc/releases/download/${PANDOC_VERSION}/pandoc-${PANDOC_VERSION}-linux-${arch}.tar.gz" \
 && echo "$sum  /tmp/pandoc.tar.gz" | sha256sum -c - \
 && tar -xzf /tmp/pandoc.tar.gz -C /tmp \
 && install -D -m 0755 "/tmp/pandoc-${PANDOC_VERSION}/bin/pandoc" /opt/pandoc/pandoc

# ---------- Python libraries, every file checked against a hash ----------
FROM ${DEBIAN_IMAGE} AS build-py
COPY docker/requirements-worker.txt /tmp/requirements.txt
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv \
 && python3 -m venv /opt/py \
 && /opt/py/bin/pip install --no-cache-dir --disable-pip-version-check --require-hashes --only-binary=:all: -r /tmp/requirements.txt

# ---------- the image that runs ----------
FROM ${DEBIAN_IMAGE} AS runtime

LABEL org.opencontainers.image.title="OmniConvert worker"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    DEBIAN_FRONTEND=noninteractive

# The base image is pinned, so the upgrade is what brings in security fixes published since. A rebuild only
# runs this with --no-cache; otherwise this layer comes from the cache and nothing changes.
RUN apt-get update \
 && apt-get upgrade -y \
 && apt-get install -y --no-install-recommends \
      file python3 \
      ffmpeg sox libsox-fmt-all fluidsynth fluid-soundfont-gm openmpt123 espeak-ng espeak-ng-data \
      imagemagick libvips-tools libheif-examples libheif-plugin-aomenc libheif-plugin-x265 libjxl-tools librsvg2-bin resvg potrace inkscape \
      ghostscript poppler-utils img2pdf libraw-bin djvulibre-bin pdf2djvu dvisvgm \
      tesseract-ocr tesseract-ocr-eng tesseract-ocr-ell ocrmypdf \
      libreoffice-nogui \
      weasyprint calibre \
      texlive-xetex texlive-latex-recommended texlive-latex-extra texlive-fonts-recommended texlive-lang-greek lmodern \
      default-jre-headless \
      gdal-bin assimp-utils openscad \
      dcmtk openbabel hdf5-tools netcdf-bin samtools \
      7zip libarchive-tools zstd xz-utils bzip2 qemu-utils genisoimage mame-tools cabextract innoextract lhasa arj cpio squashfs-tools rpm \
      fontforge-nox woff2 libimage-exiftool-perl libemail-outlook-message-perl pst-utils tnef mdbtools sqlite3 graphviz \
      fonts-liberation fonts-dejavu-core fonts-noto-core fonts-noto-cjk \
 && fc-cache -f \
 && rm -rf /var/lib/apt/lists/* /var/cache/apt/*

COPY --from=build-libredwg /opt/libredwg /opt/libredwg
COPY --from=fetch-tika /opt/tika /opt/tika
COPY --from=fetch-pandoc /opt/pandoc/pandoc /usr/local/bin/pandoc
COPY --from=build-py /opt/py /opt/py
COPY --chmod=444 docker/imagemagick-policy.xml /etc/ImageMagick-7/policy.xml

COPY shared /app/shared
COPY worker /app/worker

# The catalog comes from asking the installed tools what they support, so it can't overstate them.
RUN ldconfig /opt/libredwg/lib \
 && echo /opt/libredwg/lib > /etc/ld.so.conf.d/libredwg.conf && ldconfig \
 && PATH=/opt/libredwg/bin:/opt/py/bin:$PATH /opt/py/bin/python -m worker.catalog_build --out /app --skip /app/worker/known_bad_routes.json

# Its own user and group, sharing nothing with the gateway. No login shell, no home. Application files
# are root-owned and read-only for this user. Setuid bits are removed, no folder is left world-writable,
# and what could install or fetch anything is gone. Debian's package list stays, because scanners read it.
RUN groupadd -g 10002 worker \
 && useradd -u 10002 -g 10002 -M -d /nonexistent -s /usr/sbin/nologin worker \
 && mkdir -p /work /run/omniconvert \
 && /opt/py/bin/python -m compileall -q /app \
 && chown -R root:root /app \
 && find /app -type d -exec chmod 555 {} + \
 && find /app -type f -exec chmod 444 {} + \
 && find / -xdev -type f -perm /6000 -exec chmod a-s {} + 2>/dev/null || true
RUN find / -xdev -type d -perm -0002 -exec chmod o-w {} + 2>/dev/null || true
RUN rm -f /usr/bin/apt /usr/bin/apt-get /usr/bin/apt-cache /usr/bin/apt-config /usr/bin/apt-mark /usr/bin/dpkg /usr/bin/dpkg-deb /usr/bin/dpkg-query \
          /usr/bin/su /usr/bin/mount /usr/bin/umount /usr/bin/newgrp /usr/bin/passwd /usr/bin/chsh /usr/bin/chfn /usr/bin/gpasswd \
          /usr/bin/curl /usr/bin/wget \
 && rm -rf /opt/py/bin/pip /opt/py/bin/pip3 /opt/py/bin/pip3.13

USER 10002:10002
WORKDIR /app
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["/opt/py/bin/python", "-I", "/app/worker/healthcheck.py"]
CMD ["/usr/bin/python3", "-I", "-S", "/app/worker/init.py"]
