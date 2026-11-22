"""Publishing: YouTube Data API upload and Telegram delivery to your phone."""
import os

import requests


def youtube_upload(path, title, description, tags, privacy="public", paid=False, publish_at=None):
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
    if publish_at:
        body["status"].update({"privacyStatus": "private", "publishAt": publish_at})
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


def make_upload(path, title, description, tags, publish_at):
    """Send the clip to your Make scenario, which uploads it to YouTube.
    Make's YouTube app is already verified, so uploads can be public right away."""
    url = os.environ["MAKE_WEBHOOK_URL"]
    with open(path, "rb") as f:
        r = requests.post(
            url,
            data={"title": title[:100], "description": description[:4900],
                  "tags": ",".join(tags), "publish_at": publish_at},
            files={"video": (os.path.basename(path), f, "video/mp4")},
            timeout=600,
        )
    r.raise_for_status()
    text = r.text.strip()
    try:
        data = r.json()
        return data.get("video_id") or data.get("id") or "QUEUED"
    except ValueError:
        # "Accepted" = Make queued it and will upload it in a moment.
        return "QUEUED" if not text or text.lower() == "accepted" else text
