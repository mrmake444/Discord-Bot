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

## Task
/mcstatus still pings 25565, so it lies while the server sleeps.
Rewire it to check RCON and branch the embed: real player list when
awake, sleeping state when asleep, no shutdown button when asleep.
Also disable the idle watcher — lazymc owns sleeping now, and two
systems stopping the same container will race.
