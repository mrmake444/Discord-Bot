# rlbot

Discord bot, Python + discord.py, runs in Docker in Proxmox CT 100
("jack-dreams", 192.168.1.235). Rebuild after ANY code change:
`docker compose up -d --build` — editing bot.py alone does nothing.

Commands: rlstats, link, unlink, mcstatus, megaphone, where, tps,
ping, shutdown.

## Minecraft server (separate box, CT 101)
192.168.1.183, /root/minecraft-server. Paper 26.2, max 20 players.

lazymc-docker-proxy sits in front. It owns port 25565 and keeps the
minecraft container STOPPED until someone joins. Critical: lazymc
answers status pings itself with a fabricated response, so pinging
25565 reports the server "up" even when it is asleep. Never trust
25565 for liveness or player count.

Use RCON reachability instead: 192.168.1.183:25575. That port belongs
to the minecraft container, so connection-refused = asleep, connects
= awake. Library is aio-mc-rcon; send_cmd returns a tuple. The port
is bound to that specific host IP, so MC_HOST must be exactly
192.168.1.183 — not localhost or a container name.

lazymc timings: sleep_after=900 (counts from last player leaving),
minimum_online_time=120, start_timeout=300.

## Gotchas
- Gamerule is `keep_inventory` (snake_case) in MC 26.2.
- Two SSH keys to CT 101: `wake_key` is forced-command start-only;
  `stop_key` (MC_SSH_KEY, mounted /stop_key) is unrestricted and is
  what /shutdown uses.
- Hard rule: do not spam the channel. Ephemeral replies, digests
  replace prior messages, transient notices auto-delete.
- The minecraft container runs as uid/gid 1000, not root. Anything
  dropped into /root/minecraft-server/data/** over root SSH (plugin
  jars, script files, new directories) must be `chown 1000:1000`,
  or the server can't write to it. This is exactly what silently
  broke Skript on first install — its plugin folder was root-owned,
  so it couldn't write its own config.sk and the whole plugin came
  up broken with no obvious error tying the two together.
- CT 101's DNS depends on the router (192.168.1.1) forwarding/
  resolving; when that resolver is unreachable, the itzg image's
  entrypoint can't verify the Paper build against fill.papermc.io
  and the container fails to boot entirely (VERSION=LATEST triggers
  this check on every start, not just first install). Currently
  pointed at 192.168.1.99 (primary) / 192.168.1.235 (backup) in
  /etc/resolv.conf on CT 101 after the router's resolver hung.
- /root/minecraft-server/data/usercache.json is the authoritative
  source for exact player name spelling/casing — several requested
  usernames (FrazzleDrip, BeeKeeper42, 1mantaskforce, Chortlemyballs)
  didn't match the real account and had to be corrected against it.

## Plugins (CT 101)
- BlockLogger, bStats, spark — pre-existing, not managed here.
- Skript 2.16.1 (plugins/Skript.jar) — installed for simple
  event-driven scripting without a custom compiled plugin. Scripts
  live in plugins/Skript/scripts/*.sk.
- plugins/Skript/scripts/king.sk — per-player join broadcasts (an
  "on join: if player's name is X: broadcast Y" rule per player).
  Edit via scp + `chown 1000:1000`, then either wait for the next
  natural boot or run `sk reload all` over RCON if the server's
  already awake — no container restart needed for script changes.
