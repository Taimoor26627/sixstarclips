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
    if clip.get("_streamer", {}).get("campaign"):
        w *= 1.3   # paid Content Rewards campaign: these views earn money
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


def ask_llm(prompt, timeout=120):
    """Free text model. Claude Code (if its token is set), else GitHub Models (free, uses the
    workflow's own GITHUB_TOKEN). Returns text or None."""
    if shutil.which("claude") and (os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY")):
        try:
            out = subprocess.run(["claude", "-p", prompt, "--output-format", "text"],
                                 capture_output=True, text=True, timeout=timeout).stdout
            if out.strip():
                return out
        except Exception as e:  # noqa: BLE001
            print(f"  claude skipped: {e}")
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        import requests
        for model in ("openai/gpt-4.1", "openai/gpt-4.1-mini"):
            try:
                r = requests.post("https://models.github.ai/inference/chat/completions",
                                  headers={"Authorization": f"Bearer {token}",
                                           "Accept": "application/vnd.github+json",
                                           "X-GitHub-Api-Version": "2022-11-28",
                                           "Content-Type": "application/json"},
                                  json={"model": model, "temperature": 0.9,
                                        "messages": [{"role": "user", "content": prompt}]},
                                  timeout=timeout)
                if r.status_code == 200:
                    try:
                        return r.json()["choices"][0]["message"]["content"]
                    except ValueError:
                        print(f"  GitHub Models {model}: unreadable answer: {r.headers.get('content-type')} "
                              f"{r.text[:200]!r}")
                        continue
                print(f"  GitHub Models {model}: {r.status_code} {r.text[:150]}")
            except Exception as e:  # noqa: BLE001
                print(f"  GitHub Models {model} failed: {e}")
    return None


def ask_json(prompt):
    out = ask_llm(prompt)
    if not out:
        return None
    m = re.search(r"\{.*\}", out, re.S)
    try:
        return json.loads(m.group(0)) if m else None
    except ValueError:
        return None


TITLE_RULES = (
    "Title rules (copied from the most-viewed GTA 6 streamer Shorts): start with the streamer's name, "
    "say exactly what happens in this clip as a mini story (who did what, what went wrong), "
    "Title Case, 45-75 characters, end with 1-2 emoji like 😭 💀 😂 🤣. Examples of the style: "
    "'Kai Cenat Was Hopping Fences For His GTA 6 Trailer! 😭💀', "
    "'NPC Did NOT Let YourRage Slide After He Kicked His Car in GTA 6 😂😭'. "
    "Never use vague templates like 'can't believe what happened' or 'lost it'. "
    "The on-screen hook is different from the title: max 6 words, a bold claim or question that makes "
    "people stop scrolling, no emoji. No story spoilers, no lies, no 'official'."
)


def _claude_hook(name, clip_title, transcript, login=""):
    """Hook + title written by a language model from what was actually said. dict or None."""
    prompt = (
        "You write hooks and titles for a YouTube Shorts channel that posts credited GTA 6 stream clips.\n"
        f"Streamer: {name}\nClip title (written by a Twitch viewer, may be junk): {clip_title}\n"
        f"What the streamer says in the clip (auto transcript, may be translated): {transcript[:1500]}\n\n"
        + _audience_block(login) + TITLE_RULES + "\n"
        'Return ONLY JSON: {"hook": "...", "title": "..."}'
    )
    data = ask_json(prompt)
    if data and data.get("hook") and data.get("title"):
        return {"hook": clean_title(data["hook"])[:60], "title": data["title"].strip()[:95]}
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

    quote = transcript_quote(transcript) if transcript else None
    if not looks_like_default_title(raw, cleaned) and cleaned.lower() not in recent_openers:
        hook = cleaned                       # a viewer named the clip: usually says what happened
        title = hook
    elif quote and quote.lower() not in recent_openers:
        hook = f'"{quote}"'                  # the streamer's own words: specific to this clip
        title = f'{name}: "{quote}"'
    else:
        options = [t.format(name=name) for t in cfg["hooks"]["templates"]]
        fresh = [o for o in options if o.lower() not in recent_openers] or options
        hook = random.choice(fresh)
        title = hook

    title = title.capitalize() if title.isupper() else title[:1].upper() + title[1:]
    low = title.lower()
    if name.lower() not in low:
        first_names = [p for p in name.lower().split() if len(p) > 2]
        if not any(p in low for p in first_names):
            title = f"{name} {title[:1].lower() + title[1:]}" if title.lower().startswith(("was ", "vs ", "can't", "completely", "broke")) else f"{name}: {title}"
    if "gta" not in title.lower():
        title = f"{title} in GTA 6"
    # Top GTA 6 Shorts end titles with an emoji reaction (research: reports/research.md)
    if not EMOJI_RE.search(title):
        title = f"{title} {random.choice(['😭', '💀', '😂', '😭💀', '🤣'])}"
    return hook, title[:95]


QUOTE_WORDS = re.compile(r"\b(nah|bro|bruh|what|why|no|yo|wait|stop|help|oh my god|omg|cops?|police|run|"
                         r"chat|crazy|insane|bro what|hell nah|let'?s go|i'?m done|who)\b", re.I)


def transcript_quote(text):
    """Most quotable short line the streamer said (3-8 words), for hook and title."""
    from .captions import censor

    parts = re.split(r"(?<=[.!?])\s+", text or "")
    best, best_s = None, 0.0
    for p in parts:
        words = p.strip().split()
        if not 3 <= len(words) <= 8:
            continue
        line = " ".join(censor(w) for w in words).strip(" .,")
        if "*" in line or not re.search(r"[a-z]{3}", line, re.I):
            continue
        sc = 1.0 + 2 * p.count("!") + p.count("?") + len(QUOTE_WORDS.findall(p))
        sc -= 0.15 * abs(len(words) - 5)
        if sc > best_s:
            best, best_s = line, sc
    if not best or best_s < 2:
        return None
    best = re.sub(r"\s+", " ", best)
    return best[:1].upper() + best[1:]


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
