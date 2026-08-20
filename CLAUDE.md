# rlbot

Discord bot, Python + discord.py, runs in Docker in Proxmox CT 100
("jack-dreams", 192.168.1.235). Rebuild after ANY code change:
`docker compose up -d --build` — editing bot.py alone does nothing
(applies to any of the split-out modules below, not just bot.py).

As of 2026-08-19, split across four files instead of one:
- `bot.py` — RLBot class + every `/command` handler, `_COMMAND_REFERENCE`.
- `storage.py` — generic `load_json`/`save_json` + typed wrappers for
  links/locations/joinmessages (was three hand-duplicated pairs).
- `mc.py` — RCON, container start/stop/status, the chat bridge (incl.
  location sync, see below), `_StartView`/button.
- `tracker.py` — Tracker.gg lookups for /rlstats.

Commands: rlstats, link, unlink, mcstatus, megaphone, find, setlocation,
removelocation, locations, joinmessage, removejoinmessage, map, tps, ping,
shutdown, commands, mccommands.

Location sync is bidirectional: Discord's /setlocation already pushed to
{loc.*} over RCON; in-game /setlocation (locations.sk) now also prints a
`LOCSYNC:<player>:<name>:<x>:<y>:<z>:<color>` line to console, which
mc.py's chat-bridge log-tail parses (LOCSYNC_RE/LOCREMOVE_RE) and applies
to locations.json via `_sync_location_from_game`. A location saved from
either side now reaches the other — previously only Discord→game worked.

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
  this check on every start, not just first install). This looks
  like the server being permanently "asleep" — lazymc tries to wake
  it, the minecraft container dies on the DNS check, lazymc force-
  stops it and goes back to sleep, repeat forever. Check `docker
  logs minecraft` for `UnknownHostException: Failed to resolve
  'fill.papermc.io'` to confirm.
  Pointed at 192.168.1.99 (primary) / 192.168.1.235 (backup) in
  /etc/resolv.conf on CT 101 after the router's resolver hung.
  Recurred on 2026-08-17: a DHCP lease renewal overwrote
  /etc/resolv.conf with the router's (broken) nameserver again,
  since resolv.conf alone doesn't survive renewal. Fixed durably
  by adding `supersede domain-name-servers 192.168.1.99,
  192.168.1.235;` to /etc/dhcp/dhclient.conf on CT 101 so renewals
  can't clobber it again. If the server is asleep and won't wake,
  check /etc/resolv.conf on CT 101 and re-apply if it's drifted
  back to 192.168.1.1.
- /root/minecraft-server/data/usercache.json is the authoritative
  source for exact player name spelling/casing — several requested
  usernames (FrazzleDrip, BeeKeeper42, 1mantaskforce, Chortlemyballs)
  didn't match the real account and had to be corrected against it.
- joinmessage.sk's "on join" trigger used bare %player% as a variable
  index ({joinmsg::%player%}), but Skript's global config has "use
  player UUIDs in variable names: true", which silently rewrites any
  %player% interpolated into a variable index into the player's UUID.
  /setjoinmessagemc writes with {joinmsg::%arg-1%}, where arg-1 is a
  plain name string — so the read (UUID-keyed) and write (name-keyed)
  never hit the same variable. Net effect: join messages set via
  /joinmessage silently never displayed for anyone, since server
  install. Fixed 2026-08-18 by changing the "on join" lookup to
  {joinmsg::%name of player%}, which forces the string-name form and
  matches the write side. If a similar "set X via command, read X on
  an event" pattern shows up elsewhere keyed off a player variable,
  check for this same mismatch — it fails completely silently, no
  error anywhere, the value just never matches on read.
- variables.csv is NOT a live view of Skript's variable state — it's
  written at startup/shutdown, not continuously despite what Skript's
  own config comments imply. Don't grep it to check whether a SET
  just took effect; the change is live in memory immediately, the
  file just hasn't caught up. Only trust the file to check state as
  of last boot.

## Plugins (CT 101)
- BlockLogger, bStats, spark — pre-existing, not managed here.
- Vault, EssentialsX, LuckPerms, ChestShop, WorldEdit — economy/shop/
  permissions stack, added 2026-08-18. LuckPerms is required for any
  non-op Essentials permission (e.g. essentials.balance) to work at
  all — without a permissions plugin, Bukkit's SuperPerms fallback
  only grants op-only or explicitly-`default: true` nodes.
