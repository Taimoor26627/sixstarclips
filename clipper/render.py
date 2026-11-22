"""Download a Twitch clip and render it as a 1080x1920 Short with ffmpeg."""
import json
import os
import subprocess


def download(url, workdir):
    out = os.path.join(workdir, "src.%(ext)s")
    subprocess.run(
        ["yt-dlp", "-q", "--no-warnings", "-f", "best[ext=mp4]/best", "-o", out, url],
        check=True, timeout=300,
    )
    for f in os.listdir(workdir):
        if f.startswith("src."):
            return os.path.join(workdir, f)
    raise FileNotFoundError("yt-dlp produced no file")


def probe(path):
    info = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", path],
        capture_output=True, text=True, check=True,
    ).stdout)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    has_audio = any(s["codec_type"] == "audio" for s in info["streams"])
    return int(v["width"]), int(v["height"]), float(info["format"]["duration"]), has_audio


def video_filter(layout, w, h, facecam, ass_name, fonts_dir, fps):
    subs = f"subtitles={ass_name}:fontsdir={fonts_dir}"
    if layout == "split" and facecam:
        fx, fy, fw, fh = (int(facecam[k] * w / 1920) if k in "xw" else int(facecam[k] * h / 1080)
                          for k in ("x", "y", "w", "h"))
        return (
            f"[0:v]split=2[a][b];"
            f"[a]crop={fw}:{fh}:{fx}:{fy},scale=1080:672:force_original_aspect_ratio=increase,"
            f"crop=1080:672[cam];"
            f"[b]crop=ih*1080/1248:ih,scale=1080:1248[game];"
            f"[cam][game]vstack=inputs=2,fps={fps},{subs},format=yuv420p[v]"
        )
    return (
        f"[0:v]split=2[bg][fg];"
        f"[bg]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
        f"boxblur=24:2,eq=brightness=-0.14[bgb];"
        f"[fg]scale=1080:-2[fgs];"
        f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2-80,fps={fps},{subs},format=yuv420p[v]"
    )


def render(src, ass_path, out_path, cfg, layout="blur", facecam=None, fonts_dir="assets/fonts"):
    w, h, dur, has_audio = probe(src)
    workdir = os.path.dirname(ass_path)
    rcfg = cfg.get("render", {})
    max_s = min(dur, float(rcfg.get("max_seconds", 59)))
    vf = video_filter(layout, w, h, facecam, os.path.basename(ass_path),
                      os.path.abspath(fonts_dir), rcfg.get("fps", 30))
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", os.path.abspath(src)]
    if not has_audio:
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
    cmd += ["-filter_complex", vf, "-map", "[v]"]
    if has_audio:
        cmd += ["-map", "0:a:0", "-af", "loudnorm=I=-14:TP=-1.5:LRA=11"]
    else:
        cmd += ["-map", "1:a:0"]
    cmd += [
        "-t", f"{max_s:.2f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(rcfg.get("crf", 21)),
        "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
        "-movflags", "+faststart", os.path.abspath(out_path),
    ]
    subprocess.run(cmd, check=True, cwd=workdir, timeout=900)
    return max_s


def export_small(src, out_path, max_bytes=4_700_000):
    """A lighter 720x1280 copy that fits Make's free-plan 5 MB file limit."""
    _, _, dur, _ = probe(src)
    audio_kbps = 96
    video_kbps = min(3500, max(350, int((max_bytes * 8 / 1000) / max(dur, 1) * 0.92) - audio_kbps))
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", src, "-vf", "scale=720:1280",
         "-c:v", "libx264", "-preset", "veryfast", "-b:v", f"{video_kbps}k",
         "-maxrate", f"{int(video_kbps * 1.2)}k", "-bufsize", f"{video_kbps * 2}k",
         "-c:a", "aac", "-b:a", f"{audio_kbps}k", "-movflags", "+faststart", out_path],
        check=True, timeout=600,
    )
    return os.path.getsize(out_path)
