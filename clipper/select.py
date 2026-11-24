"""Choosing which clips to post, and writing their hooks, titles and descriptions."""
import json
import os
import random
import re
import shutil
import subprocess
from datetime import datetime, timezone

EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF‍️]+"
)


def parse_time(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def clean_title(t):
    t = EMOJI_RE.sub("", t or "")
    t = re.sub(r"https?://\S+", "", t)
    t = re.sub(r"[!@]\w+", "", t)          # !commands, @mentions
    t = re.sub(r"\s+", " ", t).strip(" -|:~*")
    return t


def looks_like_default_title(raw, cleaned):
    """Twitch copies the stream title when the clipper doesn't rename a clip."""
    if len(cleaned) < 4 or len(cleaned) > 70:
        return True
    words = re.findall(r"[A-Za-z']+", cleaned)
    real = [w for w in words if len(w) >= 2 and not re.search(r"(.)\1{3,}", w, re.I)]
    if len(real) < 3:          # junk like "Sssssssss", "W", "LMAOOOO", "omg"
        return True
    if raw.count("|") >= 1 or raw.count("!") >= 3:
        return True
    if re.search(r"\b(day|hour|marathon|live|stream|subathon|sub goal|#\d+)\b", cleaned, re.I):
        return True
    return False


def has_spoiler(text, words):
    low = (text or "").lower()
    return any(w.lower() in low for w in words)


HALF_LIFE_H = 168.0   # a week-old clip counts half; set from config "selection.half_life_hours"


def score(clip, now):
    """Proven popularity (clip views), nudged by what each streamer's audience watches
    and by how our own Shorts of that streamer did (state/learn.json)."""
    from . import research

    age_h = max(0.0, (now - parse_time(clip["created_at"])).total_seconds() / 3600)
    login = clip.get("_streamer", {}).get("login", "")
    w = research.weight(login) if login else 1.0
    kws, _ = research.audience(login) if login else ([], [])
    title = (clip.get("title") or "").lower()
    if kws and any(k in title for k in kws[:8]):
        w *= 1.3   # the clip is about something this audience already watches a lot
    return clip["view_count"] * w * (0.5 ** (age_h / HALF_LIFE_H))


def pick(candidates, n, now):
    """Highest score first, but avoid the same streamer twice in one run."""
    ranked = sorted(candidates, key=lambda c: score(c, now), reverse=True)
    chosen, used = [], set()
    for c in ranked:                       # first pass: one per streamer
        if len(chosen) >= n:
            break
        if c["_streamer"]["login"] not in used:
            chosen.append(c)
            used.add(c["_streamer"]["login"])
    for c in ranked:                       # second pass: fill any gaps
        if len(chosen) >= n:
            break
        if c not in chosen:
            chosen.append(c)
    return chosen


def _claude_hook(name, clip_title, transcript, login=""):
    """Optional: ask Claude Code (on your own plan) for a hook. Returns dict or None."""
    if not shutil.which("claude"):
        return None
    if not (os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY")):
        return None
    prompt = (
        "You write hooks for a YouTube Shorts channel that posts credited GTA 6 stream clips.\n"
        f"Streamer: {name}\nTwitch clip title (written by a viewer): {clip_title}\n"
        f"Transcript: {transcript[:1500]}\n\n"
        + _audience_block(login) +
        "Return ONLY JSON: {\"hook\": \"on-screen text, max 7 words, no emoji\", "
        "\"title\": \"YouTube title under 80 chars that names the streamer and says what happened\"}. "
        "The hook must make a viewer stop scrolling in the first second: a bold claim, a question, "
        "or the stakes of the moment (e.g. 'He did NOT expect this cop'). Match the tone of what this "
        "streamer's audience already watches. No story spoilers, no clickbait lies, no 'official'."
    )
    try:
        out = subprocess.run(
            ["claude", "-p", prompt, "--output-format", "text"],
            capture_output=True, text=True, timeout=180,
        ).stdout
        m = re.search(r"\{.*\}", out, re.S)
        data = json.loads(m.group(0)) if m else None
        if data and data.get("hook") and data.get("title"):
            return {"hook": clean_title(data["hook"])[:60], "title": clean_title(data["title"])[:90]}
    except Exception as e:  # noqa: BLE001 - optional step must never break a run
        print(f"  claude hook skipped: {e}")
    return None


def _audience_block(login):
    from . import research

    kws, titles = research.audience(login)
    if not titles:
        return ""
    return ("Most-viewed GTA 6 Shorts about this streamer right now (learn from what their audience "
            "likes; do not copy):\n" + "\n".join(f"- {t}" for t in titles[:6]) + "\n"
            + (f"Topics that do well: {', '.join(kws[:8])}\n" if kws else "") + "\n")


def make_hook(clip, streamer, cfg, recent_openers, transcript=""):
    name = streamer["name"]
    raw = clip.get("title", "")
    cleaned = clean_title(raw)

    if cfg.get("hooks", {}).get("use_claude"):
        got = _claude_hook(name, raw, transcript, streamer.get("login", ""))
        if got and got["hook"].lower() not in recent_openers:
            return got["hook"], got["title"]

    if not looks_like_default_title(raw, cleaned) and cleaned.lower() not in recent_openers:
        hook = cleaned
    else:
        options = [t.format(name=name) for t in cfg["hooks"]["templates"]]
        fresh = [o for o in options if o.lower() not in recent_openers] or options
        hook = random.choice(fresh)

    title = hook.capitalize() if hook.isupper() else hook[:1].upper() + hook[1:]
    if "gta" not in title.lower():
        title = f"{title} in GTA 6"
    low = title.lower()
    if name.lower() not in low:
        first_names = [p for p in name.lower().split() if len(p) > 2]
        title = f"{title} | {name}" if any(p in low for p in first_names) else f"{name}: {title}"
    return hook, title[:95]


def source_link(streamer):
    if streamer.get("platform") == "youtube":
        ch = streamer["channel"]
        return ch if ch.startswith("http") else f"https://youtube.com/{ch}"
    return f"https://twitch.tv/{streamer['login']}"


def credit_text(streamer):
    if streamer.get("platform") == "youtube":
        return "youtube.com/" + streamer["channel"].rstrip("/").split("/")[-1]
    return f"twitch.tv/{streamer['login']}"


def describe(clip, streamer, cfg):
    name = streamer["name"]
    tags = ["#GTA6", "#Shorts"] + list(streamer.get("hashtags", []))
    lines = []
    if streamer.get("campaign"):
        lines.append("#ad Clipped as part of the creator's official clipping program.")
    lines += [
        f"All credit to {name}. Watch live: {source_link(streamer)}",
        f"Original clip: {clip['url']}",
        "",
        f"Daily GTA 6 streamer moments on {cfg['channel']['name']}. Subscribe for more.",
        "Not affiliated with Rockstar Games or Take-Two Interactive.",
        "",
        " ".join(dict.fromkeys(tags)),
    ]
    from . import research

    kws, _ = research.audience(streamer.get("login", ""))
    yt_tags = ["GTA 6", "GTA VI", name, f"{name} GTA 6", "GTA 6 funny moments", "streamer clips"]
    yt_tags += [f"{name} {k}" for k in kws[:4]]
    return "\n".join(lines), yt_tags


def now_utc():
    return datetime.now(timezone.utc)
