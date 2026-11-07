"""Audio and video."""

from __future__ import annotations

import re
import subprocess

from worker.engines.base import Engine, Simple, group

# Never offered, and refused even if a file's contents try to select one. Playlist-style demuxers
# open other files named inside the input, which is the classic way to make ffmpeg read local files.
_DENY_DEMUX = {
    "hls", "dash", "concat", "sdp", "rtsp", "rtp", "rtmp", "tee", "lavfi", "fbdev", "kmsgrab", "x11grab",
    "v4l2", "alsa", "oss", "pulse", "sndio", "jack", "ass_sub", "webm_dash_manifest", "image2", "imf",
    "libcdio", "dv1394", "iec61883", "cue", "ffmetadata", "mpjpeg", "smjpeg", "bin", "data", "nut_probe",
    "sap", "rtsp_tunnel", "tcp", "udp", "rawvideo", "yuv4mpegpipe",
    # Renders ANSI art, and claims .asc and .nfo, which are nearly always something else.
    "tty",
}
_DENY_DEMUX_RE = re.compile(r"(_pipe$|^image2|^pcm|^[su](8|16|24|32)|^[fd](32|64)|^(alaw|mulaw|vidc|sln|g722|g726))")
_DENY_MUX = {
    "hls", "dash", "tee", "segment", "stream_segment", "ssegment", "fifo", "fifo_test", "null", "image2",
    "image2pipe", "rtp", "rtp_mpegts", "rtsp", "sap", "smoothstreaming", "hds", "webm_chunk", "webm_dash_manifest",
    "chromaprint", "framecrc", "framehash", "framemd5", "hash", "crc", "md5", "streamhash", "ffmetadata", "uncodedframecrc",
    "fbdev", "alsa", "oss", "pulse", "sndio", "v4l2", "xv", "caca", "opengl", "sdl", "sdl2", "rawvideo", "yuv4mpegpipe",
    "singlejpeg", "mjpeg", "mpjpeg", "bit", "data", "psp", "ipod", "ismv", "f4v", "dvd", "svcd", "vcd", "vob",
    "gif", "apng", "webp",
}
_DENY_MUX_RE = re.compile(r"(_pipe$|^pcm|^[su](8|16|24|32)|^[fd](32|64)|^(alaw|mulaw|vidc|sln|g722|g726|ac4|adx|amrnb|amrwb))")
_NON_MEDIA_FAMILIES = {"image", "raw", "vector", "document", "ebook", "archive", "spreadsheet", "slides", "data", "disk", "font", "map", "model", "cad", "mail", "science", "medical", "diagram", "encoded"}
_LOSSLESS_CODECS = re.compile(r"^(pcm_|flac|alac|wavpack|tta|mlp|truehd|s302m|ffv1|huffyuv|ffvhuff|utvideo|rawvideo|none)")

# Common inputs whose demuxer reports no extension.
_STATIC_READS = [
    "mp4", "m4v", "mkv", "webm", "mov", "avi", "wmv", "asf", "flv", "mpg", "mpeg", "ts", "m2ts", "mts", "vob", "ogv",
    "3gp", "3g2", "rm", "rmvb", "mxf", "swf", "mp3", "flac", "wav", "ogg", "oga", "opus", "aac", "m4a", "m4b", "wma",
    "aiff", "aif", "ac3", "dts", "amr", "ape", "wv", "mka", "au", "caf", "voc",
]


