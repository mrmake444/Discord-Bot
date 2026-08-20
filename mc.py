"""Everything that talks to the Minecraft server: RCON, container
start/stop/status, and the log-tail chat bridge (which now also carries
location sync — see _sync_location_from_game/_remove_location_from_game).

Needs a discord.Client set via init() before the bridge or announcements
can reach Discord — done once from bot.py right after RLBot() is created,
to avoid a circular import (bot.py already imports this module).
"""

import asyncio
import json
import logging
import os
import re

import asyncssh
import discord
from aiomcrcon import Client, IncorrectPasswordError, RCONConnectionError

import storage

log = logging.getLogger("rlbot.mc")

MC_HOST = os.getenv("MC_HOST", "192.168.1.183")
MC_RCON_PORT = int(os.getenv("MC_RCON_PORT", "25575"))
MC_RCON_PASSWORD = os.getenv("MC_RCON_PASSWORD")
MC_SSH_HOST = os.getenv("MC_SSH_USER", MC_HOST)
MC_SSH_USER = os.getenv("MC_SSH_USER", "root")
MC_SSH_KEY = os.getenv("MC_SSH_KEY", "/wake_key")
MC_CONTAINER = os.getenv("MC_CONTAINER", "minecraft")
MC_WAKE_KEY = os.getenv("MC_WAKE_KEY", "/wake_key")
MC_WORLD_NAME = os.getenv("MC_WORLD_NAME", "world")

# Two-way Discord <-> Minecraft chat bridge. "0" = disabled.
MC_CHAT_CHANNEL_ID = int(os.getenv("MC_CHAT_CHANNEL_ID", "0"))
MC_CHAT_LOG_PATH = os.getenv("MC_CHAT_LOG_PATH", "/root/minecraft-server/data/logs/latest.log")
MC_BRIDGE_JOIN_LEAVE = os.getenv("MC_BRIDGE_JOIN_LEAVE", "true").lower() == "true"
# stop_key is unrestricted (unlike the forced-command wake_key), so it's the
# one that can run an arbitrary `tail -F` instead of just `docker start`.
MC_BRIDGE_SSH_KEY = os.getenv("MC_BRIDGE_SSH_KEY", "/stop_key")

# Optional: channel to announce shutdowns in. "0" = stay silent.
MC_ANNOUNCE_CHANNEL_ID = int(os.getenv("MC_ANNOUNCE_CHANNEL_ID", "616023990921723954"))

CHAT_LINE_RE = re.compile(r"^\[\d{2}:\d{2}:\d{2}\] \[[^\]]*\]: <([^>]+)> (.*)$")
JOIN_LEAVE_RE = re.compile(r"^\[\d{2}:\d{2}:\d{2}\] \[Server thread/INFO\]: (\S+) (joined|left) the game$")
# Emitted by locations.sk's /setlocation and /removelocation (in-game) so
# changes made in-game reach locations.json the same way Discord's
# /setlocation already reaches {loc.*} in-game (via RCON push) — see
# _sync_location_from_game / _remove_location_from_game below. Keep this
# format in sync with locations.sk if it ever changes.
LOCSYNC_RE = re.compile(
    r"^\[\d{2}:\d{2}:\d{2}\] \[Server thread/INFO\]: LOCSYNC:([^:]+):([^:]+):(-?\d+):(-?\d+):(-?\d+):(\w+)$"
)
LOCREMOVE_RE = re.compile(r"^\[\d{2}:\d{2}:\d{2}\] \[Server thread/INFO\]: LOCREMOVE:([^:]+):([^:]+)$")

# This Paper build's threaded region scheduler ("Moonrise") intermittently
# dispatches RCON commands onto a worker thread instead of the primary one;
# its own AsyncCatcher then rejects them. Race, not a config/plugin issue —
# a fresh connection's next attempt is usually fine, so retry past it.
_ASYNC_CATCHER_MARKER = "Cannot perform command async"
_RCON_RETRIES = 3

