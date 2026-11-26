"""Multi-clip Shorts ("concepts") built from the day's best clips.

  versus  - 2-3 streamers reacting to the same kind of moment, ends with "WHO DID IT BETTER?"
  ranking - 3 streamers on one theme, counted down #3 -> #1
  top3    - one streamer's 3 best moments, counted down #3 -> #1

A language model (free GitHub Models, or Claude if set up) reads what each streamer says in the
clips and groups them. Without it, simple keyword matching is used.
"""
import hashlib
import json
import os
import re
import subprocess

from . import render, select

TOPICS = {
    "the police": r"\b(cops?|police|wanted|stars?|arrest\w*|jail|prison|chase)\b",
    "Lucia": r"\blucia\b",
    "the first mission": r"\b(first mission|mission|tutorial|intro)\b",
    "crashing cars": r"\b(crash\w*|car|drive|driving|truck|bike)\b",
    "NPCs": r"\b(npc|npcs|guy|lady|man|woman|dude)\b",
    "an alligator": r"\b(gator|alligator|crocodile)\b",
    "the graphics": r"\b(graphics|realistic|looks? (so )?real|beautiful|4k)\b",
    "dying": r"\b(died|dead|die|wasted|killed)\b",
}


def prepare(pool, transcribe, workroot):
    """Download each clip and read what is said in it. Adds c['_src'] and c['_text']."""
    ready = []
    for i, c in enumerate(pool):
        wd = os.path.join(workroot, f"p{i}")
        os.makedirs(wd, exist_ok=True)
        try:
            if "_yt" in c:
                from . import youtube_source
                c["_src"] = youtube_source.fetch(c, wd)
            else:
                c["_src"] = render.download(c["url"], wd)
            try:
                words = transcribe(c["_src"], c["_streamer"].get("language", "en"))
                c["_text"] = " ".join(w["word"] for w in words)
            except Exception as e:  # noqa: BLE001
                print(f"  transcript failed: {e}")
                c["_text"] = ""
            ready.append(c)
        except Exception as e:  # noqa: BLE001
            print(f"  concept pool: could not get {c['url']}: {e}")
    return ready


def _ids(clips):
    return "combo_" + hashlib.sha1("|".join(sorted(c["id"] for c in clips)).encode()).hexdigest()[:12]


def _llm_plan(pool, want, kinds):
    lines = []
    for i, c in enumerate(pool):
        lines.append(f"[{i}] {c['_streamer']['name']} | {c['view_count']:,} views | clip title: "
                     f"{c.get('title', '')[:60]} | says: {c.get('_text', '')[:260]}")
    prompt = (
        "You plan multi-clip YouTube Shorts for a GTA 6 streamer-clips channel. Here are today's clips:\n"
        + "\n".join(lines) + "\n\n"
        f"Plan up to {want} Shorts using these formats ({', '.join(kinds)}):\n"
        "- versus: 2 or 3 clips from DIFFERENT streamers reacting to the same kind of moment or the same "
        "scene (e.g. both get arrested, both meet Lucia, both crash). Ends with 'who did it better?'.\n"
        "- ranking: 3 clips from different streamers sharing one theme (e.g. biggest rage, funniest NPC), "
        "listed weakest first, best last.\n"
        "- top3: 3 clips of the SAME streamer, weakest first, best last.\n"
        "Only group clips that really fit together; fewer good Shorts beat forced ones. Each clip once.\n"
        + select.TITLE_RULES.replace("start with the streamer's name, ", "name the streamers, ") + "\n"
        "For each Short also give 'labels': one short on-screen label per clip, in order "
        "(versus: the streamer's name; ranking/top3: '#3 NAME', '#2 NAME', '#1 NAME').\n"
        'Return ONLY JSON: {"shorts": [{"type": "versus", "clips": [0, 4], "hook": "...", "title": "...", '
        '"labels": ["...", "..."]}]}'
    )
    data = select.ask_json(prompt)
    out = []
    for sh in (data or {}).get("shorts", [])[:want]:
        try:
            idx = [int(i) for i in sh["clips"]]
            clips = [pool[i] for i in idx]
        except (KeyError, ValueError, IndexError, TypeError):
            continue
        kind = sh.get("type")
        names = [c["_streamer"]["login"] for c in clips]
        if kind not in kinds or len(set(idx)) != len(idx) or not 2 <= len(clips) <= 3:
            continue
        if kind in ("versus", "ranking") and len(set(names)) != len(names):
            continue
        if kind == "top3" and len(set(names)) != 1:
            continue
        labels = sh.get("labels") or []
        if len(labels) != len(clips):
            labels = [c["_streamer"]["name"] for c in clips]
        out.append({"type": kind, "clips": clips, "hook": select.clean_title(sh.get("hook", ""))[:60],
                    "title": (sh.get("title") or "").strip()[:95], "labels": labels})
    return out


