"""Entry point.

  python -m clipper run [--max-posts N] [--dry-run]   find, render and publish clips
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


def make_short(src, workdir, out_path, hook, credit, cfg, layout="blur", facecam=None, disclosure=None):
    """Captions + render. Returns (duration, transcript_text)."""
    _, _, dur, has_audio = render.probe(src)
    dur = min(dur, float(cfg.get("render", {}).get("max_seconds", 59)))
    words = []
    if cfg.get("captions", {}).get("enabled", True) and has_audio:
        try:
            words = captions.transcribe(src, cfg["captions"].get("model", "small"))
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


def cmd_run(args):
    cfg = load_cfg(args.config)
    from .twitch import Twitch

    tw = Twitch(env("TWITCH_CLIENT_ID"), env("TWITCH_CLIENT_SECRET"))
    state = st.load()
    streamers = [s for s in cfg["streamers"] if s.get("enabled")]
    users = tw.users(s["login"] for s in streamers)
    game = tw.game_id(cfg["game_name"])
    if not game:
        print(f"WARNING: Twitch category '{cfg['game_name']}' not found; clips are not filtered by game.")

    sel = cfg["selection"]
    now = select.now_utc()
    started = (now - timedelta(hours=sel["lookback_hours"])).strftime("%Y-%m-%dT%H:%M:%SZ")
    ended = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    candidates = []
    for s in streamers:
        u = users.get(s["login"].lower())
        if not u:
            print(f"Skipping {s['login']}: Twitch user not found")
            continue
        if st.posted_today(state, s["login"]) >= sel["max_per_streamer_per_day"]:
            continue
        for c in tw.clips(u["id"], started, ended):
            if game and c.get("game_id") != game:
                continue
            if c["id"] in state["posted"] or c["view_count"] < sel["min_views"]:
                continue
            if c["duration"] < sel["min_duration"]:
                continue
            if select.has_spoiler(c["title"], cfg.get("spoiler_words", [])):
                continue
            c["_streamer"] = s
            candidates.append(c)
    print(f"{len(candidates)} candidate clips from {len(streamers)} streamers")

    n = args.max_posts or sel.get("posts_per_run", 1)
    chosen = select.pick(candidates, n, now)
    if not chosen:
        summary("No new clips met the rules this run.")
        return

    os.makedirs(OUT_DIR, exist_ok=True)
    pub = cfg["publish"]
    rows = []
    for c in chosen:
        s = c["_streamer"]
        print(f"\n> {s['name']}: '{c['title']}' ({c['view_count']} views) {c['url']}")
        workdir = tempfile.mkdtemp(prefix="clip_")
        try:
            src = render.download(c["url"], workdir)
            out_path = os.path.join(OUT_DIR, f"{s['login']}_{c['id'][:24]}.mp4")
            # Draft hook from the clip title; refine with transcript if Claude is on.
            hook, title = select.make_hook(c, s, {**cfg, "hooks": {**cfg["hooks"], "use_claude": False}},
                                           st.recent_openers(state))
            dur, transcript = make_short(
                src, workdir, out_path, hook, f"twitch.tv/{s['login']}", cfg,
                layout="split" if s.get("facecam") else "blur", facecam=s.get("facecam"),
                disclosure="#ad" if s.get("campaign") else None,
            )
            if cfg["hooks"].get("use_claude") and transcript:
                h2, t2 = select.make_hook(c, s, cfg, st.recent_openers(state), transcript)
                if h2 != hook:
                    hook, title = h2, t2
                    make_short(src, workdir, out_path, hook, f"twitch.tv/{s['login']}", cfg,
                               layout="split" if s.get("facecam") else "blur", facecam=s.get("facecam"),
                               disclosure="#ad" if s.get("campaign") else None)
            desc, tags = select.describe(c, s, cfg)

            yt_id = None
            if pub.get("youtube") and not args.dry_run:
                from .publish import youtube_upload
                yt_id = youtube_upload(out_path, title, desc, tags, pub.get("privacy", "public"),
                                       paid=bool(s.get("campaign")))
                print(f"  uploaded: https://youtube.com/shorts/{yt_id}")
            if pub.get("telegram") and not args.dry_run and os.environ.get("TELEGRAM_BOT_TOKEN"):
                from .publish import telegram_send, telegram_text
                telegram_send(out_path, f"{title}\n\n{s['name']} | {c['view_count']} views on Twitch")
                extra = f"\nPosted: https://youtube.com/shorts/{yt_id}" if yt_id else ""
                telegram_text(f"TITLE:\n{title}\n\nDESCRIPTION:\n{desc}{extra}"
                              + ("\n\nSubmit this clip to the campaign within 30 min." if s.get("campaign") else ""))
            if not args.dry_run:
                st.record(state, c, s["login"], hook, title, yt_id)
                st.save(state)
            rows.append(f"| {s['name']} | {title} | {c['view_count']} | {dur:.0f}s | "
                        f"{'https://youtube.com/shorts/' + yt_id if yt_id else 'rendered'} |")
        except Exception:  # noqa: BLE001 - one bad clip must not stop the rest
            traceback.print_exc()
            rows.append(f"| {s['name']} | FAILED: {c['url']} | {c['view_count']} | - | - |")
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    summary("| Streamer | Title | Twitch views | Length | Result |\n| --- | --- | --- | --- | --- |\n"
            + "\n".join(rows))


def cmd_report(args):
    cfg = load_cfg(args.config)
    from .twitch import Twitch

    tw = Twitch(env("TWITCH_CLIENT_ID"), env("TWITCH_CLIENT_SECRET"))
    users = tw.users(s["login"] for s in cfg["streamers"])
    live = tw.live_streams(u["id"] for u in users.values())
    lines = [
        f"# Streamer numbers ({select.now_utc():%Y-%m-%d %H:%M} UTC)",
        "",
        "| Streamer | Live now | Viewers now | Game now | Last VOD date | Last VOD views | Last VOD length | Enabled |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for s in cfg["streamers"]:
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
    sub.add_parser("report")
    t = sub.add_parser("render-test")
    t.add_argument("video")
    t.add_argument("hook")
    t.add_argument("--facecam", help="x,y,w,h in 1920x1080 source")
    args = p.parse_args()
    {"run": cmd_run, "report": cmd_report, "render-test": cmd_render_test}[args.cmd](args)


if __name__ == "__main__":
    main()
