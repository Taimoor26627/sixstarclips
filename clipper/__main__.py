"""Entry point.

  python -m clipper run [--max-posts N] [--dry-run]   find, render and publish clips
  python -m clipper learn                             read our views + research audiences
  python -m clipper report                            latest stream numbers -> reports/streamers.md
  python -m clipper render-test VIDEO "Hook text"     render a local file (no Twitch, no upload)
"""
import argparse
import os
import shutil
import sys
import tempfile
import traceback
from datetime import timedelta

import yaml

from . import captions, render, select, state as st

OUT_DIR = "out"
FONTS = os.path.join("assets", "fonts")


def load_cfg(path="config.yaml"):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def env(name):
    v = os.environ.get(name)
    if not v:
        sys.exit(f"Missing secret/env var {name}. See README > Setup.")
    return v


def summary(md):
    print(md)
    p = os.environ.get("GITHUB_STEP_SUMMARY")
    if p:
        with open(p, "a", encoding="utf-8") as f:
            f.write(md + "\n")


def make_short(src, workdir, out_path, hook, credit, cfg, layout="blur", facecam=None,
               disclosure=None, language="en"):
    """Captions + render. Returns (duration, transcript_text)."""
    _, _, dur, has_audio = render.probe(src)
    dur = min(dur, float(cfg.get("render", {}).get("max_seconds", 59)))
    words = []
    if cfg.get("captions", {}).get("enabled", True) and has_audio:
        try:
            words = captions.transcribe(src, cfg["captions"].get("model", "small"), language)
        except Exception as e:  # noqa: BLE001
            print(f"  captions skipped: {e}")
    ass = captions.build_ass(
        words, hook, credit, dur, layout=layout if facecam else "blur",
        censor_words=cfg.get("captions", {}).get("censor_profanity", True),
        disclosure=disclosure,
    )
    ass_path = os.path.join(workdir, "subs.ass")
    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(ass)
    render.render(src, ass_path, out_path, cfg, layout=layout, facecam=facecam, fonts_dir=FONTS)
    return dur, " ".join(w["word"] for w in words)


def _tz(cfg):
    from datetime import timezone as _t
    return _t(timedelta(hours=cfg.get("schedule", {}).get("utc_offset_hours", 5)))


def next_slots(cfg, state, count):
    """The next free go-live times (UTC ISO strings), at least 30 min from now."""
    from datetime import datetime
    tz = _tz(cfg)
    now_local = datetime.now(tz)
    taken = st.taken_slots(state)
    out, day = [], 0
    while len(out) < count and day < 7:
        date = (now_local + timedelta(days=day)).date()
        for hhmm in sorted(cfg["schedule"]["slots"]):
            h, m = (int(x) for x in hhmm.split(":"))
            t = datetime(date.year, date.month, date.day, h, m, tzinfo=tz)
            iso = t.astimezone(select.now_utc().tzinfo).strftime("%Y-%m-%dT%H:%M:%SZ")
            if t > now_local + timedelta(minutes=30) and iso not in taken and iso not in out:
                out.append(iso)
        day += 1
    return sorted(out)[:count]


