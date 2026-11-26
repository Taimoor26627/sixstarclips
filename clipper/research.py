"""Research + learning loop.

- our_stats: views/likes of every Short we posted (YouTube API through the Make webhook).
- market: for each streamer, the most-viewed GTA 6 Shorts other channels made about them:
  how big the demand is, and what those Shorts are about (keywords, titles).
- learn: turns both into per-streamer weights and "what this audience likes" keywords,
  saved in state/learn.json and used by the next batch.
"""
import json
import math
import os
import re
import statistics
from collections import Counter
from datetime import timedelta
from urllib.parse import quote

import requests

from .select import now_utc, parse_time

LEARN_PATH = os.path.join("state", "learn.json")
STOP = set("""a an the and or of to in on for at by is it this that with from as be are was were
his her their he she they you your i me my we our us vs vs. not no yes so just get got gets
gta gta6 gta vi 6 grand theft auto shorts short live stream streamer clip clips moment moments
funny best new full part ep episode day when what how why who its it's im i'm""".split())


def load_learn():
    if os.path.exists(LEARN_PATH):
        with open(LEARN_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"streamers": {}, "last_stats": None, "last_market": None}


def save_learn(data):
    os.makedirs("state", exist_ok=True)
    with open(LEARN_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)


def api(path):
    """Read-only YouTube Data API call via the Make scenario's 'api' route."""
    url = os.environ.get("MAKE_WEBHOOK_URL")
    if not url:
        raise RuntimeError("MAKE_WEBHOOK_URL not set")
    r = requests.post(url, json={"action": "api", "path": path}, timeout=120)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, str):
        data = json.loads(data)
    return data


def _videos(ids):
    out = {}
    ids = [i for i in ids if i]
    for i in range(0, len(ids), 50):
        chunk = ",".join(ids[i:i + 50])
        data = api(f"/v3/videos?part=statistics,snippet,status&id={chunk}")
        for v in data.get("items", []):
            st = v.get("statistics", {})
            out[v["id"]] = {
                "views": int(st.get("viewCount", 0)),
                "likes": int(st.get("likeCount", 0)),
                "comments": int(st.get("commentCount", 0)),
                "title": v.get("snippet", {}).get("title", ""),
                "channel": v.get("snippet", {}).get("channelTitle", ""),
                "privacy": v.get("status", {}).get("privacyStatus", ""),
            }
    return out


def our_stats(state):
    ids = [v["youtube_id"] for v in state["posted"].values()
           if v.get("youtube_id") and v["youtube_id"] != "QUEUED" and not v.get("old_channel")]
    stats = _videos(ids)
    now = now_utc().isoformat()
    for v in state["posted"].values():
        s = stats.get(v.get("youtube_id"))
        if s:
            v["stats"] = {k: s[k] for k in ("views", "likes", "comments", "privacy")}
            v["stats_at"] = now
    return stats


def keywords(titles, name):
    skip = STOP | set(re.findall(r"[a-z0-9]+", name.lower()))
    words = Counter()
    for t in titles:
        seen = set()
        for w in re.findall(r"[a-z][a-z']{2,}", t.lower()):
            if w not in skip and w not in seen and not (len(w) >= 4 and w in name.lower().replace(" ", "")):
                words[w] += 1
                seen.add(w)
    return [w for w, n in words.most_common(10) if n >= 2]