# rcon() signals failure by returning one of these strings instead of
# raising, so callers that need to distinguish success from failure (e.g.
# /megaphone, which otherwise always claimed "Sent" even when the server
# was asleep and nothing actually reached it) must check against this set.
_RCON_ERROR_STRINGS = frozenset({
    "RCON timed out — server may be starting or unresponsive.",
    "RCON auth failed — check MC_RCON_PASSWORD.",
    "Can't reach RCON — is enable-rcon=true and 25575 published?",
})

# EssentialsX overrides /list, so the reply is NOT vanilla's
# "There are 1 of a max of 20 players online:". It reads
# "There are 1 out of maximum 20 players online." with the names on a
# FOLLOWING line, grouped by permission group ("default: Alice, Bob"),
# and the whole thing wrapped in legacy section colour codes. The old
# pattern matched none of that, so parse_list() silently returned 0 --
# /mcstatus always reported an empty server, and worse, /shutdown's
# "players still online" guard never tripped. Accept either wording.
_COLOUR_RE = re.compile("\u00a7.|\x1b\\[[0-9;]*m")
_LIST_RE = re.compile(
    r"There are (\d+) (?:of a max of|out of maximum) (\d+) players online[.:]?\s*(.*)",
    re.DOTALL,
)

# Set via init() from bot.py — needed for the chat bridge and shutdown
# announcements to reach Discord without a circular import.
_client: discord.Client | None = None


def init(client: discord.Client) -> None:
    global _client
    _client = client


async def rcon(cmd: str) -> str:
    try:
        for attempt in range(_RCON_RETRIES):
            async with Client(MC_HOST, MC_RCON_PORT, MC_RCON_PASSWORD) as c:
                resp, _ = await asyncio.wait_for(c.send_cmd(cmd), timeout=5)
            if _ASYNC_CATCHER_MARKER not in (resp or ""):
                return resp or "(no output)"
            if attempt < _RCON_RETRIES - 1:
                await asyncio.sleep(0.3)
        return resp or "(no output)"
    except asyncio.TimeoutError:
        return "RCON timed out — server may be starting or unresponsive."
    except IncorrectPasswordError:
        return "RCON auth failed — check MC_RCON_PASSWORD."
    except RCONConnectionError:
        return "Can't reach RCON — is enable-rcon=true and 25575 published?"


def parse_pos_xyz(resp: str) -> tuple[int, int, int] | None:
    nums = re.findall(r"(-?\d+\.?\d*)d", resp)
    if len(nums) != 3:
        return None
    x, y, z = (round(float(n)) for n in nums)
    return x, y, z


def parse_pos(resp: str) -> str | None:
    xyz = parse_pos_xyz(resp)
    if xyz is None:
        return None
    x, y, z = xyz
    return f"X: {x}  Y: {y}  Z: {z}"


async def mc_status():
    """RCON reachability check. Returns the raw `list` command output if the
    server is awake, or None if it's asleep/unreachable. Never trust port
    25565 here — lazymc answers pings itself even while the container is
    stopped, so a ping always looks "up"."""
    try:
        for attempt in range(_RCON_RETRIES):
            async with Client(MC_HOST, MC_RCON_PORT, MC_RCON_PASSWORD) as c:
                resp, _ = await asyncio.wait_for(c.send_cmd("list"), timeout=5)
            if _ASYNC_CATCHER_MARKER not in (resp or ""):
                return resp
            if attempt < _RCON_RETRIES - 1:
                await asyncio.sleep(0.3)
        return resp
    except (RCONConnectionError, asyncio.TimeoutError, IncorrectPasswordError):
        return None


def parse_list(resp: str) -> tuple[int, list[str]]:
    m = _LIST_RE.search(_COLOUR_RE.sub("", resp or ""))
    if not m:
        return 0, []
    online = int(m.group(1))
    names: list[str] = []
    for line in m.group(3).splitlines():
        # Essentials prefixes each line with its group ("default: a, b");
        # vanilla puts the names straight after the colon on the first line.
        _, _, tail = line.strip().rpartition(":")
        names.extend(n.strip() for n in tail.split(",") if n.strip())
    return online, names