def immediate_time():
    """A time just in the past: YouTube publishes it straight away."""
    return (select.now_utc() - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_label(iso, cfg):
    t = select.parse_time(iso).astimezone(_tz(cfg))
    return t.strftime("%a %d %b, %I:%M %p").replace(" 0", " ")


def send_plan(done, cfg):
    """One Telegram message: what goes live when, and what you need to do."""
    from .publish import telegram_send, telegram_text

    handle = cfg["channel"]["handle"]
    lines = [f"{len(done)} new Short{'s' if len(done) != 1 else ''} made and scheduled on YouTube {handle}:", ""]
    todo = []
    for i, d in enumerate(sorted(done, key=lambda x: x["publish_at"]), 1):
        when = local_label(d["publish_at"], cfg)
        ok = d["yt_id"] and d["yt_id"] != "QUEUED"
        link = f"https://youtube.com/shorts/{d['yt_id']}" if ok else "NOT UPLOADED - file sent below"
        lines.append(f"{i}. {when} - {d['title']}\n   {link}")
        if d["s"].get("campaign"):
            lines.append(f"   SUBMIT to the {d['s']['name']} GTA 6 campaign on Content Rewards at {when} "
                         "(within 30 min of going live)")
            todo.append(f"{when}: {d['s']['name']} campaign")
        if not ok:
            telegram_send(d["out"], d["title"])
            telegram_text(f"Upload manually:\nTITLE:\n{d['title']}\n\nDESCRIPTION:\n{d['desc']}")
    lines += ["", "YOUR TO-DO: " + ("; ".join(todo) if todo else "nothing, all automatic.")]
    telegram_text("\n".join(lines))


def cmd_alerts(args):
    """Ping Telegram when a scheduled Short has just gone live."""
    cfg = load_cfg(args.config)
    state = st.load()
    now = select.now_utc()
    changed = False
    for v in state["posted"].values():
        pa = v.get("publish_at")
        if not pa or v.get("alerted") or not v.get("youtube_id") or v["youtube_id"] == "QUEUED":
            continue
        t = select.parse_time(pa)
        if t <= now <= t + timedelta(hours=6):
            msg = f"LIVE NOW: {v['title']}\nhttps://youtube.com/shorts/{v['youtube_id']}"
            if v.get("campaign"):
                msg += "\n\nSubmit this link to the campaign on Content Rewards now (within 30 min)."
            if os.environ.get("TELEGRAM_BOT_TOKEN"):
                from .publish import telegram_text
                telegram_text(msg)
            print(msg)
            v["alerted"] = True
            changed = True
        elif now > t + timedelta(hours=6):
            v["alerted"] = True
            changed = True
    if changed:
        st.save(state)


def cmd_learn(args, force=False):
    """Read our Shorts' views + research each streamer's audience, then re-weight selection."""
    from . import research

    cfg = load_cfg(args.config)
    rc = cfg.get("research", {})
    if not os.environ.get("MAKE_WEBHOOK_URL"):
        print("Learning skipped: MAKE_WEBHOOK_URL not set.")
        return None
    data = research.load_learn()
    force_stats = force or getattr(args, "stats", False)
    do_stats = force_stats or research.due(data, "last_stats", rc.get("stats_every_hours", 44))
    do_market = force or research.due(data, "last_market", 24 * rc.get("market_every_days", 10))
    if not (do_stats or do_market):
        print("Learning: data is fresh, nothing to fetch.")
        return None
    state = st.load()
    try:
        text = research.learn(cfg, state, do_market=do_market)
    except Exception:  # noqa: BLE001 - learning must never stop clips being made
        traceback.print_exc()
        return None
    st.save(state)
    print(text)
    summary("### What the bot learned\n" + text.replace("\n", "  \n"))
    if cfg["publish"].get("telegram") and os.environ.get("TELEGRAM_BOT_TOKEN"):
        from .publish import telegram_text
        try:
            telegram_text("SIX STAR CLIPS - what's working\n\n" + text)
        except Exception as e:  # noqa: BLE001
            print(f"Telegram failed: {e}")
    return text


def cmd_tick(args):
    """Scheduled run: send live alerts, learn from results, then top the schedule up to a full day."""
    cmd_alerts(args)
    cmd_learn(args)
    cfg = load_cfg(args.config)
    state = st.load()
    now = select.now_utc()
    ahead = sum(1 for v in state["posted"].values()
                if v.get("publish_at") and select.parse_time(v["publish_at"]) > now)
    want = cfg.get("schedule", {}).get("per_day", 6)
    if ahead >= want:
        print(f"{ahead} Shorts already scheduled ahead; nothing to make.")
        return
    print(f"Only {ahead} scheduled ahead; making {want - ahead} more.")
    args.batch, args.max_posts, args.dry_run = True, want - ahead, False
    cmd_run(args)


def checklist(s, cfg, yt_id):
    """What (if anything) you need to do for this clip. YouTube only."""
    handle = cfg["channel"]["handle"]
    if yt_id == "QUEUED":
        lines = [f"Sent to Make for YouTube {handle}, but no video link came back. Check the channel in "
                 "10 minutes: if it isn't there, upload the video above with the TITLE and DESCRIPTION below."]
    elif yt_id:
        lines = [f"Posted on YouTube {handle} automatically: https://youtube.com/shorts/{yt_id}"]
    else:
        lines = [f"Auto-post did not happen. Upload this video to YouTube {handle} "
                 "and paste the TITLE and DESCRIPTION below."]
    if s.get("campaign"):
        lines.append(f"TO DO: submit the YouTube link to the {s['name']} GTA 6 campaign on "
                     "Content Rewards within 30 minutes.")
    else:
        lines.append("Nothing else to do.")
    return "\n".join(lines)


def twitch_candidates(streamers, cfg, state, usable, since, now, n):
    sel = cfg["selection"]
    out = []
    if not streamers:
        return out
    if os.environ.get("TWITCH_CLIENT_ID") and os.environ.get("TWITCH_CLIENT_SECRET"):
        from .twitch import Twitch

        tw = Twitch(os.environ["TWITCH_CLIENT_ID"], os.environ["TWITCH_CLIENT_SECRET"])
        users = tw.users(s["login"] for s in streamers)
        game = tw.game_id(cfg["game_name"])
        if not game:
            print(f"WARNING: Twitch category '{cfg['game_name']}' not found; clips are not filtered by game.")
        started, ended = since.strftime("%Y-%m-%dT%H:%M:%SZ"), now.strftime("%Y-%m-%dT%H:%M:%SZ")
        for s in streamers:
            u = users.get(s["login"].lower())
            if not u:
                print(f"Skipping {s['login']}: Twitch user not found")
                continue
            if st.posted_today(state, s["login"]) >= sel["max_per_streamer_per_day"]:
                continue
            for c in tw.clips(u["id"], started, ended):
                if (not game or c.get("game_id") == game) and usable(c):
                    c["_streamer"] = s
                    out.append(c)
        return out

    from . import keyless

    print("Twitch: no keys set, reading each streamer's public clips page.")
    for s in streamers:
        if st.posted_today(state, s["login"]) >= sel["max_per_streamer_per_day"]:
            continue
        for c in keyless.list_clips(s["login"]):
            if usable(c):
                c["_streamer"] = s
                out.append(c)
    # The clips page doesn't say which game a clip is from, so check the best few one by one.
    ranked = sorted(out, key=lambda c: select.score(c, now), reverse=True)
    checked, want, tries, per = [], max(n * 4, 8), 0, {}
    for c in ranked:
        if len(checked) >= want or tries >= sel.get("max_game_checks", 60):
            break
        login = c["_streamer"]["login"]
        if per.get(login, 0) >= 3:      # keep a mix of streamers, not just the biggest two
            continue
        tries += 1
        g = keyless.clip_game(c["url"])
        if g and g.strip().lower() == cfg["game_name"].lower():
            checked.append(c)
            per[login] = per.get(login, 0) + 1
        else:
            print(f"  skip (game: {g}): {c['title'][:60]}")
    return checked


def youtube_candidates(streamers, cfg, state):
    from . import youtube_source

    sel = cfg["selection"]
    out = []
    if streamers and not youtube_source.available():
        print("YouTube streamers skipped: YouTube blocks GitHub's servers without the YT_COOKIES secret.")
        return out
    for s in streamers:
        if st.posted_today(state, s["login"]) >= sel["max_per_streamer_per_day"]:
            continue
        for c in youtube_source.candidates(s, cfg, state, sel.get("youtube_lookback_hours", 72)):
            if not select.has_spoiler(c["title"], cfg.get("spoiler_words", [])):
                c["_streamer"] = s
                out.append(c)
    return out


def queued_clips(cfg, state):
    """Clips you asked for by link (config 'queue'), posted before anything else."""
    import yt_dlp

    by_login = {s["login"]: s for s in cfg["streamers"]}
    out = []
    for item in cfg.get("queue") or []:
        s = by_login.get(item.get("login"))
        if not s:
            print(f"Queue: unknown streamer {item.get('login')}")
            continue
        try:
            with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True}) as ydl:
                info = ydl.extract_info(item["url"], download=False) or {}
        except Exception as e:  # noqa: BLE001
            print(f"Queue: could not read {item['url']}: {e}")
            continue
        cid = str(info.get("id") or item["url"])
        done = state["posted"].get(cid)
        if done and done.get("youtube_id"):
            continue  # already on YouTube
        out.append({
            "id": cid, "url": item["url"], "title": item.get("title") or info.get("title") or "",
            "view_count": int(info.get("view_count") or 0),
            "created_at": select.now_utc().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "duration": float(info.get("duration") or 0), "_streamer": s, "_queued": True,
        })
    if out:
        print(f"Queue: {len(out)} clip(s) waiting, posting first")
    return out