def _fallback_plan(pool, want, kinds):
    out = []
    used = set()
    if "versus" in kinds:
        for topic, rx in TOPICS.items():
            if len(out) >= want:
                break
            best = {}
            for c in pool:
                if c["id"] in used:
                    continue
                if re.search(rx, f"{c.get('title', '')} {c.get('_text', '')}", re.I):
                    login = c["_streamer"]["login"]
                    if login not in best or c["view_count"] > best[login]["view_count"]:
                        best[login] = c
            if len(best) >= 2:
                clips = sorted(best.values(), key=lambda c: c["view_count"], reverse=True)[:3]
                names = [c["_streamer"]["name"] for c in clips]
                out.append({"type": "versus", "clips": clips,
                            "hook": f"{len(clips)} streamers vs {topic}",
                            "title": f"{' vs '.join(names)}: Who Handled {topic.title()} Better in GTA 6? 😭",
                            "labels": names})
                used |= {c["id"] for c in clips}
    if "top3" in kinds and len(out) < want:
        by = {}
        for c in pool:
            if c["id"] not in used:
                by.setdefault(c["_streamer"]["login"], []).append(c)
        for clips in sorted(by.values(), key=lambda v: -sum(c["view_count"] for c in v)):
            if len(out) >= want:
                break
            if len(clips) >= 3:
                clips = sorted(clips, key=lambda c: c["view_count"])[-3:]
                name = clips[0]["_streamer"]["name"]
                out.append({"type": "top3", "clips": clips, "hook": f"{name}'s 3 craziest GTA 6 moments",
                            "title": f"{name}'s 3 Craziest GTA 6 Moments So Far 😭💀",
                            "labels": [f"#{3 - i} {name}" for i in range(3)]})
                used |= {c["id"] for c in clips}
    return out


def plan(pool, cfg, state, want):
    kinds = list(cfg.get("concepts", {}).get("formats", ["versus", "ranking", "top3"]))
    if want <= 0 or len(pool) < 2:
        return []
    got = _llm_plan(pool, want, kinds)
    if not got:
        print("  concepts: no model answer, using keyword matching")
        got = _fallback_plan(pool, want, kinds)
    fresh = [g for g in got if _ids(g["clips"]) not in state["posted"]]
    for g in fresh:
        g["id"] = _ids(g["clips"])
        if not g["hook"]:
            g["hook"] = "same moment, different streamers" if g["type"] == "versus" else "wait for #1"
        if not g["title"]:
            g["title"] = g["hook"].title() + " in GTA 6 😭"
    return fresh


def _segment(src, out, seg_len):
    """The seg_len seconds of the clip that end just after its loudest reaction."""
    from .youtube_source import _loudest_second

    _, _, dur, _ = render.probe(src)
    loud = _loudest_second(src)
    end = min(dur, (loud + 3.0) if loud is not None else dur)
    end = max(end, min(dur, seg_len))
    start = max(0.0, end - seg_len)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.2f}", "-i", src, "-t", f"{seg_len:.2f}",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "17", "-c:a", "aac", "-b:a", "192k",
                    "-ar", "48000", out], check=True, timeout=600)
    return out


def render_concept(g, cfg, out_path, workdir, make_short):
    """Render every part full-screen with its label, then join them. Returns total seconds."""
    total = float(cfg.get("concepts", {}).get("max_seconds", 44))
    n = len(g["clips"])
    seg_len = min(20.0, total / n)
    parts = []
    for i, c in enumerate(g["clips"]):
        s = c["_streamer"]
        seg = _segment(c["_src"], os.path.join(workdir, f"seg{i}.mp4"), seg_len)
        part = os.path.join(workdir, f"part{i}.mp4")
        last = i == n - 1
        outro = ("who did it better?" if g["type"] == "versus" else None) if last else None
        make_short(seg, workdir, part, g["hook"] if i == 0 else None, select.credit_text(s), cfg,
                   layout="fill", disclosure="#ad" if (i == 0 and g.get("campaign")) else None,
                   language=s.get("language", "en"), label=g["labels"][i],
                   hook_until=2.5 if i == 0 else None, outro=outro)
        parts.append(part)
    lst = os.path.join(workdir, "parts.txt")
    with open(lst, "w") as f:
        f.write("".join(f"file '{os.path.abspath(p)}'\n" for p in parts))
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy",
                    "-movflags", "+faststart", out_path], check=True, timeout=600)
    return render.probe(out_path)[2]


def describe(g, cfg):
    seen, credits, links, tags = set(), [], [], ["#GTA6", "#Shorts"]
    for c in g["clips"]:
        s = c["_streamer"]
        if s["login"] not in seen:
            seen.add(s["login"])
            credits.append(f"{s['name']}: {select.source_link(s)}")
            tags += list(s.get("hashtags", []))[:2]
        links.append(c["url"])
    lines = []
    if g.get("campaign"):
        lines.append("#ad Clipped as part of the creator's official clipping program.")
    lines += ["All credit to the streamers. Watch them live:", *credits, "", "Original clips:", *links, "",
              f"Daily GTA 6 streamer moments on {cfg['channel']['name']}. Subscribe for more.",
              "Not affiliated with Rockstar Games or Take-Two Interactive.", "",
              " ".join(dict.fromkeys(tags))]
    names = [s.split(":")[0] for s in credits]
    yt_tags = ["GTA 6", "GTA VI", "GTA 6 streamers", "GTA 6 reactions", *names, *(f"{n} GTA 6" for n in names)]
    return "\n".join(lines), yt_tags


def summary_json(g):
    return json.dumps({"type": g["type"], "clips": [c["id"] for c in g["clips"]]})
