"""Publishing: YouTube Data API upload and Telegram delivery to your phone."""
import os

import requests


def youtube_upload(path, title, description, tags, privacy="public", paid=False):
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    creds = Credentials(
        token=None,
        refresh_token=os.environ["YT_REFRESH_TOKEN"],
        client_id=os.environ["YT_CLIENT_ID"],
        client_secret=os.environ["YT_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/youtube.upload"],
    )
    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
    body = {
        "snippet": {
            "title": title[:100],
            "description": description[:4900],
            "tags": tags,
            "categoryId": "20",  # Gaming
        },
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }
    parts = ["snippet", "status"]
    if paid:
        body["paidProductPlacementDetails"] = {"hasPaidProductPlacement": True}
        parts.append("paidProductPlacementDetails")

    def _send(part_list, payload):
        media = MediaFileUpload(path, mimetype="video/mp4", chunksize=-1, resumable=True)
        req = yt.videos().insert(part=",".join(part_list), body=payload, media_body=media)
        resp = None
        while resp is None:
            _, resp = req.next_chunk()
        return resp["id"]

    try:
        return _send(parts, body)
    except HttpError as e:
        if paid and e.resp.status == 400:
            # Fall back if the paid-promotion field is rejected; "#ad" stays in the description.
            body.pop("paidProductPlacementDetails", None)
            return _send(["snippet", "status"], body)
        raise


def telegram_send(path, caption):
    token, chat = os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
    base = f"https://api.telegram.org/bot{token}"
    if os.path.getsize(path) < 49 * 1024 * 1024:
        with open(path, "rb") as f:
            r = requests.post(
                f"{base}/sendVideo",
                data={"chat_id": chat, "caption": caption[:1024], "supports_streaming": "true"},
                files={"video": f},
                timeout=300,
            )
    else:
        r = requests.post(f"{base}/sendMessage",
                          data={"chat_id": chat, "text": caption[:4000] + "\n\n(File too big for Telegram: grab it from the GitHub run.)"},
                          timeout=60)
    r.raise_for_status()
    # Send the full description as a second message so it is easy to copy.
    return r.json()


def telegram_text(text):
    token, chat = os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
    requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                  data={"chat_id": chat, "text": text[:4000], "disable_web_page_preview": "true"},
                  timeout=60).raise_for_status()
