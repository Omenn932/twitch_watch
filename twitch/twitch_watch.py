import asyncio
import json
import os
import time
from pathlib import Path

import aiohttp
from dotenv import load_dotenv
from twitchio.ext import commands

load_dotenv()

TRIGGERS = {"!join", "!wheel"}
COOLDOWN_SECONDS = 30

TANGIA_BOT = "tangiabot"
TANGIA_TRIGGER = "!join"
TANGIA_COOLDOWN_SECONDS = 30

MAIN_CHANNEL = os.environ["TWITCH_CHANNEL"].lower()

TANGIA_CHANNELS = {
    channel.strip().lower()
    for channel in os.environ.get(
        "TWITCH_TANGIA_CHANNELS",
        "comradespacegod,suparticus,hobbitide",
    ).split(",")
    if channel.strip()
}

# ---------------------------------------------------------------------------
# Token handling
#
# Twitch access tokens expire. The bot now validates its token on startup and
# every TOKEN_CHECK_MINUTES, and uses the refresh token to get a new one before
# it runs out. Refreshed tokens are saved to TOKEN_FILE so they survive restarts
# (Twitch rotates the refresh token too, so the one in .env goes stale).
#
# Refresh goes through Twitch directly when TWITCH_CLIENT_SECRET is set (your
# own app at dev.twitch.tv), otherwise through twitchtokengenerator.com, which
# is where tokens made on that site must be refreshed.
# ---------------------------------------------------------------------------

TOKEN_FILE = Path(os.environ.get("TWITCH_TOKEN_FILE", "/app/data/tokens.json"))
TOKEN_CHECK_MINUTES = 10
REFRESH_WHEN_LEFT_SECONDS = 30 * 60

VALIDATE_URL = "https://id.twitch.tv/oauth2/validate"
TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
TTG_REFRESH_URL = "https://twitchtokengenerator.com/api/refresh/{}"


def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def strip_oauth(token):
    token = (token or "").strip()
    return token[6:] if token.lower().startswith("oauth:") else token


class TokenManager:
    def __init__(self):
        self.client_id = os.environ["TWITCH_CLIENT_ID"].strip()
        self.client_secret = os.environ.get("TWITCH_CLIENT_SECRET", "").strip()
        self.access_token = strip_oauth(os.environ.get("TWITCH_ACCESS_TOKEN"))
        self.refresh_token = os.environ.get("TWITCH_REFRESH_TOKEN", "").strip()
        self._load()

    def _load(self):
        # Saved tokens are newer than whatever is in .env, unless .env was
        # given a different refresh token by hand (a freshly generated pair).
        try:
            saved = json.loads(TOKEN_FILE.read_text())
        except (OSError, ValueError):
            return
        env_refresh = os.environ.get("TWITCH_REFRESH_TOKEN", "").strip()
        if env_refresh and env_refresh != saved.get("env_refresh_token"):
            log("New refresh token found in .env; ignoring saved tokens.")
            return
        self.access_token = saved.get("access_token") or self.access_token
        self.refresh_token = saved.get("refresh_token") or self.refresh_token

    def _save(self):
        try:
            TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
            TOKEN_FILE.write_text(json.dumps({
                "access_token": self.access_token,
                "refresh_token": self.refresh_token,
                "env_refresh_token": os.environ.get("TWITCH_REFRESH_TOKEN", "").strip(),
                "saved_at": int(time.time()),
            }))
        except OSError as e:
            log(f"Warning: could not save tokens to {TOKEN_FILE}: {e}")

    async def seconds_left(self, session):
        """Seconds until the access token expires, or 0 if it is invalid."""
        if not self.access_token:
            return 0
        async with session.get(
            VALIDATE_URL, headers={"Authorization": f"OAuth {self.access_token}"}
        ) as resp:
            if resp.status == 401:
                return 0
            resp.raise_for_status()
            data = await resp.json()
            return int(data.get("expires_in", 0)) or 10**9  # 0 means non-expiring

    async def refresh(self, session):
        if not self.refresh_token:
            raise RuntimeError(
                "Access token is invalid or expiring and TWITCH_REFRESH_TOKEN is "
                "not set. Generate a new token pair and put both the access "
                "token and refresh token in twitch/.env."
            )

        if self.client_secret:
            async with session.post(TWITCH_TOKEN_URL, data={
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            }) as resp:
                data = await resp.json(content_type=None)
                if resp.status != 200:
                    raise RuntimeError(f"Twitch refused the refresh: {data}")
                access, refresh = data.get("access_token"), data.get("refresh_token")
        else:
            async with session.get(TTG_REFRESH_URL.format(self.refresh_token)) as resp:
                data = await resp.json(content_type=None)
                if not data.get("success", resp.status == 200):
                    raise RuntimeError(f"twitchtokengenerator refused the refresh: {data}")
                access = data.get("token") or data.get("access_token")
                refresh = data.get("refresh") or data.get("refresh_token")

        if not access:
            raise RuntimeError(f"Refresh response had no access token: {data}")

        self.access_token = strip_oauth(access)
        if refresh:
            self.refresh_token = refresh
        self._save()
        log("Access token refreshed.")

    async def ensure_fresh(self, session):
        """Make sure the token is valid with some time left. Returns True if it changed."""
        left = await self.seconds_left(session)
        if left > REFRESH_WHEN_LEFT_SECONDS:
            return False
        log("Access token invalid." if left == 0 else f"Access token expires in {left // 60} min.")
        await self.refresh(session)
        return True