def cmd_run(args):
    cfg = load_cfg(args.config)
    if cfg.get("paused") and not args.dry_run:
        summary("Paused (config.yaml 'paused: true'): no clips made or uploaded.")
        return
    select.HALF_LIFE_H = float(cfg["selection"].get("half_life_hours", 168))
    state = st.load()
    streamers = [s for s in cfg["streamers"] if s.get("enabled")]
    sel = cfg["selection"]
    now = select.now_utc()
    n = args.max_posts or (cfg.get("schedule", {}).get("per_day", 6) if args.batch
                           else sel.get("posts_per_run", 1))
    since = now - timedelta(hours=sel["lookback_hours"])

    reasons = {}

    def usable(c):
        why = None
        if c["id"] in state["posted"]:
            why = "already posted"
        elif c["view_count"] < sel["min_views"]:
            why = "too few views"
        elif c["duration"] and c["duration"] < sel["min_duration"]:
            why = "too short"
        elif select.parse_time(c["created_at"]) < since:
            why = "too old"
        elif select.has_spoiler(c["title"], cfg.get("spoiler_words", [])):
            why = "spoiler words"
        if why:
            reasons[why] = reasons.get(why, 0) + 1
        return why is None

    tw_streamers = [s for s in streamers if s.get("platform", "twitch") == "twitch"]
    yt_streamers = [s for s in streamers if s.get("platform") == "youtube"]
    candidates = twitch_candidates(tw_streamers, cfg, state, usable, since, now, n)
    try:
        candidates += youtube_candidates(yt_streamers, cfg, state)
    except Exception:  # noqa: BLE001 - YouTube trouble must not stop Twitch clips
        traceback.print_exc()
    print(f"{len(candidates)} candidate clips from {len(streamers)} streamers; skipped: {reasons}")

    queued = queued_clips(cfg, state)
    chosen = queued[:n] + select.pick([c for c in candidates if c["id"] not in {q["id"] for q in queued}],
                                      max(0, n - len(queued)), now)
    if not chosen:
        summary("No new clips met the rules this run.")
        return

    os.makedirs(OUT_DIR, exist_ok=True)
    pub = cfg["publish"]
    rows, done = [], []
    slots = next_slots(cfg, state, len(chosen)) if args.batch else []
    for c in chosen:
        s = c["_streamer"]
        lang = s.get("language", "en")
        credit = select.credit_text(s)
        layout = "split" if s.get("facecam") else "blur"
        disclosure = "#ad" if s.get("campaign") else None
        print(f"\n> {s['name']}: '{c['title'][:70]}' ({c['view_count']:,} views, "
              f"score {select.score(c, now):,.0f}) {c['url']}")
        workdir = tempfile.mkdtemp(prefix="clip_")
        try:
            if "_yt" in c:
                from . import youtube_source
                src = youtube_source.fetch(c, workdir)
            else:
                src = render.download(c["url"], workdir)
            out_path = os.path.join(OUT_DIR, f"{s['login']}_{c['id'][-24:]}.mp4")
            # Draft hook from the clip title; refine with transcript if Claude is on.
            recent = set() if c.get("_queued") else st.recent_openers(state)
            hook, title = select.make_hook(c, s, {**cfg, "hooks": {**cfg["hooks"], "use_claude": False}}, recent)
            dur, transcript = make_short(src, workdir, out_path, hook, credit, cfg, layout=layout,
                                         facecam=s.get("facecam"), disclosure=disclosure, language=lang)
            if transcript and not c.get("_queued"):
                # Now that we know what was said: better hook (Claude if set up, else the best quote).
                h2, t2 = select.make_hook(c, s, cfg, st.recent_openers(state), transcript)
                if h2 != hook:
                    hook, title = h2, t2
                    make_short(src, workdir, out_path, hook, credit, cfg, layout=layout,
                               facecam=s.get("facecam"), disclosure=disclosure, language=lang)
            desc, tags = select.describe(c, s, cfg)

            publish_at = slots.pop(0) if slots else immediate_time()
            yt_id = None
            if not args.dry_run:
                if pub.get("youtube"):
                    from .publish import youtube_upload
                    yt_id = youtube_upload(out_path, title, desc, tags, "private",
                                           paid=bool(s.get("campaign")), publish_at=publish_at)
                elif pub.get("make") and os.environ.get("MAKE_WEBHOOK_URL"):
                    from .publish import make_upload
                    small = out_path
                    if os.path.getsize(out_path) > 4_700_000:
                        small = out_path.replace(".mp4", "_small.mp4")
                        size = render.export_small(out_path, small)
                        print(f"  light copy for Make: {size / 1e6:.1f} MB")
                    yt_id = make_upload(small, title, desc, tags, publish_at)
                print(f"  scheduled for {local_label(publish_at, cfg)}: {yt_id}")
                st.record(state, c, s["login"], hook, title, yt_id, publish_at, bool(s.get("campaign")))
                st.save(state)
            done.append({"s": s, "title": title, "desc": desc, "yt_id": yt_id, "publish_at": publish_at,
                         "out": out_path})
            rows.append(f"| {local_label(publish_at, cfg)} | {s['name']} | {title} | {dur:.0f}s | "
                        f"{('https://youtube.com/shorts/' + yt_id) if yt_id and yt_id != 'QUEUED' else (yt_id or 'rendered')} |")
        except Exception:  # noqa: BLE001 - one bad clip must not stop the rest
            traceback.print_exc()
            rows.append(f"| - | {s['name']} | FAILED: {c['url']} | - | - |")
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    if done and pub.get("telegram") and not args.dry_run and os.environ.get("TELEGRAM_BOT_TOKEN"):
        send_plan(done, cfg)
    summary("| Goes live | Streamer | Title | Length | Video |\n| --- | --- | --- | --- | --- |\n"
            + "\n".join(rows))


