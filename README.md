# Six Star Clips: GTA 6 streamer clip bot

Every few hours this bot:

1. Finds the most-viewed new GTA 6 Twitch clips from the streamers you have permission to clip (free Twitch API)
2. Downloads them, writes word-by-word captions (faster-whisper) and a hook title
3. Renders a 1080x1920 Short: blurred background, hook at the top, credit to the streamer at the bottom
4. Sends it to your phone on Telegram, ready to post to YouTube, TikTok, Instagram and X. Once YouTube approves the API audit, it uploads to YouTube by itself too.
5. Remembers what it posted so nothing is posted twice. Writes a daily report of every streamer's live and last-stream numbers to `reports/streamers.md`.

It runs free on GitHub Actions: this repo must be **public**, and your keys stay hidden in GitHub Secrets.

---

## Setup (about 30 minutes, all free)

### 1. YouTube channel
- On the Google account you want to use, create the channel (Brand Account) with your chosen name and handle.
- Verify it with your phone at youtube.com/verify. This raises your daily upload limit.

### 2. Permission: join the paid clipping campaigns
- Sign up at contentrewards.com (runs on Whop) and join the GTA 6 campaigns for the streamers in `config.yaml`: Kai Cenat, Lacy, Clix, YourRAGE, AsianJeff.
- Read each campaign's rules: minimum views, required hashtags, ad disclosure, and submitting within 30 minutes of posting.
- Don't turn on any streamer marked `enabled: false` until you have their written OK.

### 3. Twitch API keys (optional)
The bot works **without** Twitch keys: it reads each streamer's public "top clips" page and checks each clip's game one by one. Keys make it faster and filter by game more reliably, so add them later if you can.
1. Sign in at dev.twitch.tv/console (your Twitch account needs two-factor auth turned on).
2. Click **Register Your Application**. Name: anything. OAuth Redirect URL: `http://localhost`. Category: *Application Integration*. Client type: *Confidential*.
3. Open the app, copy the **Client ID**, then click **New Secret** and copy the **Client Secret**.

### 4. Telegram bot (clips delivered to your phone)
1. In Telegram, message **@BotFather** and send `/newbot`. Follow the prompts and copy the **bot token**.
2. Send any message to your new bot.
3. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and copy the number after `"chat":{"id":`. That's your **chat ID**.

### 5. Add the secrets to GitHub
Repo > **Settings > Secrets and variables > Actions > New repository secret**:

| Secret | Value |
| --- | --- |
| `TWITCH_CLIENT_ID` | from step 3 (optional) |
| `TWITCH_CLIENT_SECRET` | from step 3 (optional) |
| `TELEGRAM_BOT_TOKEN` | from step 4 |
| `TELEGRAM_CHAT_ID` | from step 4 |

### 6. First run
- Go to the **Actions** tab and enable workflows if asked.
- Open **Clip and post** > **Run workflow**. Tick *dry run* for a test: the rendered clips appear under the run's **Artifacts**.
- Run it again without *dry run* and the clips arrive on Telegram.
- After that it runs by itself 6 times a day, plus the **Daily streamer report**.

---

## Auto-posting to YouTube through Make (no audit, free)

Make's YouTube app is already verified by Google, so uploads through it go public straight away.
1. In Make (make.com), go to **Connections > Create a connection > YouTube** and sign in with the channel's Google account.
2. Claude then builds the Make scenario (webhook > YouTube "Upload a Video") and gives you its webhook URL.
3. Add it as the GitHub secret `MAKE_WEBHOOK_URL`. Leave `publish.make: true` in `config.yaml`.

Free-plan limits: 5 MB per file (the bot sends a lighter 720p copy when needed) and 1,000 operations a month (about 2 per post, so roughly 15 posts a day at most).

## YouTube streamers
Entries with `platform: youtube` (CarryMinati, Ducky Bhai, Mythpat, Total Gaming, Payal Gaming, Techno Gamerz, IShowSpeed) are clipped from their past live streams, using YouTube's "Most replayed" graph to find the moments. `language: hi` / `ur` translates Hindi or Urdu speech into English captions. YouTube sometimes blocks downloads from GitHub's servers: if those streamers keep failing in the run log, they can run from your PC instead.

## Full auto-posting to YouTube with your own API project (optional, approval takes time)

YouTube locks anything uploaded through an unaudited API project to **private**, and those videos can't be made public later. So keep `publish.youtube: false` until the audit is approved.

1. **Google Cloud project:** console.cloud.google.com > new project > *APIs & Services > Library* > enable **YouTube Data API v3**.
2. **OAuth consent screen:** User type *External*. Add the scope `.../auth/youtube.upload`. Then click **Publish app** (status *In production*). If you leave it in *Testing*, the login token expires every 7 days.
3. **Credentials > Create OAuth client ID** > *Web application*. Authorized redirect URI: `https://developers.google.com/oauthplayground`. Copy the client ID and secret.
4. Open **developers.google.com/oauthplayground**:
   - Click the gear icon, tick *Use your own OAuth credentials*, and paste the ID and secret.
   - In Step 1, type `https://www.googleapis.com/auth/youtube.upload` and click *Authorize APIs*.
   - Sign in and pick **the channel**, then accept the "unverified app" warning (it's your own app).
   - In Step 2, click *Exchange authorization code for tokens* and copy the **Refresh token**.
5. Add the secrets `YT_CLIENT_ID`, `YT_CLIENT_SECRET` and `YT_REFRESH_TOKEN`.
6. Submit the **YouTube API Services Audit and Quota Extension Form** (support.google.com/youtube/contact/yt_api_form). Describe it honestly: an internal tool that uploads credited, permission-based clips to your own channel. Approval isn't guaranteed.
7. When approved, set `publish.youtube: true` in `config.yaml`. Telegram keeps sending you a copy for TikTok, Instagram and X.

## Optional: better hooks with Claude
On a computer with Claude Code installed, run `claude setup-token` and save the token as the `CLAUDE_CODE_OAUTH_TOKEN` secret. The bot will then ask Claude (on your own plan) for a hook and title from each clip's transcript. Without it, it uses the viewer-written Twitch clip title or a rotating set of templates.

## Everyday changes (edit `config.yaml` on GitHub)
- **More or fewer posts:** `posts_per_run` (6 runs a day), or change the `cron` times in `.github/workflows/clip-and-post.yml`.
- **Add a streamer:** add an entry with their Twitch login. Set `enabled: true` only with permission.
- **Facecam-on-top layout:** add `facecam: {x, y, w, h}`, the box around their camera in the 1920x1080 stream.
- **Test a layout locally:** `python -m clipper render-test video.mp4 "Hook text" --facecam 1460,40,420,300`

## Rules the bot follows
- Credit strip and Twitch link on every clip, with `#ad` and the campaign hashtags where a campaign requires them.
- Skips clips whose titles look like story spoilers (Rockstar takes spoilers down), and anything not in the GTA 6 category.
- Max 3 posts per streamer per day. Never the same clip twice, and never the same opening line within 7 days.