# Containers whose default codecs only accept certain frame rates, sample rates or sizes. Without
# these, a phone video at 29.97 fps or 48 kHz audio would simply fail.
_VIDEO_OPTIONS = {
    "mpg": ["-r", "25", "-ar", "44100"],
    "vob": ["-r", "25", "-ar", "48000"],
    "mxf": ["-r", "25", "-ar", "48000", "-ac", "2"],
    "3gp": ["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "aac"],
    "flv": ["-ar", "44100"],
    "swf": ["-ar", "44100"],
    "dv": ["-s", "720x576", "-r", "25", "-pix_fmt", "yuv420p", "-ar", "48000", "-ac", "2"],
    "ogv": ["-q:v", "6"],
    "wmv": ["-q:v", "4"],
    "avi": ["-q:v", "4"],
}
_AUDIO_OPTIONS = {
    "amr": ["-ar", "8000", "-ac", "1"],
    "ac3": ["-ar", "48000"],
    "dts": ["-strict", "-2", "-ar", "48000"],
    "wma": ["-ar", "44100"],
    "mp2": ["-ar", "44100"],
    "opus": ["-ar", "48000"],
    "gsm": ["-c:a", "libgsm", "-ar", "8000", "-ac", "1"],
}


def _run(*args: str) -> str:
    return subprocess.run(args, capture_output=True, text=True, timeout=30, check=False, errors="replace").stdout


class FFmpeg(Engine):
    name = "FFmpeg"
    binary = "ffmpeg"

    def __init__(self):
        self.whitelist = ""
        self.audio_targets: set[str] = set()

    def _names(self, flag: str) -> list[str]:
        names = []
        seen_rule = False
        for line in _run("ffmpeg", "-hide_banner", flag).splitlines():
            if line.strip().startswith("---"):
                seen_rule = True
                continue
            if seen_rule and line.strip():
                parts = line.split()
                if len(parts) >= 2:
                    names.append(parts[1])
        return names

    def describe(self, db):
        reads: set[str] = set()
        allowed = []
        for spec in self._names("-demuxers"):
            for name in spec.split(","):
                if name in _DENY_DEMUX or _DENY_DEMUX_RE.search(name):
                    continue
                allowed.append(name)
            names = spec.split(",")
            if any(n in _DENY_DEMUX or _DENY_DEMUX_RE.search(n) for n in names):
                continue
            info = _run("ffmpeg", "-hide_banner", "-h", f"demuxer={names[0]}")
            head = re.match(r"Demuxer \S+ \[(.*?)\]:", info)
            match = re.search(r"Common extensions: ([^.]*)\.", info)
            for ext in (match.group(1).split(",") if match else []):
                fid = db.resolve(ext)
                if fid is None:
                    fid = db.ensure(ext, head.group(1) if head else None, "media")
                if fid and db.family(fid) in ("video", "audio", "media"):
                    reads.add(fid)
        reads.update(fid for fid in db.ids(_STATIC_READS))

        video_out: set[str] = set()
        audio_out: set[str] = set()
        subtitle_out: set[str] = set()
        for spec in self._names("-muxers"):
            name = spec.split(",")[0]
            if name in _DENY_MUX or _DENY_MUX_RE.search(name):
                continue
            info = _run("ffmpeg", "-hide_banner", "-h", f"muxer={name}")
            head = re.match(r"Muxer \S+ \[(.*?)\]:", info)
            ext_match = re.search(r"Common extensions: ([^.]*)\.", info)
            if not ext_match:
                continue
            video = re.search(r"Default video codec: (\S+?)\.?$", info, re.M)
            audio = re.search(r"Default audio codec: (\S+?)\.?$", info, re.M)
            subtitle = re.search(r"Default subtitle codec: (\S+?)\.?$", info, re.M)
            video_codec = video.group(1) if video else None
            audio_codec = audio.group(1) if audio else None
            family = "video" if video_codec else "subtitle" if subtitle and not audio_codec else "audio"
            lossy = not all(_LOSSLESS_CODECS.match(c) for c in (video_codec, audio_codec) if c)
            ext = ext_match.group(1).split(",")[0].strip()
            fid = db.resolve(ext) or db.ensure(ext, head.group(1) if head else None, family, lossy)
            if not fid or db.family(fid) in _NON_MEDIA_FAMILIES:
                continue
            kind = db.family(fid)
            if kind == "media":
                # Ambiguous in the format table, so what this muxer itself carries decides. A raw audio
                # stream such as aptX filed as video made "MP4 to aptX to MP3" look cheaper than taking the
                # sound out directly, so every video's sound was encoded twice.
                kind = family
            (video_out if kind in ("video", "media") else subtitle_out if kind == "subtitle" else audio_out).add(fid)

        self.whitelist = ",".join(sorted(set(allowed)))
        self.audio_targets = set(audio_out)
        # Formats only an audio muxer writes, such as aptX, hold no picture, so nothing that converts
        # video may read them: they made "MP3 to aptX to H.264" look like a route.
        audio_only = audio_out - video_out
        videos = {f for f in reads if db.family(f) == "video" or (db.family(f) == "media" and f not in audio_only)}
        # A format that can hold video (DV, NUT, raw H.264) isn't plain sound: taking the sound out of
        # it is extraction, like any video. Read as audio, it let "MP4 to DV to AAC" pass as cheaper
        # and better graded than taking the sound straight out of the MP4.
        carries_video = {f for f in videos if db.family(f) == "video" or f in video_out}
        audios = {f for f in reads if db.family(f) == "audio" or (db.family(f) == "media" and f not in carries_video)}
        subs = db.ids(["srt", "vtt", "ass", "ssa", "sub"])
        groups = [
            group(self.name, sorted(videos), sorted(video_out), "auto"),
            group(self.name, sorted(audios), sorted(audio_out), "auto"),
            group(self.name, sorted(carries_video), sorted(audio_out), "extract"),
            # A video container holding only the sound. Some people need this for upload sites. Terminal:
            # with no picture in it, nothing that converts video can do anything with it.
            group(self.name, sorted(f for f in audios if db.family(f) == "audio"), db.ids(["mp4", "mkv", "webm", "mov"]), "lossy", terminal=True),
            # Terminal, so video becomes an animation or frames and nothing further from there.
            group(self.name, sorted(videos), db.ids(["gif", "apng"]), "lossy", terminal=True),
            # One frame a second, returned as a zip. Terminal because it's many files.
            group(self.name, sorted(videos), db.ids(["png", "jpg"]), "extract", terminal=True),
        ]
        if subtitle_out:
            groups.append(group(self.name, subs, sorted(subtitle_out), "lossy"))
        return groups

    def export_data(self):
        return {"whitelist": self.whitelist, "audio_targets": sorted(self.audio_targets)}

    def load_data(self, data):
        self.whitelist = data["whitelist"]
        self.audio_targets = set(data["audio_targets"])

    def commands(self, step):
        argv = [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-protocol_whitelist", "file,pipe",
            # Content picks the demuxer whatever the file is called, so a file named .mp4 can really
            # be a playlist. This refuses every demuxer that isn't on the list.
            "-format_whitelist", self.whitelist,
            "-i", f"file:{step.src}",
        ]
        target = step.out_ext
        output = str(step.output)
        if step.dst_fmt in self.audio_targets:
            argv += ["-vn", *_AUDIO_OPTIONS.get(target, [])]
        elif target in _VIDEO_OPTIONS:
            argv += _VIDEO_OPTIONS[target]
        elif target == "gif":
            argv += ["-vf", "fps=10,scale=min(640\\,iw):-2:flags=lanczos"]
        elif target == "apng":
            argv += ["-vf", "fps=10,scale=min(640\\,iw):-2", "-plays", "0"]
        elif target in ("png", "jpg"):
            argv += ["-vf", "fps=1"]
            output = str(step.out / f"frame-%05d.{target}")
        elif target in ("mp4", "mkv", "mov", "m4v"):
            argv += ["-preset", "veryfast", "-pix_fmt", "yuv420p"]
        elif target == "webm":
            # The encoder's default speed is far too slow for a Raspberry Pi.
            argv += ["-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "5", "-b:v", "0", "-crf", "34"]
        argv += ["-threads", str(step.threads), "-y", output]
        return [argv]


def media_engines():
    return [
        FFmpeg(),
        Simple("SoX", "sox", [
            (["wav", "aiff", "au", "flac", "ogg", "voc", "mp3", "8svx", "gsm", "cdda", "cvs", "maud", "prc-audio"],
             ["wav", "aiff", "au", "flac", "ogg", "voc", "mp3"], "auto"),
        ], lambda s: ["sox", s.src, s.output], priority=0.5, address_space=4 * 1024**3),
        Simple("FluidSynth", "fluidsynth", [(["mid"], ["wav"], "lossy")],
               lambda s: ["fluidsynth", "-ni", "-q", "-F", s.output, "-T", "wav", "-r", "44100",
                          "/usr/share/sounds/sf2/FluidR3_GM.sf2", s.src]),
        Simple("OpenMPT", "openmpt123", [(["mod", "xm", "it", "s3m", "mptm", "stm", "669", "mtm", "med", "okt", "ult"], ["wav", "flac"], "exact")],
               lambda s: ["openmpt123", "--batch", "--force", "--quiet", "--output", s.output, s.src], address_space=4 * 1024**3),
    ]
