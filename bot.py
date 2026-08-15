"""
Rocket League stats bot.

Slash commands:
    /rlstats <platform> <player>   current ranks from Tracker.gg
    /ping                          liveness check

Env vars (see .env.example):
    DISCORD_TOKEN   bot token from the Developer Portal
    GUILD_ID        your server's ID - scopes commands to it so they
                    appear instantly instead of taking up to an hour
    TRN_API_KEY     Tracker.gg developer key
"""

import json
import re
import asyncssh
import asyncio
import logging
import os
import aiohttp
import discord
from datetime import datetime, timezone
from discord.ext import tasks
from pathlib import Path
from discord import app_commands
from mcstatus import JavaServer
from aiomcrcon import Client, RCONConnectionError, IncorrectPasswordError

MC_HOST = os.getenv("MC_HOST", "192.168.1.183")
MC_RCON_PORT = int(os.getenv("MC_RCON_PORT", "25575"))
MC_RCON_PASSWORD = os.getenv("MC_RCON_PASSWORD")
MC_PORT = int(os.getenv("MC_PORT", "25565"))
MC_SSH_HOST = os.getenv("MC_SSH_USER", MC_HOST)
MC_SSH_USER = os.getenv("MC_SSH_USER", "root")
MC_SSH_KEY = os.getenv("MC_SSH_KEY", "/wake_key")
MC_CONTAINER = os.getenv("MC_CONTAINER", "minecraft")
MC_WAKE_KEY = os.getenv("MC_WAKE_KEY", "/wake_key")
LINKS_PATH = Path("/data/links.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",)
log = logging.getLogger("rlbot")

TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = os.getenv("GUILD_ID")

# Minutes the server must sit empty before it gets stopped
EMPTY_SHUTDOWN_MINUTES = int(os.getenv("MC_EMPTY_SHUTDOWN_MINUTES", "15"))
# How often to poll player count
CHECK_INTERVAL_MINUTES = int(os.getenv("MC_CHECK_INTERVAL_MINUTES", "2"))
# Optional: channel to announce shutdowns in. "0" = stay silent.
MC_ANNOUNCE_CHANNEL_ID = int(os.getenv("MC_ANNOUNCE_CHANNEL_ID", "616023990921723954"))

def load_links() -> dict:
    try:
        return json.loads(LINKS_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_links(links: dict) -> None:
    LINKS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = LINKS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(links, indent=2))
    tmp.replace(LINKS_PATH)


def parse_pos(resp: str) -> str | None:
    nums = re.findall(r"(-?\d+\.?\d*)d", resp)
    if len(nums) != 3:
        return None
    x, y, z = (round(float(n)) for n in nums)
    return f"X: {x}  Y: {y}  Z: {z}"

async def rcon(cmd: str) -> str:
    try:
        async with Client(MC_HOST, MC_RCON_PORT, MC_RCON_PASSWORD) as c:
            resp, _ = await asyncio.wait_for(c.send_cmd(cmd), timeout=5)
            return resp or "(no output)"
    except asyncio.TimeoutError:
        return "RCON timed out."
    except IncorrectPasswordError:
        return "RCON auth failed."
    except RCONConnectionError:
        return "Can't reach RCON."

TRN_API_KEY = os.getenv("TRN_API_KEY", "046ef1ba-317b-4f63-9642-e58570b9a7d0")

TRN_BASE = "https://public-api.tracker.gg/v2/rocket-league/standard/profile"

async def rcon(cmd: str) -> str:
    try:
        async with Client(MC_HOST, MC_RCON_PORT, MC_RCON_PASSWORD) as c:
            resp, _ = await asyncio.wait_for(c.send_cmd(cmd), timeout=5)
            return resp or "(no output)"
    except asyncio.TimeoutError:
        return "RCON timed out — server may be starting or unresponsive."
    except IncorrectPasswordError:
        return "RCON auth failed — check MC_RCON_PASSWORD."
    except RCONConnectionError:
        return "Can't reach RCON — is enable-rcon=true and 25575 published?"
# Tracker returns a segment per playlist. These are the ones worth showing;
# add or remove freely.
PLAYLISTS = [
    "Ranked Duel 1v1",
    "Ranked Doubles 2v2",
    "Ranked Standard 3v3",
    "Tournament Matches",
]

