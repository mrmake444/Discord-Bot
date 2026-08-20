# RL Stats Bot

Discord bot for Rocket League ranks. Runs in Docker inside CT 100, next to
the InfluxDB/Grafana stack.

## 1. Discord Developer Portal

From your new application:

- **Bot** tab → Reset Token → copy it. This is `DISCORD_TOKEN`.
- **OAuth2 → URL Generator** → tick scopes `bot` *and* `applications.commands`.
  Under Bot Permissions tick **Send Messages** and **Embed Links**.
  Open the generated URL and invite it to your server.

`applications.commands` is the one people forget. Without it slash commands
never register, and the failure is silent.

You do **not** need Message Content Intent — this bot only uses slash commands.

## 2. Tracker.gg key

Register at <https://tracker.gg/developers> for an API key. That's `TRN_API_KEY`.

Rate limits are tight on the free tier. Fine for a handful of friends,
not fine for a public bot.

## 3. Server ID

Enable Developer Mode in Discord (Settings → Advanced), then right-click your
server → Copy Server ID. That's `GUILD_ID`.

Setting it scopes commands to your server, which registers them **instantly**.
Leave it blank and Discord registers globally, which can take an hour to
propagate — the usual reason a new bot "doesn't work."

## 4. Deploy

Copy the folder into CT 100, then:

```bash
cp .env.example .env
nano .env          # fill in all three values
docker compose up -d --build
docker logs -f discordbot
```

You want to see `Logged in as ...` and `Commands synced to guild ...`.

Then in Discord: `/ping` to confirm it's alive, `/rlstats` for the real thing.

## Verify before you trust the output

The Tracker.gg endpoint shape in `fetch_profile()` and the playlist names in
`PLAYLISTS` are the long-standing v2 API format, but confirm against their
current docs. If ranks come back empty, dump the raw JSON:

```python
log.info(json.dumps(payload, indent=2))
```

and compare the `segments[].metadata.name` values against the `PLAYLISTS` list.

## Where this goes next

- **Ballchasing.com API** for replay-level detail (goals, saves, boost usage).
  Different data source, same bot — add a second command.
- **Write ranks to InfluxDB** on a schedule. InfluxDB is already in CT 100, so
  it's a client library and a loop. Then MMR history graphs in Grafana for free.
- **Steam news feed and wishlist alerts** are the same shape as this bot:
  poll an API, post an embed. Reuse this scaffold rather than starting over.