def cmd_report(args):
    cfg = load_cfg(args.config)
    if not (os.environ.get("TWITCH_CLIENT_ID") and os.environ.get("TWITCH_CLIENT_SECRET")):
        return cmd_report_keyless(cfg)
    from .twitch import Twitch

    tw = Twitch(env("TWITCH_CLIENT_ID"), env("TWITCH_CLIENT_SECRET"))
    users = tw.users(s["login"] for s in cfg["streamers"] if s.get("platform", "twitch") == "twitch")
    live = tw.live_streams(u["id"] for u in users.values())
    lines = [
        f"# Streamer numbers ({select.now_utc():%Y-%m-%d %H:%M} UTC)",
        "",
        "| Streamer | Live now | Viewers now | Game now | Last VOD date | Last VOD views | Last VOD length | Enabled |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for s in cfg["streamers"]:
        if s.get("platform") == "youtube":
            lines.append(youtube_report_row(s, 8))
            continue
        u = users.get(s["login"].lower())
        if not u:
            lines.append(f"| {s['name']} | user not found | | | | | | {s.get('enabled')} |")
            continue
        l = live.get(u["id"])
        vod = tw.last_vod(u["id"])
        lines.append(
            f"| {s['name']} | {'yes' if l else 'no'} | {l['viewer_count'] if l else ''} | "
            f"{l['game_name'] if l else ''} | {vod['created_at'][:10] if vod else ''} | "
            f"{vod['view_count'] if vod else ''} | {vod['duration'] if vod else ''} | {s.get('enabled')} |"
        )
    os.makedirs("reports", exist_ok=True)
    with open(os.path.join("reports", "streamers.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    summary("\n".join(lines))


def youtube_report_row(s, cols):
    from . import youtube_source

    items = youtube_source.recent_streams(s, max_items=1)
    if not items:
        return f"| {s['name']} (YouTube) | not found |" + " |" * (cols - 2)
    v = items[0]
    title = (v.get("title") or "").replace("|", "/")[:60]
    cells = [f"{s['name']} (YouTube)", title, str(v.get("view_count") or ""),
             f"{(v.get('duration') or 0) / 3600:.1f}"]
    cells += [""] * (cols - len(cells) - 1) + [str(s.get("enabled"))]
    return "| " + " | ".join(cells) + " |"


def cmd_report_keyless(cfg):
    from . import keyless

    lines = [
        f"# Streamer numbers ({select.now_utc():%Y-%m-%d %H:%M} UTC, no-key mode)",
        "",
        "| Streamer | Last broadcast | Views | Length (h) | Enabled |",
        "| --- | --- | --- | --- | --- |",
    ]
    for s in cfg["streamers"]:
        if s.get("platform") == "youtube":
            lines.append(youtube_report_row(s, 5))
            continue
        v = keyless.last_vod(s["login"])
        if not v:
            lines.append(f"| {s['name']} | not found | | | {s.get('enabled')} |")
            continue
        title = (v.get("title") or "").replace("|", "/")[:60]
        hours = f"{(v.get('duration') or 0) / 3600:.1f}"
        lines.append(f"| {s['name']} | {title} | {v.get('view_count') or ''} | {hours} | {s.get('enabled')} |")
    os.makedirs("reports", exist_ok=True)
    with open(os.path.join("reports", "streamers.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    summary("\n".join(lines))


def cmd_render_test(args):
    cfg = load_cfg(args.config)
    os.makedirs(OUT_DIR, exist_ok=True)
    workdir = tempfile.mkdtemp(prefix="test_")
    out = os.path.join(OUT_DIR, "render_test.mp4")
    facecam = None
    if args.facecam:
        x, y, w, h = (int(v) for v in args.facecam.split(","))
        facecam = {"x": x, "y": y, "w": w, "h": h}
    make_short(args.video, workdir, out, args.hook, "twitch.tv/streamer", cfg,
               layout="split" if facecam else "blur", facecam=facecam, disclosure="#ad")
    print(f"Wrote {out}")


def main():
    p = argparse.ArgumentParser(prog="clipper")
    p.add_argument("--config", default="config.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--max-posts", type=int, default=0)
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--batch", action="store_true", help="make the whole day's clips and schedule them")
    sub.add_parser("report")
    sub.add_parser("alerts")
    sub.add_parser("tick")
    lp = sub.add_parser("learn")
    lp.add_argument("--stats", action="store_true", help="re-read our views now")
    t = sub.add_parser("render-test")
    t.add_argument("video")
    t.add_argument("hook")
    t.add_argument("--facecam", help="x,y,w,h in 1920x1080 source")
    args = p.parse_args()
    {"run": cmd_run, "report": cmd_report, "alerts": cmd_alerts, "tick": cmd_tick,
     "learn": lambda a: cmd_learn(a), "render-test": cmd_render_test}[args.cmd](args)


if __name__ == "__main__":
    main()
