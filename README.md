# Twitch Watch

A small Twitch chat bot that:

- repeats `!join` / `!wheel` in the main channel when the streamer types them (30 s cooldown)
- replies `!join` when TangiaBot posts a `!join` prompt in the configured Tangia channels (30 s cooldown per channel)

## Setup

1. Go to https://twitchtokengenerator.com, choose **Bot Chat Token**, sign in as the bot account, and copy the **Access Token**, **Refresh Token** and **Client ID**.
2. Copy `twitch/.env.example` to `twitch/.env` and paste them in.
3. Run it:

   ```
   docker compose up -d --build
   docker compose logs -f
   ```

You should see `Connected as ...` within a few seconds.

## Tokens

Twitch access tokens expire. The bot checks its token every 10 minutes and uses the refresh token to get a new one before it runs out, then reconnects. The latest tokens are saved in `twitch/data/tokens.json` (ignored by git), because each refresh also replaces the refresh token.

If you ever paste a new refresh token into `.env`, the bot uses it instead of the saved one.