async def stop_minecraft_container(reason: str) -> str:
    """save-all over RCON, then `docker stop` over SSH. Returns a status message."""
    # Best-effort world save first — don't abort the stop if this fails,
    # since `docker stop` sends SIGTERM and the Paper server saves on shutdown
    # anyway (your stop_grace_period: 120s gives it room to finish).
    try:
        async with Client(MC_HOST, MC_RCON_PORT, MC_RCON_PASSWORD) as c:
            await c.send_cmd("save-all")
    except Exception as e:
        log.warning("save-all failed, continuing: %s", e)

    try:
        async with asyncssh.connect(
            MC_SSH_HOST,
            username=MC_SSH_USER,
            client_keys=[MC_SSH_KEY],
            known_hosts=None,
        ) as conn:
            result = await conn.run(f"docker stop {MC_CONTAINER}", check=False)

        if result.exit_status == 0:
            msg = f"🛑 Minecraft server stopped ({reason})."
        else:
            err = (result.stderr or "").strip() or f"exit {result.exit_status}"
            msg = f"⚠️ `docker stop` failed: {err}"
    except Exception as e:
        msg = f"⚠️ Couldn't reach the Minecraft host over SSH: {e}"

    log.info("[mc-shutdown] %s", msg)

    if MC_ANNOUNCE_CHANNEL_ID and _client is not None:
        channel = _client.get_channel(MC_ANNOUNCE_CHANNEL_ID)
        if channel:
            await channel.send(msg)

    return msg


async def start_minecraft_container(reason: str) -> str:
    """Starts the container via the forced-command wake key."""
    try:
        async with asyncssh.connect(
            MC_SSH_HOST,
            username=MC_SSH_USER,
            client_keys=[MC_WAKE_KEY],
            known_hosts=None,
        ) as conn:
            # Argument is ignored — authorized_keys forces `docker start minecraft`
            result = await conn.run("start", check=False)

        if result.exit_status == 0:
            msg = (
                f"▶️ Minecraft server starting ({reason}) — "
                "give it 30-60s before connecting."
            )
        else:
            err = (result.stderr or "").strip() or f"exit {result.exit_status}"
            msg = f"⚠️ Start failed: {err}"
    except Exception as e:
        msg = f"⚠️ Couldn't reach the Minecraft host over SSH: {e}"

    log.info("[mc-start] %s", msg)
    return msg


def _discord_id_for_username(username: str) -> str | None:
    links = storage.load_links()
    return next(
        (did for did, u in links.items() if u.lower() == username.lower()),
        None,
    )


def _sync_location_from_game(username: str, name: str, x: int, y: int, z: int, color: str) -> None:
    """Mirrors an in-game /setlocation into locations.json, the other
    direction of the sync Discord's /setlocation already does via RCON
    push. Silently no-ops for unlinked players — locations.json is keyed
    by Discord ID, so there's nowhere to put it without a link."""
    discord_id = _discord_id_for_username(username)
    if discord_id is None:
        log.info("LOCSYNC for unlinked player %s, skipping", username)
        return
    locations = storage.load_locations()
    user_locs = locations.setdefault(discord_id, {})
    key = name.strip().lower()
    user_locs[key] = {
        "display_name": name.strip(),
        "mc_username": username,
        "x": x, "y": y, "z": z,
        "color": color,
    }
    storage.save_locations(locations)


def _remove_location_from_game(username: str, name: str) -> None:
    discord_id = _discord_id_for_username(username)
    if discord_id is None:
        return
    locations = storage.load_locations()
    key = name.strip().lower()
    if locations.get(discord_id, {}).pop(key, None) is not None:
        storage.save_locations(locations)


