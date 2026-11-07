#!/usr/bin/env bash
# Scans the built images for known vulnerabilities with Trivy, run from its own pinned container.
#
#   scripts/scan.sh
#
# It fails only on HIGH or CRITICAL issues that have a fix available. Debian marks many ffmpeg issues
# "postponed", with no fix yet; those show up in the full table but can't be acted on, which is why the
# worker is built assuming a converter can be compromised. Rebuild weekly to pick up what does get
# fixed: docker compose build --no-cache && docker compose up -d
set -euo pipefail

TRIVY_IMAGE=${TRIVY_IMAGE:-aquasec/trivy:0.68.1}
status=0
for image in omniconvert-gateway:1.2.0 omniconvert-worker:1.2.0; do
    echo "== $image"
    docker run --rm -v /var/run/docker.sock:/var/run/docker.sock:ro -v omniconvert-trivy-cache:/root/.cache \
        "$TRIVY_IMAGE" image --quiet --scanners vuln --severity HIGH,CRITICAL --ignore-unfixed --exit-code 1 "$image" || status=1
done
if [ "$status" -ne 0 ]; then
    echo "Fixable HIGH or CRITICAL issues found. Rebuild to pick up fixes: docker compose build --no-cache && docker compose up -d"
fi
exit "$status"
