"""Small client for the free Twitch Helix API (app access token)."""
import time

import requests

API = "https://api.twitch.tv/helix"


class Twitch:
    def __init__(self, client_id: str, client_secret: str):
        r = requests.post(
            "https://id.twitch.tv/oauth2/token",
            params={
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "client_credentials",
            },
            timeout=30,
        )
        r.raise_for_status()
        self.headers = {
            "Client-Id": client_id,
            "Authorization": f"Bearer {r.json()['access_token']}",
        }

    def get(self, path, params):
        for _ in range(4):
            r = requests.get(API + path, headers=self.headers, params=params, timeout=30)
            if r.status_code == 429:
                reset = int(r.headers.get("Ratelimit-Reset", time.time() + 5))
                time.sleep(max(1, reset - time.time()))
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError(f"Twitch rate limit kept failing for {path}")

    def users(self, logins):
        out = {}
        logins = list(logins)
        for i in range(0, len(logins), 100):
            data = self.get("/users", [("login", l) for l in logins[i:i + 100]])
            for u in data.get("data", []):
                out[u["login"].lower()] = u
        return out

    def game_id(self, name):
        data = self.get("/games", {"name": name}).get("data", [])
        if data:
            return data[0]["id"]
        hits = self.get("/search/categories", {"query": name, "first": 10}).get("data", [])
        for h in hits:
            if h["name"].lower().startswith(name.lower()):
                return h["id"]
        return None

    def clips(self, broadcaster_id, started_at, ended_at, pages=2):
        """Clips for a broadcaster in a time window, most-viewed first."""
        out, cursor = [], None
        for _ in range(pages):
            params = {
                "broadcaster_id": broadcaster_id,
                "started_at": started_at,
                "ended_at": ended_at,
                "first": 100,
            }
            if cursor:
                params["after"] = cursor
            data = self.get("/clips", params)
            out.extend(data.get("data", []))
            cursor = data.get("pagination", {}).get("cursor")
            if not cursor:
                break
        return out

    def live_streams(self, user_ids):
        out = {}
        ids = list(user_ids)
        for i in range(0, len(ids), 100):
            data = self.get("/streams", [("user_id", u) for u in ids[i:i + 100]])
            for s in data.get("data", []):
                out[s["user_id"]] = s
        return out

    def last_vod(self, user_id):
        data = self.get("/videos", {"user_id": user_id, "type": "archive", "first": 1})
        vids = data.get("data", [])
        return vids[0] if vids else None