async def _bridge_send(text: str) -> None:
    if _client is None:
        return
    channel = _client.get_channel(MC_CHAT_CHANNEL_ID)
    if channel is None:
        try:
            channel = await _client.fetch_channel(MC_CHAT_CHANNEL_ID)
        except discord.HTTPException as e:
            log.warning("mc bridge: can't reach channel %s: %s", MC_CHAT_CHANNEL_ID, e)
            return
    await channel.send(text)


async def mc_chat_bridge():
    """Tails the Paper log over SSH and mirrors chat (and join/leave) into
    Discord, plus applies in-game location sync (LOCSYNC/LOCREMOVE) to
    locations.json. `tail -F` follows by filename, so it survives lazymc
    putting the container to sleep and waking it back up — only an actual
    SSH drop needs a reconnect."""
    assert _client is not None, "mc.init(client) must be called before starting the bridge"
    await _client.wait_until_ready()
    log.info("mc chat bridge starting, channel=%s", MC_CHAT_CHANNEL_ID)

    while not _client.is_closed():
        try:
            async with asyncssh.connect(
                MC_SSH_HOST,
                username=MC_SSH_USER,
                client_keys=[MC_BRIDGE_SSH_KEY],
                known_hosts=None,
                # Without keepalives a dropped connection (NAT/conntrack
                # timeout, network blip) leaves proc.stdout awaiting data
                # that will never arrive — no exception, task hangs forever.
                # These make asyncssh notice and raise so we reconnect.
                keepalive_interval=30,
                keepalive_count_max=3,
            ) as conn:
                async with conn.create_process(f"tail -F -n0 {MC_CHAT_LOG_PATH}") as proc:
                    async for line in proc.stdout:
                        line = line.rstrip("\n")

                        m = CHAT_LINE_RE.match(line)
                        if m:
                            name, msg = m.groups()
                            await _bridge_send(f"**<{name}>** {msg}")
                            continue

                        lm = LOCSYNC_RE.match(line)
                        if lm:
                            username, name, x, y, z, color = lm.groups()
                            _sync_location_from_game(username, name, int(x), int(y), int(z), color)
                            continue

                        rm = LOCREMOVE_RE.match(line)
                        if rm:
                            username, name = rm.groups()
                            _remove_location_from_game(username, name)
                            continue

                        if MC_BRIDGE_JOIN_LEAVE:
                            jm = JOIN_LEAVE_RE.match(line)
                            if jm:
                                name, verb = jm.groups()
                                emoji = "➡️" if verb == "joined" else "⬅️"
                                await _bridge_send(f"{emoji} *{name} {verb} the game*")
        except Exception as e:
            log.warning("mc chat bridge disconnected, retrying in 15s: %s", e)

        await asyncio.sleep(15)


class _StartButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Start server", style=discord.ButtonStyle.success, emoji="▶️")

    async def callback(self, interaction: discord.Interaction):
        self.disabled = True
        await interaction.response.edit_message(view=self.view)
        msg = await start_minecraft_container(f"button, by {interaction.user.display_name}")
        await interaction.followup.send(msg)


class StartView(discord.ui.View):
    """Only one of these should ever be live at a time — see
    _last_start_view below. Disables its own button once idle for 5
    minutes so a dead button doesn't sit in the channel forever."""

    def __init__(self):
        super().__init__(timeout=300)
        self.message: discord.Message | None = None
        self.add_item(_StartButton())

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


# Most recent live "Start server" button, if any. Cleared (button disabled)
# as soon as a newer /mcstatus asleep-response replaces it, so only ever one
# start button is live in the channel at a time instead of piling up.
_last_start_view: StartView | None = None


async def retire_last_start_view() -> None:
    global _last_start_view
    view = _last_start_view
    _last_start_view = None
    if view is None:
        return
    view.stop()
    for item in view.children:
        item.disabled = True
    if view.message is not None:
        try:
            await view.message.edit(view=view)
        except discord.HTTPException:
            pass


def set_last_start_view(view: StartView) -> None:
    global _last_start_view
    _last_start_view = view