PLATFORM_CHOICES = [
    app_commands.Choice(name="Steam", value="steam"),
    app_commands.Choice(name="Epic Games", value="epic"),
    app_commands.Choice(name="PlayStation", value="psn"),
    app_commands.Choice(name="Xbox", value="xbl"),
]


class TrackerError(Exception):
    """Anything that went wrong talking to Tracker.gg."""


class RLBot(discord.Client):
    def __init__(self):
        # This bot only uses slash commands, so it needs no privileged
        # intents. Keeping them off means nothing to enable in the portal.
        super().__init__(intents=discord.Intents.default() | discord.Intents(members=True))
        self.tree = app_commands.CommandTree(self)
        self.session: aiohttp.ClientSession | None = None

    async def setup_hook(self):
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15),
            headers={"TRN-Api-Key": TRN_API_KEY, "Accept": "application/json"},
        )

        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Commands synced to guild %s", GUILD_ID)
        else:
            await self.tree.sync()
            log.warning("Synced globally - can take up to an hour to appear")

    async def close(self):
        if self.session:
            await self.session.close()
        await super().close()

    async def on_ready(self):
        log.info("Logged in as %s (id %s)", self.user, self.user.id)
        if not idle_shutdown_watcher.is_running():
            idle_shutdown_watcher.start()


client = RLBot()


async def fetch_profile(platform: str, name: str) -> dict:
    """Pull a player profile from Tracker.gg."""
    url = f"{TRN_BASE}/{platform}/{name}"
    assert client.session is not None

    async with client.session.get(url) as resp:
        if resp.status == 404:
            raise TrackerError(
                f"No tracked profile for **{name}** on {platform}. "
                "Tracker only knows players who have appeared on the site."
            )
        if resp.status == 429:
            raise TrackerError("Rate limited by Tracker.gg. Try again shortly.")
        if resp.status in (401, 403):
            raise TrackerError("Tracker.gg rejected the API key.")
        if resp.status != 200:
            raise TrackerError(f"Tracker.gg returned HTTP {resp.status}.")

        return await resp.json()


def build_embed(payload: dict, platform: str) -> discord.Embed:
    data = payload.get("data", {})
    handle = data.get("platformInfo", {}).get("platformUserHandle", "unknown")

    embed = discord.Embed(
        title=handle,
        description=f"Rocket League ranks - {platform}",
        colour=discord.Colour.from_str("#e57000"),
    )

    avatar = data.get("platformInfo", {}).get("avatarUrl")
    if avatar:
        embed.set_thumbnail(url=avatar)

    found = False
    for segment in data.get("segments", []):
        label = segment.get("metadata", {}).get("name")
        if label not in PLAYLISTS:
            continue

        stats = segment.get("stats", {})
        tier = stats.get("tier", {}).get("metadata", {}).get("name", "Unranked")
        division = stats.get("division", {}).get("metadata", {}).get("name", "")
        mmr = stats.get("rating", {}).get("value")

        line = tier if not division else f"{tier} {division}"
        if mmr is not None:
            line += f"\n{int(mmr)} MMR"

        embed.add_field(name=label, value=line, inline=True)
        found = True

    if not found:
        embed.add_field(
            name="No ranked data",
            value="This profile has no tracked competitive playlists.",
            inline=False,
        )

    embed.set_footer(text="Data via Tracker.gg")
    return embed


@client.tree.command(name="rlstats", description="Look up Rocket League ranks")
@app_commands.describe(
    platform="Which platform the account is on",
    player="Username, Epic display name, or Steam ID",
)
@app_commands.choices(platform=PLATFORM_CHOICES)
async def rlstats(
    interaction: discord.Interaction,
    platform: app_commands.Choice[str],
    player: str,
):
    # Discord kills the interaction after 3 seconds. Defer immediately so the
    # HTTP round trip has room to finish.
    await interaction.response.defer()

    try:
        payload = await fetch_profile(platform.value, player)
    except TrackerError as exc:
        await interaction.followup.send(str(exc), ephemeral=True)
        return
    except asyncio.TimeoutError:
        await interaction.followup.send("Tracker.gg timed out.", ephemeral=True)
        return
    except Exception:
        log.exception("Unexpected failure in /rlstats")
        await interaction.followup.send("Something broke. Check the logs.", ephemeral=True)
        return

    await interaction.followup.send(embed=build_embed(payload, platform.name))