- PlaceholderAPI + TAB (NEZNAMY) + skhud-bridge — added 2026-08-19 for
  the in-game HUD sidebar/tab-footer. **Do not install SkBee** — it
  crashes on enable on this server's Paper version (26.2); no SkBee
  release, past or present, has ever targeted a version this new, so
  trying a different SkBee version won't help either. Base Skript
  also has zero built-in sidebar/tab-footer syntax (confirmed by
  testing to failure with SkBee fully removed) — there is no
  addon-free path. skhud-bridge (source at /root/skhud-bridge on the
  bot host, Maven project, JDK 25) is a deliberately thin custom
  plugin: it does NOT touch Scoreboard/Player-list APIs at all, only
  implements a PlaceholderAPI expansion (%skhud_<key>%) backed by an
  in-memory per-player map, updated via its own `skhudset`/
  `skhudclear` console commands. TAB (confirmed compatible with
  26.2, actively maintained) does all the actual per-player
  rendering — its `scoreboard`/`header-footer` sections in
  `/data/plugins/TAB/config.yml` are templated with `%skhud_line_N%`
  / `%skhud_title%` (sidebar) and `%skhud_footer%` / `%skhud_header%`
  (tab-press full coordinates), config also mirrored at
  data/tab-config.yml in this repo.
  - **The two halves collapse differently when empty, and this is the
    whole reason for that `%skhud_footer%` key.** TAB's sidebar drops
    a line whose placeholder is empty (`StableDynamicLine` calls
    `removeLine`), so `%skhud_line_1..10%` can be listed freely and
    the sidebar sizes itself. Header/footer does NOT: it is a plain
    `String.join("\n", configuredLines)` with no filtering, so the
    twelve `%skhud_footer_N%` slots it used to list drew twelve
    blank lines — the "huge transparent tab box" bug (fixed
    2026-08-19). skhud-bridge therefore serves a computed `footer`
    key joining footer_1..N up to the first gap, so the config lists
    one slot. Do not add per-line footer slots back.
  - Because TAB always draws the sidebar title even when every line
    under it is empty, hud.sk only sets the `title` key when the
    sidebar actually has content — otherwise an empty HUD showed a
    bare "HUD" heading over nothing.
  - The `header` key is the exception and **is** set unconditionally:
    it carries the viewer's own rounded X/Y/Z plus a friendly
    dimension name (Overworld/Nether/End, mapped from the raw
    world/world_nether/world_the_end), which is useful with nothing
    pinned. It is the only always-on part of the HUD, so an unpinned
    player still gets exactly one header line on tab-press instead of
    an empty box. It refreshes on clock.sk's 1s tick, so it trails
    slightly while the player is moving — TAB's own 500ms
    default-refresh-interval is not the limiting factor.
  - skhud-bridge also supplies **tab completion for /pin and /unpin**
    via `AsyncTabCompleteEvent`. Skript cannot: its
    `ScriptCommand.onTabComplete` returns `Collections.emptyList()` for
    every argument type except `Player`/`OfflinePlayer` (those return
    null, which is what makes Bukkit fall back to online player names),
    and /pin's first argument has to be `<text>` to accept location
    names — so /pin offered no suggestions at all once players were
    folded into it. Candidates come from two more keys hud.sk pushes
    each tick, `pinnable` (the player's saved location names) and
    `pinned` (what's currently on their HUD), space-joined; online
    player names are added to /pin's list in the plugin, from a
    join/quit-maintained cache because the event fires off the main
    thread. It hooks the event rather than the commands' TabCompleter
    because Skript rebuilds its command objects on every `sk reload`.
  Rebuild skhud-bridge: `cd /root/skhud-bridge && mvn package`,
  deploy the resulting target/skhud-bridge.jar, restart (new/changed
  Java always needs a restart, unlike .sk files).
- Skript 2.16.1 (plugins/Skript.jar) — event-driven scripting without
  a custom compiled plugin for most features. Scripts live in
  plugins/Skript/scripts/*.sk, source-of-truth copies in this repo's
  data/. Edit via scp + `chown 1000:1000` (uid/gid 1000, not root —
  see gotcha above), then `sk reload <file>.sk` over RCON if the
  server's already awake — no restart needed for .sk changes, only
  for new/changed plugin jars.
  - **`run console command` is not valid Skript syntax** — the real
    effect is `execute console command`. Using the wrong one produces
    a wall of "Can't understand this condition/effect" parse errors
    long enough to exceed RCON's response size and make `rcon-cli`
    fail with "response too long" instead of showing them; use a
    direct RCON client (e.g. this repo's data/tools/rcon_client.py)
    to see the real error text in that situation.
  - clock.sk — the *only* `every 1 second: loop all players` trigger
    on the server; calls `tickHud(loop-player)` (hud.sk) and
    `tickRaceFall(loop-player)` (race.sk) so per-player per-second
    work happens in one shared loop instead of each script running
    its own (locationhud.sk and race.sk used to, independently).
  - locations.sk — /setlocation, /removelocation, /find, /locations,
    plus /setlocationmc, /removelocationmc (console-only, RCON-pushed
    from bot.py). Also prints LOCSYNC/LOCREMOVE to console (see the
    bot.py section above for the bidirectional sync this feeds).
  - hud.sk — /pin, /unpin, /track, /untrack, an `on command` hook
    that auto-pins "home" (permanent) whenever Essentials' /sethome
    runs, and `tickHud()` which builds each player's sidebar/footer
    content and pushes it via `skhudset`. Multiple pins and/or
    tracked players can be active at once, each shown as
    "<name> <compass dir> <distance>". **/pin takes either a saved
    location or a player** and routes to `{pins::*}` or `{track::*}`
    accordingly (`hudAdd`/`hudRemove`); /track and /untrack are just
    the explicit player-only spelling. It used to be location-only,
    which is why pinning a player appeared to do nothing at all —
    players kept typing /pin for players, since the HUD draws both
    kinds of entry identically. The player branch requires `is
    online` or `has played before`: `parsed as offline player`
    returns a real OfflinePlayer for any syntactically valid name
    (verified — "house" yields a never-seen player named "House",
    while a 17-char name is rejected for length), so without that
    check a typo'd location name became a permanent phantom HUD
    entry. An entry auto-removes once
    you're within 5 blocks unless added with the "permanent" keyword
    (`/pin home permanent`, `/track Steve permanent`). Full exact
    coordinates for every active pin/tracked-player show on
    tab-press (TAB's header/footer), not in the compact sidebar.
  - mccommands.sk — hardcoded in-game mirror of bot.py's
    `_MINECRAFT_COMMANDS`, kept in sync by hand. **Known dead code:**
    bot.py's `push_mccommands()` (fired from `setup_hook`) RCONs
    `clearmccommandsmc` / `setmccommandsmc` to replace that hand-sync
    with a pushed `{mccommands::*}` variable, but neither command is
    implemented in any .sk — the calls just return "Unknown command"
    and the comment claiming /mccommands is pushed rather than
    hand-copied is wrong. Either implement the two console-only
    commands (and have the bot translate its Discord markdown to
    legacy color codes on the way in) or delete `push_mccommands`;
    until then treat the two lists as a manual sync point.
  - **The ChestShop sign format now lives in three places** and is
    hand-synced: `_SHOP_SIGN` in bot.py (Discord `/commands` and
    `/mccommands`), mccommands.sk (in-game `/mccommands`) and
    shophelp.sk (in-game `/shophelp`, the long form). Its specifics are
    read off `plugins/ChestShop/config.yml` and go stale if that
    changes — currently `REVERSE_BUTTONS: false` (right-click buys,
    **left**-click sells), `ALLOW_AUTO_ITEM_FILL: true` (line 4 takes
    `?`, not a blank line), `BLOCK_SHOPS_WITH_SELL_PRICE_HIGHER_THAN_BUY_PRICE:
    true` (S ≤ B), `SHOP_CREATION_PRICE: 0`, `USE_BUILT_IN_PROTECTION:
    true`, `SHOP_CONTAINERS` limited to CHEST/TRAPPED_CHEST.
  - Discord's `/commands` and `/mccommands` send **embeds**, not plain
    messages: the full reference renders to ~2400 characters, past the
    2000-character cap on message content, which would make the command
    fail to send outright. Embed descriptions allow 4096 — check the
    rendered length before adding to those lists.
  - race.sk, tint.sk, shopsigninfo.sk — unchanged in
    structure; race.sk's fall-death check was extracted from its own
    "every 1 second" into `tickRaceFall()`, called from clock.sk.
  - joinmessage.sk (source of truth: data/joinmessage.sk in this
    repo) — custom per-player join messages, set via Discord's
    /joinmessage and pushed in over RCON. Replaces the vanilla join
    line with the player's text via a single "on join" trigger keyed
    off the {joinmsg::*} variable store, so a player only ever gets
    one join message. Formerly king.sk hardcoded 8 players' messages
    as a second, separate on-join trigger (always firing alongside
    the vanilla line); that file was deleted 2026-08-18 and its
    entries migrated into {joinmsg::*} so there's exactly one
    mechanism now.
