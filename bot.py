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

Two-way MC chat bridge (see .env.example): set MC_CHAT_CHANNEL_ID to the
Discord channel to mirror chat in both directions. Requires the "Message
Content" privileged intent enabled on the Bot page in the Developer Portal.
"""

import json
import re
import asyncssh
import asyncio
import logging
import os
import aiohttp
import discord
from discord import app_commands
from pathlib import Path
from aiomcrcon import Client, RCONConnectionError, IncorrectPasswordError

MC_HOST = os.getenv("MC_HOST", "192.168.1.183")
MC_RCON_PORT = int(os.getenv("MC_RCON_PORT", "25575"))
MC_RCON_PASSWORD = os.getenv("MC_RCON_PASSWORD")
MC_SSH_HOST = os.getenv("MC_SSH_USER", MC_HOST)
MC_SSH_USER = os.getenv("MC_SSH_USER", "root")
MC_SSH_KEY = os.getenv("MC_SSH_KEY", "/wake_key")
MC_CONTAINER = os.getenv("MC_CONTAINER", "minecraft")
MC_WAKE_KEY = os.getenv("MC_WAKE_KEY", "/wake_key")
LINKS_PATH = Path("/data/links.json")

# Two-way Discord <-> Minecraft chat bridge. "0" = disabled.
MC_CHAT_CHANNEL_ID = int(os.getenv("MC_CHAT_CHANNEL_ID", "0"))
MC_CHAT_LOG_PATH = os.getenv("MC_CHAT_LOG_PATH", "/root/minecraft-server/data/logs/latest.log")
MC_BRIDGE_JOIN_LEAVE = os.getenv("MC_BRIDGE_JOIN_LEAVE", "true").lower() == "true"
# stop_key is unrestricted (unlike the forced-command wake_key), so it's the
# one that can run an arbitrary `tail -F` instead of just `docker start`.
MC_BRIDGE_SSH_KEY = os.getenv("MC_BRIDGE_SSH_KEY", "/stop_key")

CHAT_LINE_RE = re.compile(r"^\[\d{2}:\d{2}:\d{2}\] \[[^\]]*\]: <([^>]+)> (.*)$")
JOIN_LEAVE_RE = re.compile(r"^\[\d{2}:\d{2}:\d{2}\] \[Server thread/INFO\]: (\S+) (joined|left) the game$")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",)
log = logging.getLogger("rlbot")

TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = os.getenv("GUILD_ID")

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

TRN_API_KEY = os.getenv("TRN_API_KEY", "046ef1ba-317b-4f63-9642-e58570b9a7d0")

TRN_BASE = "https://public-api.tracker.gg/v2/rocket-league/standard/profile"

# This Paper build's threaded region scheduler ("Moonrise") intermittently
# dispatches RCON commands onto a worker thread instead of the primary one;
# its own AsyncCatcher then rejects them. Race, not a config/plugin issue —
# a fresh connection's next attempt is usually fine, so retry past it.
_ASYNC_CATCHER_MARKER = "Cannot perform command async"
_RCON_RETRIES = 3


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
        # message_content is a privileged intent needed to read chat text
        # for the MC bridge — must also be enabled on the Bot page in the
        # Developer Portal, or on_message sees empty content.
        super().__init__(
            intents=discord.Intents.default()
            | discord.Intents(members=True, message_content=True)
        )
        self.tree = app_commands.CommandTree(self)
        self.session: aiohttp.ClientSession | None = None

    async def setup_hook(self):
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15),
            headers={"TRN-Api-Key": TRN_API_KEY, "Accept": "application/json"},
        )

        if MC_CHAT_CHANNEL_ID:
            self.loop.create_task(mc_chat_bridge())

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

    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if not MC_CHAT_CHANNEL_ID or message.channel.id != MC_CHAT_CHANNEL_ID:
            return

        # clean_content resolves @mentions/#channels to readable names.
        text = message.clean_content.strip().replace("\n", " ")
        if not text:
            if not message.attachments:
                return
            text = "[attachment]"
        text = text[:400]

        who = message.author.display_name
        payload = json.dumps({"text": f"[Discord] {who}: {text}", "color": "aqua"})
        await rcon(f"tellraw @a {payload}")


client = RLBot()


async def _bridge_send(text: str) -> None:
    channel = client.get_channel(MC_CHAT_CHANNEL_ID)
    if channel is None:
        try:
            channel = await client.fetch_channel(MC_CHAT_CHANNEL_ID)
        except discord.HTTPException as e:
            log.warning("mc bridge: can't reach channel %s: %s", MC_CHAT_CHANNEL_ID, e)
            return
    await channel.send(text)


async def mc_chat_bridge():
    """Tails the Paper log over SSH and mirrors chat (and join/leave) into
    Discord. `tail -F` follows by filename, so it survives lazymc putting the
    container to sleep and waking it back up — only an actual SSH drop needs
    a reconnect."""
    await client.wait_until_ready()
    log.info("mc chat bridge starting, channel=%s", MC_CHAT_CHANNEL_ID)

    while not client.is_closed():
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

                        if MC_BRIDGE_JOIN_LEAVE:
                            jm = JOIN_LEAVE_RE.match(line)
                            if jm:
                                name, verb = jm.groups()
                                emoji = "➡️" if verb == "joined" else "⬅️"
                                await _bridge_send(f"{emoji} *{name} {verb} the game*")
        except Exception as e:
            log.warning("mc chat bridge disconnected, retrying in 15s: %s", e)

        await asyncio.sleep(15)


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



_LIST_RE = re.compile(r"There are (\d+) of a max of \d+ players online:?\s*(.*)")


async def _mc_status():
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


def _parse_list(resp: str) -> tuple[int, list[str]]:
    m = _LIST_RE.search(resp or "")
    if not m:
        return 0, []
    online = int(m.group(1))
    names = [n.strip() for n in m.group(2).split(",") if n.strip()]
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

    print(f"[mc-start] {msg}")
    return msg


# --- /start command --------------------------------------------------------
@client.tree.command(name="start", description="Start the Minecraft server")
async def start_cmd(interaction: discord.Interaction):
    await interaction.response.defer()

    if await _mc_status() is not None:
        await interaction.followup.send("Server is already up.")
        return

    result = await start_minecraft_container(f"manual, by {interaction.user.display_name}")
    await interaction.followup.send(result)


# --- button for /mcstatus ---------------------------------------------------
class _StartButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Start server", style=discord.ButtonStyle.success, emoji="▶️")

    async def callback(self, interaction: discord.Interaction):
        self.disabled = True
        await interaction.response.edit_message(view=self.view)
        msg = await start_minecraft_container(f"button, by {interaction.user.display_name}")
        await interaction.followup.send(msg)


# --- /mcstatus command -------------------------------------------------
@client.tree.command(name="mcstatus", description="Minecraft server status")
async def mcstatus_cmd(interaction: discord.Interaction):
    await interaction.response.defer()

    resp = await _mc_status()
    if resp is None:
        view = discord.ui.View(timeout=180)
        view.add_item(_StartButton())
        await interaction.followup.send(
            "Server is asleep — nobody's connected. lazymc will spin it up "
            "when someone joins, or use the button below.",
            view=view,
        )
        return

    online, names = _parse_list(resp)
    names_text = ", ".join(names) if names else "nobody"
    text = f"**Online:** {online}\n**Players:** {names_text}"
    await interaction.followup.send(text)

# --- /shutdown command -----------------------------------------------------
@client.tree.command(name="shutdown", description="Stop the Minecraft server")
async def shutdown_cmd(interaction: discord.Interaction):
    await interaction.response.defer()

    resp = await _mc_status()

    if resp is None:
        await interaction.followup.send("Server already appears to be offline.")
        return

    online, _ = _parse_list(resp)
    if online > 0:
        await interaction.followup.send(
            f"⚠️ {online} player(s) still online — not stopping. "
            "Use `/mcstatus` to see who."
        )
        return

    result = await stop_minecraft_container(f"manual, by {interaction.user.display_name}")
    await interaction.followup.send(result)

if __name__ == "__main__":
    client.run(TOKEN, log_handler=None)