@client.tree.command(name="link", description="link your Minecraft username")
@app_commands.describe(username="Your Minecraft username")
async def link_cmd(interaction: discord.Interaction, username: str):
    links = load_links()
    links[str(interaction.user.id)] = username
    save_links(links)
    await interaction.response.send_message(f"Linked to `{username}`", ephemeral=True)


@client.tree.command(name="unlink", description="remove your Minecraft link")
async def unlink_cmd(interaction: discord.Interaction):
    links = load_links()
    if links.pop(str(interaction.user.id), None) is None:
        await interaction.response.send_message("You aren't linked.", ephemeral=True)
        return
    save_links(links)
    await interaction.response.send_message("Unlinked.", ephemeral=True)


@client.tree.command(name="megaphone", description="broadcast message in game")
@app_commands.checks.has_role("Minecraft")
@app_commands.describe(message="What to broadcast in-game")
async def say_cmd(interaction: discord.Interaction, message: str):
    await interaction.response.defer()
    who = interaction.user.display_name
    await rcon(f"say [{who}] {message}")
    await interaction.followup.send(f"Sent: `[{who}] {message}`")

@client.tree.command(name="where", description="get live player coordinates")
@app_commands.describe(username="Minecraft username (optional if linked)")
async def where_cmd(interaction: discord.Interaction, username: str | None = None):
    await interaction.response.defer()

    if username is None:
        username = load_links().get(str(interaction.user.id))
        if username is None:
            await interaction.followup.send("Not linked — run `/link <username>` first.")
            return

    resp = await rcon(f"data get entity {username} Pos")
    pos = parse_pos(resp)
    if pos is None:
        await interaction.followup.send(f"Couldn't find `{username}` — online and spelled right?")
        return
    await interaction.followup.send(f"**{username}**\n{pos}")

@client.tree.command(name="tps", description="Minecraft server tick times")
async def tps_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    resp = await rcon("mspt")
    await interaction.followup.send(f"```{resp}```")

@client.tree.command(name="ping", description="Check the bot is alive")
async def ping(interaction: discord.Interaction):
    latency = round(client.latency * 1000)
    await interaction.response.send_message(f"Up. {latency} ms to Discord.")


 
_empty_since = None  # datetime | None

async def _mc_player_count():
    """Returns online player count, or None if the server is unreachable/offline."""
    try:
        server = await JavaServer.async_lookup(f"{MC_HOST}:{MC_PORT}")
        status = await server.async_status()
        return status.players.online
    except Exception:
        return None

async def stop_minecraft_container(reason: str) -> str:
    """save-all over RCON, then `docker stop` over SSH. Returns a status message."""
    # Best-effort world save first — don't abort the stop if this fails,
    # since `docker stop` sends SIGTERM and the Paper server saves on shutdown
    # anyway (your stop_grace_period: 120s gives it room to finish).
    try:
        async with Client(MC_HOST, MC_RCON_PORT, MC_RCON_PASSWORD) as c:
            await c.send_cmd("save-all")
    except Exception as e:
        print(f"[mc-shutdown] save-all failed, continuing: {e}")
 
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
 
    print(f"[mc-shutdown] {msg}")
 
    if MC_ANNOUNCE_CHANNEL_ID:
        channel = client.get_channel(MC_ANNOUNCE_CHANNEL_ID)
        if channel:
            await channel.send(msg)
 
    return msg
 
 
# --- background watcher ----------------------------------------------------
@tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
async def idle_shutdown_watcher():
    global _empty_since
 
    online = await _mc_player_count()
 
    if online is None:          # server already down — nothing to do
        _empty_since = None
        return
    if online > 0:              # someone's playing — reset the clock
        _empty_since = None
        return
 
    now = datetime.now(timezone.utc)
    if _empty_since is None:
        _empty_since = now
        print(f"[mc-shutdown] Server empty — {EMPTY_SHUTDOWN_MINUTES}m countdown started")
        return
 
    idle_minutes = (now - _empty_since).total_seconds() / 60
    if idle_minutes >= EMPTY_SHUTDOWN_MINUTES:
        await stop_minecraft_container(f"empty for {EMPTY_SHUTDOWN_MINUTES} minutes")
        _empty_since = None
 
 