def market_all(streamers, since_days=30):
    """Top GTA 6 Shorts about each streamer on YouTube, by views.
    One search per streamer + one batched stats call per 50 videos (saves Make operations)."""
    after = (now_utc() - timedelta(days=since_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    found = {}
    for s in streamers:
        q = quote(f"{s['name']} GTA 6")
        try:
            data = api(f"/v3/search?part=snippet&type=video&videoDuration=short&order=viewCount"
                       f"&maxResults=15&publishedAfter={after}&q={q}")
        except Exception as e:  # noqa: BLE001
            print(f"  market search failed for {s['name']}: {e}")
            continue
        found[s["login"]] = [it["id"]["videoId"] for it in data.get("items", [])
                             if it.get("id", {}).get("videoId")]
    vids = _videos(sorted({i for ids in found.values() for i in ids}))
    out = {}
    for s in streamers:
        if s["login"] not in found:
            continue
        rows = sorted((vids[i] for i in found[s["login"]] if i in vids), key=lambda v: v["views"], reverse=True)
        views = [r["views"] for r in rows] or [0]
        out[s["login"]] = {
            "median_views": int(statistics.median(views)),
            "top_views": views[0],
            "top_titles": [f"{r['title']} ({r['views']:,} views, {r['channel']})" for r in rows[:6]],
            "keywords": keywords([r["title"] for r in rows[:15]], s["name"]),
        }
    return out


def learn(cfg, state, do_market=False):
    """Update weights. Returns a short plain-text summary for Telegram."""
    data = load_learn()
    lines = []
    enabled = [s for s in cfg["streamers"] if s.get("enabled")]

    if do_market:
        for login, m in market_all(enabled).items():
            data["streamers"].setdefault(login, {}).update(m)
        data["last_market"] = now_utc().isoformat()

    try:
        our_stats(state)
        data["last_stats"] = now_utc().isoformat()
    except Exception as e:  # noqa: BLE001
        print(f"  could not read our video stats: {e}")

    # Our own results per streamer (only Shorts live for 24h+ count)
    cutoff = now_utc() - timedelta(hours=24)
    ours = {}
    for v in state["posted"].values():
        live_at = v.get("publish_at") or v.get("at")
        if v.get("stats") and not v.get("old_channel") and live_at and parse_time(live_at) <= cutoff:
            ours.setdefault(v["login"], []).append(v["stats"]["views"])

    demand = {k: d.get("median_views", 0) for k, d in data["streamers"].items()}
    top_demand = max(demand.values() or [1]) or 1
    all_ours = [x for xs in ours.values() for x in xs]
    our_avg = (sum(all_ours) / len(all_ours)) if all_ours else 0

    for s in enabled:
        d = data["streamers"].setdefault(s["login"], {})
        w = 1.0
        if demand.get(s["login"]):
            # 0.6x .. 1.6x by audience demand (log scale)
            w *= 0.6 + math.log10(1 + 9 * demand[s["login"]] / top_demand)
        mine = ours.get(s["login"])
        if mine and our_avg:
            avg = sum(mine) / len(mine)
            w *= max(0.5, min(2.0, (avg + 50) / (our_avg + 50)))
            d["our_avg_views"] = int(avg)
            d["our_posts"] = len(mine)
        d["weight"] = round(max(0.3, min(3.0, w)), 2)
    save_learn(data)

    from . import youtube_source

    usable = [s for s in enabled if s.get("platform", "twitch") == "twitch" or youtube_source.available()]
    ranked = sorted(usable, key=lambda s: data["streamers"][s["login"]]["weight"], reverse=True)
    if all_ours:
        best = max((v for v in state["posted"].values() if v.get("stats") and not v.get("old_channel")), key=lambda v: v["stats"]["views"])
        lines.append(f"Your Shorts so far: {len(all_ours)} live 24h+, average {int(our_avg):,} views. "
                     f"Best: \"{best['title']}\" ({best['stats']['views']:,}).")
    lines.append("Priority now: " + ", ".join(f"{s['name']} x{data['streamers'][s['login']]['weight']}"
                                             for s in ranked[:6]))
    likes = {s["name"]: data["streamers"][s["login"]].get("keywords", [])[:4] for s in ranked[:4]}
    lines += [f"{n} audience likes: {', '.join(k)}" for n, k in likes.items() if k]
    write_reports(cfg, state, data)
    return "\n".join(lines)


def write_reports(cfg, state, data):
    os.makedirs("reports", exist_ok=True)
    rows = sorted((v for v in state["posted"].values() if v.get("stats") and not v.get("old_channel")),
                  key=lambda v: v["stats"]["views"], reverse=True)
    perf = ["# Our Shorts performance", "", "| Views | Likes | Streamer | Title | Video |",
            "| --- | --- | --- | --- | --- |"]
    perf += [f"| {v['stats']['views']:,} | {v['stats']['likes']:,} | {v['login']} | {v['title']} | "
             f"https://youtube.com/shorts/{v['youtube_id']} |" for v in rows]
    with open(os.path.join("reports", "performance.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(perf) + "\n")
    res = ["# Audience research (top GTA 6 Shorts about each streamer)", ""]
    for s in cfg["streamers"]:
        d = data["streamers"].get(s["login"])
        if not d or not s.get("enabled"):
            continue
        res += [f"## {s['name']} (weight x{d.get('weight', 1)})",
                f"- Median views of top Shorts: {d.get('median_views', 0):,}; best: {d.get('top_views', 0):,}",
                f"- What works: {', '.join(d.get('keywords', [])) or 'n/a'}",
                f"- Our average: {d.get('our_avg_views', 'n/a')} over {d.get('our_posts', 0)} posts", ""]
        res += [f"  - {t}" for t in d.get("top_titles", [])] + [""]
    with open(os.path.join("reports", "research.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(res) + "\n")


def due(data, key, hours):
    last = data.get(key)
    return not last or (now_utc() - parse_time(last)).total_seconds() > hours * 3600


_CACHE = {}


def _cached():
    m = os.path.getmtime(LEARN_PATH) if os.path.exists(LEARN_PATH) else 0
    if _CACHE.get("m") != m:
        _CACHE.update(m=m, data=load_learn())
    return _CACHE["data"]


def weight(login):
    return _cached().get("streamers", {}).get(login, {}).get("weight", 1.0)


def audience(login):
    d = _cached().get("streamers", {}).get(login, {})
    return d.get("keywords", []), d.get("top_titles", [])
