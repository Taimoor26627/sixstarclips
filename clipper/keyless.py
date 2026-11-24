"""Read Twitch clips with no API keys, from each streamer's public clips page (via yt-dlp).

Used automatically when TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET are not set.
Output uses the same fields as the Twitch API so the rest of the bot doesn't care.
"""
from datetime import datetime, timezone

import yt_dlp

_FLAT = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist", "skip_download": True}
_FULL = {"quiet": True, "no_warnings": True, "skip_download": True}


def _iso(ts):
    if not ts:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def list_clips(login, ranges=("24hr", "7d"), per_range=60):
    """Top clips of the last 24h and 7d, merged, newest data wins."""
    seen = {}
    for rng in ranges:
        url = f"https://www.twitch.tv/{login}/clips?filter=clips&range={rng}"
        try:
            with yt_dlp.YoutubeDL({**_FLAT, "playlistend": per_range}) as ydl:
                info = ydl.extract_info(url, download=False) or {}
        except Exception as e:  # noqa: BLE001
            print(f"  could not read clips page for {login} ({rng}): {e}")
            continue
        for e in info.get("entries") or []:
            if not e or not e.get("url"):
                continue
            seen[e.get("id") or e["url"]] = {
                "id": e.get("id") or e["url"].rstrip("/").split("/")[-1],
                "url": e["url"],
                "title": e.get("title") or "",
                "view_count": int(e.get("view_count") or 0),
                "created_at": _iso(e.get("timestamp")),
                "duration": float(e.get("duration") or 0),
                "game_id": None,
            }
    return list(seen.values())


def clip_game(url):
    """The game/category a clip was made in, e.g. 'Grand Theft Auto VI'."""
    try:
        with yt_dlp.YoutubeDL(_FULL) as ydl:
            info = ydl.extract_info(url, download=False) or {}
        cats = info.get("categories") or []
        return cats[0] if cats else None
    except Exception as e:  # noqa: BLE001
        print(f"  could not check game for {url}: {e}")
        return None


def last_vod(login):
    url = f"https://www.twitch.tv/{login}/videos?filter=archives&sort=time"
    try:
        with yt_dlp.YoutubeDL({**_FLAT, "playlistend": 1}) as ydl:
            info = ydl.extract_info(url, download=False) or {}
        entries = [e for e in (info.get("entries") or []) if e]
        return entries[0] if entries else None
    except Exception as e:  # noqa: BLE001
        print(f"  could not read videos for {login}: {e}")
        return None
