"""Runs inside the worker: feeds the known tricks straight to ffmpeg and ImageMagick, with their real
settings, and prints the exit code and the end of what they said. See guards() in run_attacks.py."""

import json
import subprocess
import sys

NL = chr(10)
kind = sys.argv[1]
whitelist = json.load(open("/app/engine_data.json"))["FFmpeg"]["whitelist"]

with open("/tmp/concat.bin", "w") as fh:
    fh.write("ffconcat version 1.0" + NL + "file /etc/passwd" + NL)
with open("/tmp/hls.m3u8", "w") as fh:
    fh.write(NL.join(["#EXTM3U", "#EXT-X-MEDIA-SEQUENCE:0", "#EXTINF:1.0,", "file:///etc/passwd", "#EXT-X-ENDLIST", ""]))
with open("/tmp/x.mvg", "w") as fh:
    fh.write(NL.join(["push graphic-context", "viewbox 0 0 64 64", "image over 0,0 0,0 'file:///etc/passwd'", "pop graphic-context", ""]))
with open("/tmp/x.msl", "w") as fh:
    fh.write("<image><read filename='/etc/passwd'/></image>" + NL)

ffmpeg = ["ffmpeg", "-nostdin", "-hide_banner", "-protocol_whitelist", "file,pipe", "-format_whitelist", whitelist]
runs = {
    "concat": [*ffmpeg, "-f", "concat", "-i", "file:/tmp/concat.bin", "-y", "/tmp/o.wav"],
    "hls": [*ffmpeg, "-f", "hls", "-i", "file:/tmp/hls.m3u8", "-y", "/tmp/o.wav"],
    "http": [*ffmpeg, "-i", "http://169.254.169.254/x", "-y", "/tmp/o.wav"],
    "mvg": ["magick", "MVG:/tmp/x.mvg", "/tmp/o.png"],
    "msl": ["magick", "MSL:/tmp/x.msl", "/tmp/o.png"],
    "url": ["magick", "https://169.254.169.254/x.png", "/tmp/o.png"],
    "at": ["magick", "@/etc/passwd", "/tmp/o.png"],
}
done = subprocess.run(runs[kind], capture_output=True, text=True, errors="replace", timeout=30, check=False)
said = done.stdout + done.stderr
# FFmpeg's refusal quotes the whole whitelist, which pushes everything else out of a short excerpt.
if "not on whitelist" in said:
    print(done.returncode, "not on whitelist")
else:
    print(done.returncode, said[-240:].replace(NL, " "))