# ---------------------------------------------------------------------------
# Bot (behaviour unchanged)
# ---------------------------------------------------------------------------

class TwitchWatch(commands.Bot):
    def __init__(self, token):
        channels = [MAIN_CHANNEL, *TANGIA_CHANNELS]

        super().__init__(
            token=f"oauth:{token}",
            client_id=os.environ["TWITCH_CLIENT_ID"],
            nick=os.environ["TWITCH_BOT_NICK"],
            prefix="!",
            initial_channels=channels,
        )

        self.last_sent = {}
        self.last_tangia_sent = {}

    async def event_ready(self):
        log(f"Connected as {self.nick}")
        log(f"Watching main channel: #{MAIN_CHANNEL}")

        for channel in TANGIA_CHANNELS:
            log(f"Watching TangiaBot in #{channel}")

    async def event_message(self, message):
        if message.echo or message.author is None:
            return

        author = message.author.name.lower()
        channel_name = message.channel.name.lower()
        text = message.content.strip().lower()

        # React to commands sent by the main channel streamer.
        if channel_name == MAIN_CHANNEL and author == MAIN_CHANNEL:
            if text in TRIGGERS:
                now = time.time()
                last_time = self.last_sent.get(text, 0)

                if now - last_time >= COOLDOWN_SECONDS:
                    self.last_sent[text] = now
                    await message.channel.send(text)
                    log(f"Sent {text} to #{MAIN_CHANNEL}")

        # React to TangiaBot messages in configured channels.
        if (
            channel_name in TANGIA_CHANNELS
            and author == TANGIA_BOT
            and TANGIA_TRIGGER in text
        ):
            now = time.time()
            last_time = self.last_tangia_sent.get(channel_name, 0)

            if now - last_time >= TANGIA_COOLDOWN_SECONDS:
                self.last_tangia_sent[channel_name] = now
                await message.channel.send(TANGIA_TRIGGER)
                log(f"Sent !join to #{channel_name}")


# ---------------------------------------------------------------------------
# Supervisor: keep the token fresh and reconnect with the new one
# ---------------------------------------------------------------------------

async def watch_token(tokens, session, bot):
    while True:
        await asyncio.sleep(TOKEN_CHECK_MINUTES * 60)
        try:
            if await tokens.ensure_fresh(session):
                log("Reconnecting with the new token.")
                await bot.close()
                return
        except Exception as e:  # keep running; retry next check
            log(f"Token check failed: {e}")


async def main():
    tokens = TokenManager()
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                await tokens.ensure_fresh(session)
            except Exception as e:
                log(f"Could not get a valid token: {e}")
                await asyncio.sleep(60)
                continue

            bot = TwitchWatch(tokens.access_token)
            watcher = asyncio.create_task(watch_token(tokens, session, bot))
            try:
                await bot.start()
            except Exception as e:
                log(f"Bot stopped: {e!r}")
            finally:
                watcher.cancel()
                try:
                    await bot.close()
                except Exception:
                    pass
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