@idle_shutdown_watcher.before_loop
async def _before_idle_watcher():
    await client.wait_until_ready()
 
async def start_minecraft_container(reason: str) -> str:
    """Starts the container via the forced-command wake key."""
    global _empty_since
 
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
            # Reset the idle clock so the watcher doesn't stop a server that
            # was just started but hasn't had time for anyone to join.
            _empty_since = None
        else:
            err = (result.stderr or "").strip() or f"exit {result.exit_status}"
            msg = f"⚠️ Start failed: {err}"
    except Exception as e:
        msg = f"⚠️ Couldn't reach the Minecraft host over SSH: {e}"
 
    print(f"[mc-start] {msg}")
    return msg
 
 
# --- /start command --------------------------------------------------------
@client.tree.command(name="start", description="Start the Minecraft server")
async def start_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
 
    if await _mc_player_count() is not None:
        await interaction.followup.send("Server is already up.")
        return
 
    result = await start_minecraft_container(f"manual, by {interaction.user.display_name}")
    await interaction.followup.send(result)
 
 
# --- buttons for /mcstatus -------------------------------------------------
class _StartButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Start server", style=discord.ButtonStyle.success, emoji="▶️")
 
    async def callback(self, interaction: discord.Interaction):
        self.disabled = True
        await interaction.response.edit_message(view=self.view)
        msg = await start_minecraft_container(f"button, by {interaction.user.display_name}")
        await interaction.followup.send(msg)
 
 
class _StopButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Shut it down", style=discord.ButtonStyle.danger, emoji="🛑")
 
    async def callback(self, interaction: discord.Interaction):
        # Re-check first — someone may have joined since the status was posted
        online = await _mc_player_count()
        if online:
            await interaction.response.send_message(
                f"⚠️ {online} player(s) joined since then — leaving it up.", ephemeral=True
            )
            return
 
        self.disabled = True
        await interaction.response.edit_message(view=self.view)
        msg = await stop_minecraft_container(f"button, by {interaction.user.display_name}")
        await interaction.followup.send(msg)
 
 
# --- REPLACE your existing /mcstatus with this -----------------------------
@client.tree.command(name="mcstatus", description="Minecraft server status")
async def mcstatus_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
 
    try:
        server = await JavaServer.async_lookup(f"{MC_HOST}:{MC_PORT}")
        s = await server.async_status()
    except Exception as e:
        view = discord.ui.View(timeout=180)
        view.add_item(_StartButton())
        await interaction.followup.send(f"Server appears offline ({e})", view=view)
        return
 
    names = ", ".join(p.name for p in (s.players.sample or [])) or "nobody"
    text = (
        f"**Online:** {s.players.online}/{s.players.max}\n"
        f"**Players:** {names}\n"
        f"**Version:** {s.version.name} — {s.latency:.0f}ms"
    )
 
    if s.players.online == 0:
        view = discord.ui.View(timeout=180)
        view.add_item(_StopButton())
        idle_note = ""
        if _empty_since is not None:
            mins_left = EMPTY_SHUTDOWN_MINUTES - (
                datetime.now(timezone.utc) - _empty_since
            ).total_seconds() / 60
            if mins_left > 0:
                idle_note = f" Auto-shutdown in ~{mins_left:.0f} min."
        await interaction.followup.send(
            f"{text}\n\nNobody's playing.{idle_note}", view=view
        )
    else:
        await interaction.followup.send(text)
 
# --- /shutdown command -----------------------------------------------------
@client.tree.command(name="shutdown", description="Stop the Minecraft server")
async def shutdown_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
 
    online = await _mc_player_count()
 
    if online is None:
        await interaction.followup.send("Server already appears to be offline.")
        return
 
    if online > 0:
        await interaction.followup.send(
            f"⚠️ {online} player(s) still online — not stopping. "
            "Use `/mcstatus` to see who, or wait for the idle timer."
        )
        return
 
    result = await stop_minecraft_container(f"manual, by {interaction.user.display_name}")
    await interaction.followup.send(result)

if __name__ == "__main__":
    client.run(TOKEN, log_handler=None)
