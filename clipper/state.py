"""Remembers what was posted so nothing is ever posted twice."""
import json
import os
from datetime import datetime, timedelta, timezone

PATH = os.path.join("state", "posted.json")


def load():
    if os.path.exists(PATH):
        with open(PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"posted": {}}


def save(state):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
    state["posted"] = {k: v for k, v in state["posted"].items() if v.get("at", "") >= cutoff}
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    with open(PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1, ensure_ascii=False)


def posted_today(state, login):
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    return sum(1 for v in state["posted"].values() if v.get("login") == login and v.get("at", "") >= since)


def recent_openers(state, days=7):
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    return {v.get("hook", "").lower() for v in state["posted"].values() if v.get("at", "") >= since}


def taken_slots(state):
    return {v["publish_at"] for v in state["posted"].values() if v.get("publish_at")}


def record(state, clip, login, hook, title, youtube_id=None, publish_at=None, campaign=False):
    state["posted"][clip["id"]] = {
        "at": datetime.now(timezone.utc).isoformat(),
        "login": login,
        "views_at_pick": clip["view_count"],
        "hook": hook,
        "title": title,
        "youtube_id": youtube_id,
        "source": clip["url"],
        "publish_at": publish_at,
        "campaign": campaign,
        "alerted": False,
    }
