"""Clips from YouTube live streams (no keys): uses the stream's "Most replayed" heatmap.

1. List the channel's recent past live streams and keep GTA 6 ones.
2. Read the heatmap (what viewers re-watched most) and take the top peaks.
3. Download only ~3 minutes around a peak, find the loudest reaction in it,
   and cut a ~35 s clip ending just after that reaction.
"""
import json
import os
import re
import subprocess

import yt_dlp

from .select import now_utc

_FLAT = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist", "skip_download": True}
_FULL = {"quiet": True, "no_warnings": True, "skip_download": True}


def cookie_file():
    """YouTube blocks cloud servers unless the request carries cookies from a signed-in
    browser. The YT_COOKIES secret holds a cookies.txt (Netscape format) from a SPARE account."""
    data = os.environ.get("YT_COOKIES", "").strip()
    if not data:
        return None
    path = os.path.join(os.environ.get("RUNNER_TEMP", "/tmp"), "yt_cookies.txt")
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write(data + "\n")
    return path


def available():
    return cookie_file() is not None


def _opts(base):
    c = cookie_file()
    return {**base, "cookiefile": c} if c else base
GTA_RE = re.compile(r"gta\s*(6|vi)\b|grand theft auto\s*(6|vi)\b|gta6|vice city", re.I)


def _channel_url(s):
    ch = s["channel"]
    if ch.startswith("http"):
        return ch.rstrip("/") + "/streams"
    return f"https://www.youtube.com/{ch.lstrip('/')}/streams"


def recent_streams(s, max_items=20, keep=5):
    """The channel's most-viewed GTA 6 past streams among its last `max_items` streams."""
    try:
        with yt_dlp.YoutubeDL(_opts({**_FLAT, "playlistend": max_items})) as ydl:
            info = ydl.extract_info(_channel_url(s), download=False) or {}
        rows = [e for e in (info.get("entries") or []) if e and e.get("id") and GTA_RE.search(e.get("title") or "")]
        return sorted(rows, key=lambda e: e.get("view_count") or 0, reverse=True)[:keep]
    except Exception as e:  # noqa: BLE001
        print(f"  could not list streams for {s['name']}: {e}")
        return []


def full_info(video_id):
    with yt_dlp.YoutubeDL(_opts(_FULL)) as ydl:
        return ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False) or {}


def peaks(heatmap, duration, count=4, min_gap=120, skip_start=300, min_value=0.35):
    """Highest heatmap segments, spaced out, ignoring the stream's first minutes."""
    segs = sorted(heatmap or [], key=lambda h: h.get("value", 0), reverse=True)
    out = []
    for h in segs:
        mid = (h["start_time"] + h["end_time"]) / 2
        if h.get("value", 0) < min_value or mid < skip_start or mid > duration - 30:
            continue
        if all(abs(mid - (p["start_time"] + p["end_time"]) / 2) >= min_gap for p in out):
            out.append(h)
        if len(out) >= count:
            break
    return out


def candidates(s, cfg, state, lookback_hours):
    """Candidate clips for one YouTube streamer, in the same shape as Twitch clips."""
    now = now_utc()
    out = []
    for e in recent_streams(s):
        if not GTA_RE.search(e.get("title") or ""):
            continue
        try:
            info = full_info(e["id"])
        except Exception as ex:  # noqa: BLE001
            print(f"  could not read {e['id']}: {ex}")
            continue
        if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
            continue  # heatmap only exists after the stream ends
        ts = info.get("release_timestamp") or info.get("timestamp")
        if ts and (now.timestamp() - ts) > lookback_hours * 3600:
            continue
        dur = float(info.get("duration") or 0)
        hm = info.get("heatmap")
        if not hm:
            print(f"  no 'most replayed' data yet for {e['id']}")
            continue
        for h in peaks(hm, dur):
            cid = f"yt_{e['id']}_{int(h['start_time'])}"
            if cid in state["posted"]:
                continue
            created = now.strftime("%Y-%m-%dT%H:%M:%SZ")
            out.append({
                "id": cid,
                "url": f"https://www.youtube.com/watch?v={e['id']}&t={int(h['start_time'])}s",
                "title": info.get("title") or "",
                # comparable-ish to Twitch clip views: how hot the moment is x how big the stream was
                "view_count": int(h["value"] * min(cfg["selection"].get("youtube_peak_weight", 40000),
                                                   0.05 * (info.get("view_count") or 0) + 2000)),
                "created_at": created,
                "duration": 35.0,
                "_yt": {"video_id": e["id"], "start": h["start_time"], "end": h["end_time"], "duration": dur},
            })
    return out


def _loudest_second(path):
    """Second (from file start) with the loudest short-term audio."""
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", path, "-af", "ebur128=peak=none", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    best_t, best_s = None, -1e9
    for line in r.stderr.splitlines():
        m = re.search(r"t:\s*([\d.]+).*?S:\s*(-?[\d.]+)", line)
        if m:
            t, s = float(m.group(1)), float(m.group(2))
            if s > best_s:
                best_t, best_s = t, s
    return best_t


def fetch(c, workdir, clip_len=35, tail=8):
    """Download around the peak and cut the final clip. Returns the clip path."""
    y = c["_yt"]
    win_start = max(0.0, y["start"] - 40)
    win_end = min(y["duration"], max(y["end"], y["start"] + 60) + 40)
    raw = os.path.join(workdir, "window.%(ext)s")
    cookies = ["--cookies", cookie_file()] if cookie_file() else []
    subprocess.run(
        ["yt-dlp", "-q", "--no-warnings", *cookies,
         "-f", "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080]/b",
         "--download-sections", f"*{win_start:.0f}-{win_end:.0f}",
         "--merge-output-format", "mp4", "-o", raw,
         f"https://www.youtube.com/watch?v={y['video_id']}"],
        check=True, timeout=900,
    )
    window = next(os.path.join(workdir, f) for f in os.listdir(workdir) if f.startswith("window."))
    peak = _loudest_second(window) or (y["start"] - win_start + 20)
    start = max(0.0, peak + tail - clip_len)
    out = os.path.join(workdir, "src.mp4")
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.2f}", "-i", window, "-t", str(clip_len),
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-c:a", "aac", "-b:a", "192k", out],
        check=True, timeout=600,
    )
    return out
