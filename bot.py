"""
Rocket League stats bot.

Slash commands:
    /rlstats <platform> <player>   current ranks from Tracker.gg
    /balance [user] [username]     someone's in-game money
    /balancetop                    the richest players
    /bounty <player> <amount>      put a price on someone's head
    /bounties [player]             the bounty board
    /economy                       how the in-game money system works
    /request <feature>             ask for a feature; /requests lists them
    /ping                          liveness check

Env vars (see .env.example):
    DISCORD_TOKEN   bot token from the Developer Portal
    GUILD_ID        your server's ID - scopes commands to it so they
                    appear instantly instead of taking up to an hour
    TRN_API_KEY     Tracker.gg developer key

Two-way MC chat bridge (see .env.example): set MC_CHAT_CHANNEL_ID to the
Discord channel to mirror chat in both directions. Requires the "Message
Content" privileged intent enabled on the Bot page in the Developer Portal.

Structure: this file is the Discord-facing half only — the DiscordBot class and
every /command handler. Everything Minecraft-connectivity-related (RCON,
container start/stop, the chat bridge + location sync) lives in mc.py;
Tracker.gg lookups live in tracker.py; JSON persistence lives in storage.py.
"""

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone

import aiohttp
import discord
from discord import app_commands

import mc
import storage
import tracker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
log = logging.getLogger("discordbot")

TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = os.getenv("GUILD_ID")
# LAN address until map.mrmake.org's Cloudflare hostname is live, then switch
# this to "https://map.mrmake.org".
MC_MAP_BASE_URL = os.getenv("MC_MAP_BASE_URL", "http://192.168.1.183:8100")
JOIN_MESSAGE_MAX_LEN = 100

PLATFORM_CHOICES = [
    app_commands.Choice(name="Steam", value="steam"),
    app_commands.Choice(name="Epic Games", value="epic"),
    app_commands.Choice(name="PlayStation", value="psn"),
    app_commands.Choice(name="Xbox", value="xbl"),
]

# value = Minecraft legacy color code (used as `&<code>` in the in-game HUD).
LOCATION_COLOR_CHOICES = [
    app_commands.Choice(name="Yellow", value="e"),
    app_commands.Choice(name="Red", value="c"),
    app_commands.Choice(name="Green", value="a"),
    app_commands.Choice(name="Aqua", value="b"),
    app_commands.Choice(name="Blue", value="9"),
    app_commands.Choice(name="Purple", value="d"),
    app_commands.Choice(name="Gold", value="6"),
    app_commands.Choice(name="White", value="f"),
]
DEFAULT_LOCATION_COLOR = "e"


class DiscordBot(discord.Client):
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
            headers={"TRN-Api-Key": tracker.TRN_API_KEY, "Accept": "application/json"},
        )

        if mc.MC_CHAT_CHANNEL_ID:
            self.loop.create_task(mc.mc_chat_bridge())

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
        if not mc.MC_CHAT_CHANNEL_ID or message.channel.id != mc.MC_CHAT_CHANNEL_ID:
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
        await mc.rcon(f"tellraw @a {payload}")


client = DiscordBot()
mc.init(client)


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
        payload = await tracker.fetch_profile(client.session, platform.value, player)
    except tracker.TrackerError as exc:
        await interaction.followup.send(str(exc), ephemeral=True)
        return
    except asyncio.TimeoutError:
        await interaction.followup.send("Tracker.gg timed out.", ephemeral=True)
        return
    except Exception:
        log.exception("Unexpected failure in /rlstats")
        await interaction.followup.send("Something broke. Check the logs.", ephemeral=True)
        return

    await interaction.followup.send(embed=tracker.build_embed(payload, platform.name))


@client.tree.command(name="link", description="link your Minecraft username")
@app_commands.describe(username="Your Minecraft username")
async def link_cmd(interaction: discord.Interaction, username: str):
    links = storage.load_links()
    links[str(interaction.user.id)] = username
    storage.save_links(links)
    await interaction.response.send_message(f"Linked to `{username}`", ephemeral=True)


@client.tree.command(name="unlink", description="remove your Minecraft link")
async def unlink_cmd(interaction: discord.Interaction):
    links = storage.load_links()
    if links.pop(str(interaction.user.id), None) is None:
        await interaction.response.send_message("You aren't linked.", ephemeral=True)
        return
    storage.save_links(links)
    await interaction.response.send_message("Unlinked.", ephemeral=True)


@client.tree.command(name="megaphone", description="broadcast message in game")
@app_commands.checks.has_role("Minecraft")
@app_commands.describe(message="What to broadcast in-game")
async def say_cmd(interaction: discord.Interaction, message: str):
    await interaction.response.defer()
    who = interaction.user.display_name
    resp = await mc.rcon(f"say [{who}] {message}")
    if resp in mc._RCON_ERROR_STRINGS:
        await interaction.followup.send(resp)
        return
    await interaction.followup.send(f"Sent: `[{who}] {message}`")


@client.tree.command(name="find", description="Find a player's live position, or one of their saved locations")
@app_commands.describe(
    user="Whose position/location — @ mention a linked Discord member (optional, defaults to you)",
    username="Or a Minecraft username directly, if they're not on Discord",
    location="A saved location name — omit for their live position instead",
)
async def find_cmd(
    interaction: discord.Interaction,
    user: discord.Member | None = None,
    username: str | None = None,
    location: str | None = None,
):
    await interaction.response.defer()

    discord_id: str | None = None
    if user is not None:
        mc_username = storage.load_links().get(str(user.id))
        if mc_username is None:
            await interaction.followup.send(f"{user.mention} isn't linked — they need to run `/link <username>`.")
            return
        discord_id = str(user.id)
    elif username is not None:
        mc_username = username
    else:
        mc_username = storage.load_links().get(str(interaction.user.id))
        if mc_username is None:
            await interaction.followup.send("Not linked — run `/link <username>` first, or give a username.")
            return
        discord_id = str(interaction.user.id)

    if location:
        key = location.strip().lower()
        locations = storage.load_locations()
        if discord_id is not None:
            entry = locations.get(discord_id, {}).get(key)
        else:
            entry = next(
                (
                    locs[key] for locs in locations.values()
                    if key in locs and locs[key]["mc_username"].lower() == mc_username.lower()
                ),
                None,
            )
        if entry is None:
            await interaction.followup.send(f"No saved `{location}` for `{mc_username}`.")
            return
        await interaction.followup.send(
            f"📍 **{entry['display_name']}** ({entry['mc_username']})\n"
            f"X: {entry['x']}  Y: {entry['y']}  Z: {entry['z']}"
        )
        return

    resp = await mc.rcon(f"data get entity {mc_username} Pos")
    pos = mc.parse_pos(resp)
    if pos is None:
        await interaction.followup.send(f"Couldn't find `{mc_username}` — online and spelled right?")
        return
    await interaction.followup.send(f"**{mc_username}**\n{pos}")


@client.tree.command(name="setlocation", description="Save your current in-game position as a named location")
@app_commands.describe(
    name="A short name for this spot (e.g. house, mine, base)",
    color="HUD marker color in-game (default yellow)",
)
@app_commands.choices(color=LOCATION_COLOR_CHOICES)
async def setlocation_cmd(
    interaction: discord.Interaction,
    name: str,
    color: app_commands.Choice[str] | None = None,
):
    await interaction.response.defer()

    username = storage.load_links().get(str(interaction.user.id))
    if username is None:
        await interaction.followup.send("Not linked — run `/link <username>` first.")
        return

    resp = await mc.rcon(f"data get entity {username} Pos")
    xyz = mc.parse_pos_xyz(resp)
    if xyz is None:
        await interaction.followup.send(f"Couldn't find `{username}` — online and spelled right?")
        return

    x, y, z = xyz
    color_code = color.value if color else DEFAULT_LOCATION_COLOR
    key = name.strip().lower()

    locations = storage.load_locations()
    user_locs = locations.setdefault(str(interaction.user.id), {})
    user_locs[key] = {
        "display_name": name.strip(),
        "mc_username": username,
        "x": x, "y": y, "z": z,
        "color": color_code,
    }
    storage.save_locations(locations)

    # Push it into the in-game HUD too — see locations.sk on the MC server,
    # which drives the sidebar off this variable store. In-game /setlocation
    # pushes back the other way (LOCSYNC over the chat bridge, see mc.py) so
    # the two stay in sync regardless of which side you save from.
    push_resp = await mc.rcon(
        f"setlocationmc {username} {key} {x} {y} {z} {mc.MC_WORLD_NAME} {color_code}"
    )
    hud_note = "" if push_resp not in mc._RCON_ERROR_STRINGS else "\n⚠️ HUD marker didn't sync — server may be asleep."

    await interaction.followup.send(
        f"📍 Saved **{name.strip()}** at X: {x}  Y: {y}  Z: {z}{hud_note}"
    )


@client.tree.command(name="removelocation", description="Remove one of your saved locations")
@app_commands.describe(name="The location's name to remove")
async def removelocation_cmd(interaction: discord.Interaction, name: str):
    await interaction.response.defer()

    username = storage.load_links().get(str(interaction.user.id))
    if username is None:
        await interaction.followup.send("Not linked — run `/link <username>` first.")
        return

    key = name.strip().lower()
    locations = storage.load_locations()
    user_locs = locations.get(str(interaction.user.id), {})
    if key not in user_locs:
        await interaction.followup.send(f"You don't have a location named `{name}`.")
        return

    del user_locs[key]
    storage.save_locations(locations)

    push_resp = await mc.rcon(f"removelocationmc {username} {key}")
    hud_note = "" if push_resp not in mc._RCON_ERROR_STRINGS else "\n⚠️ HUD marker removal didn't sync — server may be asleep."

    await interaction.followup.send(f"🗑️ Removed **{name.strip()}**{hud_note}")


@client.tree.command(name="locations", description="List saved location names")
@app_commands.describe(
    user="Whose locations — @ mention a linked Discord member (optional, defaults to you)",
    username="Or a Minecraft username directly, if they're not on Discord",
)
async def locations_cmd(
    interaction: discord.Interaction,
    user: discord.Member | None = None,
    username: str | None = None,
):
    await interaction.response.defer()

    locations = storage.load_locations()

    if user is not None:
        user_locs = locations.get(str(user.id), {})
        if not user_locs:
            await interaction.followup.send(f"{user.mention} hasn't saved any locations.")
            return
    elif username is not None:
        user_locs = next(
            (
                locs for locs in locations.values()
                if any(v["mc_username"].lower() == username.lower() for v in locs.values())
            ),
            {},
        )
        if not user_locs:
            await interaction.followup.send(f"No saved locations for `{username}`.")
            return
    else:
        user_locs = locations.get(str(interaction.user.id), {})
        if not user_locs:
            await interaction.followup.send("You haven't saved any locations — try `/setlocation <name>`.")
            return

    lines = [f"• **{v['display_name']}** — X: {v['x']}  Y: {v['y']}  Z: {v['z']}" for v in user_locs.values()]
    await interaction.followup.send("\n".join(lines))


@client.tree.command(name="joinmessage", description="Set the message broadcast in-game when you join the Minecraft server")
@app_commands.describe(message=f"Your custom join message (max {JOIN_MESSAGE_MAX_LEN} chars)")
async def joinmessage_cmd(interaction: discord.Interaction, message: str):
    await interaction.response.defer(ephemeral=True)

    username = storage.load_links().get(str(interaction.user.id))
    if username is None:
        await interaction.followup.send("Not linked — run `/link <username>` first.", ephemeral=True)
        return

    text = " ".join(message.split())  # collapse newlines/whitespace so it stays a single RCON line
    if not text:
        await interaction.followup.send("Message can't be empty.", ephemeral=True)
        return
    if len(text) > JOIN_MESSAGE_MAX_LEN:
        await interaction.followup.send(
            f"Message is too long ({len(text)}/{JOIN_MESSAGE_MAX_LEN} chars).", ephemeral=True
        )
        return

    messages = storage.load_joinmessages()
    messages[str(interaction.user.id)] = {"mc_username": username, "message": text}
    storage.save_joinmessages(messages)

    push_resp = await mc.rcon(f"setjoinmessagemc {username} {text}")
    hud_note = "" if push_resp not in mc._RCON_ERROR_STRINGS else "\n⚠️ Didn't sync to the server — it may be asleep. Run this again once it's up."

    await interaction.followup.send(f"✅ Join message set to: {text}{hud_note}", ephemeral=True)


@client.tree.command(name="removejoinmessage", description="Reset your Minecraft join message to the default")
async def removejoinmessage_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    username = storage.load_links().get(str(interaction.user.id))
    if username is None:
        await interaction.followup.send("Not linked — run `/link <username>` first.", ephemeral=True)
        return

    messages = storage.load_joinmessages()
    if messages.pop(str(interaction.user.id), None) is None:
        await interaction.followup.send("You don't have a custom join message set.", ephemeral=True)
        return
    storage.save_joinmessages(messages)

    push_resp = await mc.rcon(f"removejoinmessagemc {username}")
    hud_note = "" if push_resp not in mc._RCON_ERROR_STRINGS else "\n⚠️ Didn't sync to the server — it may be asleep. Run this again once it's up."

    await interaction.followup.send(f"🗑️ Join message reset to default.{hud_note}", ephemeral=True)


@client.tree.command(name="map", description="Get a link to the live map centered on a spot")
@app_commands.describe(
    x="X coordinate (skip this and z to use a player/location instead)",
    z="Z coordinate",
    user="Whose position — @ mention a linked Discord member (optional, defaults to you)",
    username="Or a Minecraft username directly",
    location="A saved location name instead of a live position",
)
async def map_cmd(
    interaction: discord.Interaction,
    x: int | None = None,
    z: int | None = None,
    user: discord.Member | None = None,
    username: str | None = None,
    location: str | None = None,
):
    await interaction.response.defer()

    if x is not None and z is not None:
        await interaction.followup.send(f"🗺️ {MC_MAP_BASE_URL}/#overworld:{x}:64:{z}:400:0:0:0:0:perspective")
        return

    discord_id: str | None = None
    mc_username: str | None = None
    if user is not None:
        mc_username = storage.load_links().get(str(user.id))
        if mc_username is None:
            await interaction.followup.send(f"{user.mention} isn't linked — they need to run `/link <username>`.")
            return
        discord_id = str(user.id)
    elif username is not None:
        mc_username = username
    elif location is None:
        mc_username = storage.load_links().get(str(interaction.user.id))
        if mc_username is None:
            await interaction.followup.send("Not linked — run `/link <username>` first, give a username, or give x/z.")
            return
        discord_id = str(interaction.user.id)

    if location:
        key = location.strip().lower()
        locations = storage.load_locations()
        if discord_id is not None:
            entry = locations.get(discord_id, {}).get(key)
        elif mc_username is not None:
            entry = next(
                (
                    locs[key] for locs in locations.values()
                    if key in locs and locs[key]["mc_username"].lower() == mc_username.lower()
                ),
                None,
            )
        else:
            entry = locations.get(str(interaction.user.id), {}).get(key)
        if entry is None:
            await interaction.followup.send(f"No saved `{location}` found.")
            return
        await interaction.followup.send(
            f"🗺️ **{entry['display_name']}**\n"
            f"{MC_MAP_BASE_URL}/#overworld:{entry['x']}:64:{entry['z']}:400:0:0:0:0:perspective"
        )
        return

    resp = await mc.rcon(f"data get entity {mc_username} Pos")
    xyz = mc.parse_pos_xyz(resp)
    if xyz is None:
        await interaction.followup.send(f"Couldn't find `{mc_username}` — online and spelled right?")
        return
    px, _, pz = xyz
    await interaction.followup.send(f"🗺️ **{mc_username}**\n{MC_MAP_BASE_URL}/#overworld:{px}:64:{pz}:400:0:0:0:0:perspective")


@client.tree.command(name="tps", description="Minecraft server tick times")
async def tps_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    resp = await mc.rcon("mspt")
    await interaction.followup.send(f"```{resp}```")


@client.tree.command(name="clear", description="Delete this bot's recent messages in this channel")
@app_commands.describe(amount="How many recent messages to scan for bot messages to delete (default 20, max 100)")
@app_commands.default_permissions(manage_messages=True)
async def clear_cmd(interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100] = 20):
    await interaction.response.defer(ephemeral=True)

    def is_bot_msg(m: discord.Message) -> bool:
        return m.author.id == client.user.id

    try:
        deleted = await interaction.channel.purge(limit=amount, check=is_bot_msg)
    except discord.Forbidden:
        await interaction.followup.send(
            "Missing Manage Messages permission in this channel.", ephemeral=True
        )
        return
    except discord.HTTPException as e:
        await interaction.followup.send(f"Couldn't delete messages: {e}", ephemeral=True)
        return

    await interaction.followup.send(f"Deleted {len(deleted)} message(s).", ephemeral=True)


@client.tree.command(name="ping", description="Check the bot is alive")
async def ping(interaction: discord.Interaction):
    latency = round(client.latency * 1000)
    await interaction.response.send_message(f"Up. {latency} ms to Discord.")


@client.tree.command(name="start", description="Start the Minecraft server")
async def start_cmd(interaction: discord.Interaction):
    await interaction.response.defer()

    if await mc.mc_status() is not None:
        await interaction.followup.send("Server is already up.")
        return

    result = await mc.start_minecraft_container(f"manual, by {interaction.user.display_name}")
    await interaction.followup.send(result)


@client.tree.command(name="mcstatus", description="Minecraft server status")
async def mcstatus_cmd(interaction: discord.Interaction):
    await interaction.response.defer()

    resp = await mc.mc_status()
    if resp is None:
        await mc.retire_last_start_view()

        view = mc.StartView()
        msg = await interaction.followup.send(
            "Server is asleep — nobody's connected. lazymc will spin it up "
            "when someone joins, or use the button below.",
            view=view,
        )
        view.message = msg
        mc.set_last_start_view(view)
        return

    online, names = mc.parse_list(resp)
    names_text = ", ".join(names) if names else "nobody"
    text = f"**Online:** {online}\n**Players:** {names_text}"
    await interaction.followup.send(text)


@client.tree.command(name="shutdown", description="Stop the Minecraft server")
async def shutdown_cmd(interaction: discord.Interaction):
    await interaction.response.defer()

    resp = await mc.mc_status()

    if resp is None:
        await interaction.followup.send("Server already appears to be offline.")
        return

    online, _ = mc.parse_list(resp)
    if online > 0:
        await interaction.followup.send(
            f"⚠️ {online} player(s) still online — not stopping. "
            "Use `/mcstatus` to see who."
        )
        return

    result = await mc.stop_minecraft_container(f"manual, by {interaction.user.display_name}")
    await interaction.followup.send(result)


# --- money and bounties -----------------------------------------------------
# All four of these read live state off the Minecraft server, so unlike
# /economy (static text, deliberately, so it still answers while the server
# sleeps) they need it awake. RCON on 25575 does NOT wake a sleeping server —
# only a login handshake on 25565 does — so an asleep server fails cleanly
# here instead of costing a 60s boot nobody asked for.
#
# /bounty and /bounties bridge to bountymc / bountylistmc, the console-only
# half of bounty.sk on CT 101. Those reply with colon-delimited records in
# the RCON response body; the tags and field order below have to stay in
# sync with that script.

BOUNTY_MIN = 50      # mirrors {@MIN} in bounty.sk
BOUNTY_MAX = 5000    # mirrors {@MAX} in bounty.sk
BOUNTY_LIST_LIMIT = 15
BOUNTY_MARK = "☠"

_BOUNTY_TAGS = frozenset({
    "BOUNTY", "BOUNTYEND", "BOUNTYON", "BOUNTYFROM",
    "BOUNTYNONE", "BOUNTYOK", "BOUNTYERR",
})

# Every RCON command below interpolates a username into a command string, so
# anything that isn't a real Minecraft name is rejected before it gets there
# — a value with a space in it would otherwise arrive as extra arguments.
_MC_NAME_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")

_BALTOP_ROW_RE = re.compile(r"^\s*(\d+)\.\s*(\S+?),\s*\$([\d,]+(?:\.\d+)?)\s*$")
_BALANCE_RE = re.compile(r"Balance of\s+(\S+?)\s*:\s*\$([\d,]+(?:\.\d+)?)")


def _linked_username(user: discord.abc.User) -> str | None:
    return storage.load_links().get(str(user.id))


def _fmt_money(value) -> str:
    """Skript sends plain numbers ("125", "917.5"); Essentials prints its own
    already-formatted string. Render both the way the game does — thousands
    separators, and cents only when there are any."""
    try:
        n = float(str(value).replace(",", ""))
    except ValueError:
        return str(value)
    return f"${n:,.2f}" if n % 1 else f"${n:,.0f}"


def _parse_bounty_reply(resp: str) -> list[tuple[str, list[str]]]:
    """Pulls the BOUNTY* records out of an RCON reply, ignoring every other
    line. `broadcast` output lands in the same response body, so this cannot
    just split the whole thing on newlines and trust it."""
    records = []
    for line in mc.strip_colors(resp).splitlines():
        tag, sep, rest = line.strip().partition(":")
        if sep and tag in _BOUNTY_TAGS:
            records.append((tag, rest.split(":")))
    return records


async def _resolve_target(interaction: discord.Interaction, player: str) -> str | None:
    """Validates a username argument, replying with the reason if it's not
    usable. Returns None when the caller should stop."""
    name = player.strip()
    if not _MC_NAME_RE.match(name):
        await interaction.followup.send(
            f"`{name[:32]}` isn't a valid Minecraft username.", ephemeral=True
        )
        return None
    return name


async def _linked_username_or_reply(interaction: discord.Interaction) -> str | None:
    username = _linked_username(interaction.user)
    if username is None:
        await interaction.followup.send(
            "You aren't linked — run `/link <username>` first so I know which "
            "in-game account to charge.",
            ephemeral=True,
        )
    return username


async def _link_autocomplete(interaction: discord.Interaction, current: str):
    """Suggests usernames from links.json only — deliberately no RCON call.
    Discord gives autocomplete about 3 seconds and mc.rcon alone can take 5,
    so a round trip to the server here would just time out the whole box."""
    names = sorted(set(storage.load_links().values()))
    current = current.lower()
    return [
        app_commands.Choice(name=n, value=n)
        for n in names if current in n.lower()
    ][:25]


@client.tree.command(name="bounty", description="Put money on a player's head, paid to whoever kills them")
@app_commands.describe(
    player="Minecraft username to put the bounty on",
    amount=f"How much to add (${BOUNTY_MIN}-${BOUNTY_MAX}) — taken from your balance immediately",
)
@app_commands.autocomplete(player=_link_autocomplete)
async def bounty_cmd(
    interaction: discord.Interaction,
    player: str,
    amount: app_commands.Range[int, BOUNTY_MIN, BOUNTY_MAX],
):
    await interaction.response.defer()

    placer = await _linked_username_or_reply(interaction)
    if placer is None:
        return
    target = await _resolve_target(interaction, player)
    if target is None:
        return

    resp = await mc.rcon(f"bountymc {placer} {target} {amount}")
    if resp in mc._RCON_ERROR_STRINGS:
        await interaction.followup.send(resp, ephemeral=True)
        return

    records = dict(_parse_bounty_reply(resp))

    if "BOUNTYERR" in records:
        fields = records["BOUNTYERR"]
        reason, detail = fields[0], (fields[1] if len(fields) > 1 else "")
        messages = {
            "self": "You can't put a price on your own head.",
            "noplacer": f"Your linked username `{placer}` has never joined the server — "
                        "`/link` it to the right name.",
            "notarget": f"No player called `{detail}` has ever joined the server.",
            "funds": f"Not enough money — you have {_fmt_money(detail)}.",
            "min": f"Minimum bounty is {_fmt_money(detail)}.",
            "max": f"Most you can add at once is {_fmt_money(detail)}.",
        }
        await interaction.followup.send(
            messages.get(reason, f"Couldn't place that bounty (`{reason}`)."), ephemeral=True
        )
        return

    if "BOUNTYOK" not in records:
        log.warning("unparseable bountymc reply: %r", resp)
        await interaction.followup.send(
            "The server didn't confirm that — check `/bounties` before trying again.",
            ephemeral=True,
        )
        return

    name, added, pot = records["BOUNTYOK"][:3]
    log.info("bounty %s -> %s %s (pot %s)", placer, name, added, pot)
    await interaction.followup.send(
        f"{BOUNTY_MARK} **{placer}** put {_fmt_money(added)} on **{name}**'s head.\n"
        f"Total on them now: **{_fmt_money(pot)}** — paid to whoever kills them, "
        "less the 5% house cut.\n"
        f"-# `/bounties` to see the board."
    )


@client.tree.command(name="bounties", description="List the bounties standing on people's heads")
@app_commands.describe(player="Show who is funding the bounty on one player (optional)")
@app_commands.autocomplete(player=_link_autocomplete)
async def bounties_cmd(interaction: discord.Interaction, player: str | None = None):
    await interaction.response.defer()

    if player is not None:
        target = await _resolve_target(interaction, player)
        if target is None:
            return
        resp = await mc.rcon(f"bountylistmc {target}")
    else:
        resp = await mc.rcon("bountylistmc")

    if resp in mc._RCON_ERROR_STRINGS:
        await interaction.followup.send(resp, ephemeral=True)
        return

    records = _parse_bounty_reply(resp)
    tags = {tag for tag, _ in records}

    if "BOUNTYNONE" in tags:
        await interaction.followup.send(
            f"No bounty on `{player}`. `/bounty {player} <amount>` starts one.",
            ephemeral=True,
        )
        return

    if player is not None:
        header = next(f for tag, f in records if tag == "BOUNTYON")
        funders = [f for tag, f in records if tag == "BOUNTYFROM"]
        body = "\n".join(
            f"• **{who}** — {_fmt_money(amt)}" for who, amt in (f[:2] for f in funders)
        )
        embed = discord.Embed(
            title=f"{BOUNTY_MARK} {header[0]} — {_fmt_money(header[1])}",
            description=f"Funded by:\n{body}\n\n"
                        "-# Kill them and it's yours, less the 5% house cut.",
        )
        await interaction.followup.send(embed=embed)
        return

    entries = [f[:2] for tag, f in records if tag == "BOUNTY"]
    if not entries:
        await interaction.followup.send(
            "Nobody has a price on their head. `/bounty <player> <amount>` fixes that.",
            ephemeral=True,
        )
        return

    # Biggest first — bounty.sk emits them in variable order, not by size.
    entries.sort(key=lambda e: float(e[1]), reverse=True)
    shown = entries[:BOUNTY_LIST_LIMIT]
    body = "\n".join(
        f"**{i}.** {name} — {_fmt_money(amt)}"
        for i, (name, amt) in enumerate(shown, 1)
    )
    if len(entries) > len(shown):
        body += f"\n-# ...and {len(entries) - len(shown)} smaller one(s) not shown."

    end = next((f for tag, f in records if tag == "BOUNTYEND"), None)
    if end and len(end) > 1:
        body += f"\n\n-# {end[0]} standing · {_fmt_money(end[1])} on the table"

    embed = discord.Embed(title=f"{BOUNTY_MARK} Standing bounties", description=body)
    await interaction.followup.send(embed=embed)


@client.tree.command(name="balance", description="Check someone's in-game money")
@app_commands.describe(
    user="@ mention a linked Discord member (optional, defaults to you)",
    username="Or a Minecraft username directly",
)
@app_commands.autocomplete(username=_link_autocomplete)
async def balance_cmd(
    interaction: discord.Interaction,
    user: discord.Member | None = None,
    username: str | None = None,
):
    await interaction.response.defer()

    if user is not None:
        mc_username = _linked_username(user)
        if mc_username is None:
            await interaction.followup.send(
                f"{user.mention} isn't linked — they need to run `/link <username>`.",
                ephemeral=True,
            )
            return
    elif username is not None:
        mc_username = username
    else:
        mc_username = await _linked_username_or_reply(interaction)
        if mc_username is None:
            return

    target = await _resolve_target(interaction, mc_username)
    if target is None:
        return

    resp = await mc.rcon(f"balance {target}")
    if resp in mc._RCON_ERROR_STRINGS:
        await interaction.followup.send(resp, ephemeral=True)
        return

    m = _BALANCE_RE.search(mc.strip_colors(resp))
    if m is None:
        # Essentials answers an unknown name with "Error: Player not found."
        await interaction.followup.send(
            f"No balance for `{target}` — have they joined the server?", ephemeral=True
        )
        return

    await interaction.followup.send(f"\U0001f4b0 **{m.group(1)}** — {_fmt_money(m.group(2))}")


@client.tree.command(name="balancetop", description="The richest players on the server")
async def balancetop_cmd(interaction: discord.Interaction):
    await interaction.response.defer()

    # Essentials builds this list asynchronously and answers the call that
    # triggered the rebuild with an EMPTY body, so a cold first request looks
    # like an empty server. One retry is enough to pick up the built cache.
    for attempt in range(2):
        resp = await mc.rcon("balancetop")
        if resp in mc._RCON_ERROR_STRINGS:
            await interaction.followup.send(resp, ephemeral=True)
            return
        clean = mc.strip_colors(resp)
        rows = [m.groups() for m in (_BALTOP_ROW_RE.match(l) for l in clean.splitlines()) if m]
        if rows or attempt:
            break
        await asyncio.sleep(1.5)

    if not rows:
        await interaction.followup.send("Couldn't read the balance list.", ephemeral=True)
        return

    body = "\n".join(f"**{rank}.** {name} — {_fmt_money(amt)}" for rank, name, amt in rows[:20])
    total = re.search(r"Server Total:\s*\$([\d,]+(?:\.\d+)?)", clean)
    if total:
        body += f"\n\n-# Server total: {_fmt_money(total.group(1))}"

    embed = discord.Embed(title="\U0001f4b0 Richest players", description=body)
    await interaction.followup.send(embed=embed)


# --- /commands, /mccommands -------------------------------------------------
# Static reference, not introspected — Discord slash commands and Minecraft
# in-game/console commands live in entirely separate systems (discord.py's
# command tree vs Bukkit's command map inside the Skript/Essentials plugins),
# so there's no single source to query both from. Keep this in sync by hand
# when commands are added/removed/permission changes on either side. The
# Minecraft section has TWO more hand-kept copies: mccommands.sk deployed on
# CT 101 (what /mccommands actually prints in game) and data/mccommands.sk
# here. An auto-push of this text over RCON used to live here; it was removed
# 2026-08-20 because the console commands it called (setmccommandsmc /
# clearmccommandsmc) were never implemented in any deployed script, so it had
# only ever sent unknown-command no-ops. Edit all three copies together.
_DISCORD_COMMANDS = """**Discord**
-# Everyone, unless marked otherwise.

__Minecraft server__
`/mcstatus` · `/tps` · `/start` · `/shutdown`
`/megaphone <message>` — needs the **Minecraft** role

__Map and locations__
`/map [player] [x] [z]` · `/find [player] [name]` · `/locations [player]`
`/setlocation <name> [color]` · `/removelocation <name>` — `/link` first

__Your account__
`/link <username>` · `/unlink`
`/joinmessage <text>` · `/removejoinmessage` — `/link` first

__Money and bounties__
`/balance [player]` · `/balancetop` — needs the server awake
`/bounty <player> <amount>` · `/bounties [player]` — `/link` first

__Rocket League__
`/rlstats <platform> <player>`

__Help__
`/commands` · `/mccommands` · `/economy` · `/miku`

__Feature requests__
`/request <feature>` · `/requests [include_done]`
`/requestdone <id> [note]` — needs **Manage Messages**

__Housekeeping__
`/ping` · `/clear [amount]` — `/clear` needs **Manage Messages**"""

_MINECRAFT_COMMANDS = """**Minecraft (in-game chat)**
-# Everyone, unless marked otherwise.

__Money__
`/balance` (`/bal`) · `/balancetop` · `/pay <player> <amount>`
`/moneyhelp` (`/earn`) how it all works · `/daily` once-a-day payout
Shop signs (ChestShop) — no command; format below

__Gambling and racing__
`/casino` · `/bet <racer> <amount>` · `/lotto [buy <n>]`
`/race` · `/start` · `/raceleave` — the ice boat race
`/bounty [player] [amount]` — a price on their head, paid to whoever kills them

__Land claims__
`/claim` — golden shovel: click one corner, then the opposite
`/trust <player>` · `/untrust <player>` — let someone build in your claim
`/containertrust` · `/accesstrust` — chests only / doors and buttons only
`/claimslist` · `/abandonclaim` · `/buyclaimblocks <n>` — $0.25 a block
A stick shows who owns the land you're looking at

__Locations, pins and the map__
`/setlocation <name> [color]` · `/removelocation <name>` · `/locations [player]`
`/find [player] [name]` — where someone is, or one of their spots
`/pin <name|player> [permanent]` · `/unpin <name|player>` — sidebar marker
`/track <player> [permanent]` · `/untrack <player>` — the player-only `/pin`
`/hidepins` — takes pins off the sidebar; they stay on Tab
`/sethome` (Essentials) — also pins home on your HUD

__Help and odds and ends__
`/mccommands` · `/shophelp` · `/miku` — oo ee oo
Sneak + right-click glass — cycles color, then tinted, then plain

__Op only__
`/casinoadmin add|take|set <amount>` — the house bankroll
`/bountyadmin clear <player>|clearall` — refunds the placers
`/eco give|take|set <player> <amount>`
`/op` · `/deop` · `/lp ...` (LuckPerms) — op / console
WorldEdit (`//wand`, `//set`, etc.) — op / builder, not itemized here"""

# Values match this server's ChestShop config: REVERSE_BUTTONS false (so
# right-click buys, left-click sells), ALLOW_AUTO_ITEM_FILL true (line 4
# accepts "?"), BLOCK_SHOPS_WITH_SELL_PRICE_HIGHER_THAN_BUY_PRICE true,
# SHOP_CREATION_PRICE 0, USE_BUILT_IN_PROTECTION true, and SHOP_CONTAINERS
# limited to CHEST/TRAPPED_CHEST. Re-check config.yml before editing this.
_SHOP_SIGN = """**Shop sign format**
Put a chest (or trapped chest) down, fill it with what you're selling, then place a sign on it:
```
line 1:  (leave blank)
line 2:  64
line 3:  B 100 : S 50
line 4:  diamond
```
`line 1` auto-fills with your name. `line 2` is how many items each trade moves. \
`line 4` is the item — put `?` to auto-fill it from whatever is in the chest.
On `line 3`, `B` is what a customer pays to **buy** from you and `S` is what you pay to \
**buy from them**; use one or both. `S` cannot be higher than `B`.
Right-click the sign to buy, left-click to sell. Shops are free to make, and the chest is \
protected as soon as the sign goes up."""

# Mirrors in-game /moneyhelp (income.sk on CT 101), which is the real
# source of these numbers — it reads most of them straight from the options
# the payout code uses. Kept as static text rather than fetched over RCON so
# it still answers while the server is asleep under lazymc. If you retune
# anything in income.sk, update this too.
_ECONOMY = """**How money works in game**
One currency, shared by shop signs, `/pay` and the casino. Check yours with \
`/balance`, the rich list with `/balancetop`.

**Earning**
`/daily` — $50, once every 24 hours
Kill hostile mobs — $5 common, $12 tough, up to $250 a boss
Mine ore — $1-$15 a block, best on emerald and ancient debris
Win a race — $200 from the house (`/race` in game)
Kill a player — 10% of their money, capped at $500

**Losing**
Taking a hit from a mob — $1 a hit, which funds the race prize pot
Taking a hit from a player — $2, straight into their pocket
Fines stop at $0. Nothing in the economy can put you into debt.

**Gambling** — `/casino` in game
Back a racer with `/bet <racer> <amount>` while people queue for a race, or \
buy lottery tickets with `/lotto buy <n>`. The house takes 5%; everything \
else in the pot is other players' stakes, split between whoever backed the \
winner.

The 5% rake and the mob fines go to the house bankroll, and race prizes are \
paid out of it — that money is recycled rather than destroyed. Ore you placed \
yourself pays nothing when you break it again.

Run `/moneyhelp` in game for this same list."""

_COMMAND_REFERENCE = f"""
{_DISCORD_COMMANDS}

{_MINECRAFT_COMMANDS}

{_SHOP_SIGN}

Some Discord commands quietly bridge to Minecraft over RCON as a separate \
console-only command (e.g. `/joinmessage` → `setjoinmessagemc`) — those \
bridge commands aren't directly runnable by anyone and are left off this list.
"""


# Sent as embeds rather than plain messages: the full reference is past
# Discord's 2000-character limit for message content, which would make
# /commands fail to send outright. An embed description allows 4096.
@client.tree.command(name="commands", description="List available commands and where each one works")
async def commands_cmd(interaction: discord.Interaction):
    embed = discord.Embed(description=_COMMAND_REFERENCE)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@client.tree.command(name="economy", description="How the in-game money system works")
async def economy_cmd(interaction: discord.Interaction):
    embed = discord.Embed(description=_ECONOMY)
    await interaction.response.send_message(embed=embed, ephemeral=True)


# Static text rather than an RCON call, for the same reason /economy is:
# it has to answer while the Minecraft server is asleep under lazymc, and
# there is nothing here worth waking a server for. The in-game half
# (miku.sk on CT 101) is the one that broadcasts to chat and plays the
# flute riff; this is only the Discord side saying the same thing. Public,
# not ephemeral — the whole point is that other people see it.
@client.tree.command(name="miku", description="oo ee oo")
async def miku_cmd(interaction: discord.Interaction):
    await interaction.response.send_message("oo ee oo")


# Feature requests. Deliberately public rather than ephemeral: the point is
# that everyone can see what has been asked for (and react to it) instead of
# the same idea arriving five times. /requests is the ephemeral one, since
# re-reading the backlog shouldn't clutter the channel.
REQUEST_MAX_LEN = 500
REQUEST_LIST_LIMIT = 20


@client.tree.command(name="request", description="Ask for a feature to be added")
@app_commands.describe(feature=f"What you'd like added or changed (max {REQUEST_MAX_LEN} chars)")
async def request_cmd(interaction: discord.Interaction, feature: str):
    await interaction.response.defer()

    text = " ".join(feature.split())
    if not text:
        await interaction.followup.send("Say what you'd like added.", ephemeral=True)
        return
    if len(text) > REQUEST_MAX_LEN:
        await interaction.followup.send(
            f"That's too long ({len(text)}/{REQUEST_MAX_LEN} chars) — trim it and try again.",
            ephemeral=True,
        )
        return

    requests = storage.load_requests()
    items = requests.get("items", [])
    req_id = requests.get("next_id", 1)

    items.append({
        "id": req_id,
        "user_id": str(interaction.user.id),
        "user_name": interaction.user.display_name,
        "text": text,
        "created": int(datetime.now(timezone.utc).timestamp()),
        "done": False,
        "closed_by": None,
        "note": None,
    })
    storage.save_requests({"next_id": req_id + 1, "items": items})
    log.info("feature request #%s from %s: %s", req_id, interaction.user, text)

    await interaction.followup.send(
        f"📝 **Request #{req_id}** from {interaction.user.mention}\n{text}\n"
        f"-# `/requests` to see everything asked for so far."
    )


@client.tree.command(name="requests", description="List the features people have asked for")
@app_commands.describe(include_done="Also show requests already marked done (default false)")
async def requests_cmd(interaction: discord.Interaction, include_done: bool = False):
    requests = storage.load_requests()
    items = requests.get("items", [])
    if not include_done:
        items = [i for i in items if not i.get("done")]

    if not items:
        await interaction.response.send_message(
            "Nothing requested yet — `/request <feature>` starts the list.", ephemeral=True
        )
        return

    # Newest first, capped: an embed description tops out at 4096 characters
    # and a long backlog would otherwise fail to send at all.
    shown = list(reversed(items))[:REQUEST_LIST_LIMIT]
    lines = []
    for i in shown:
        mark = "✅" if i.get("done") else "•"
        line = f"{mark} **#{i['id']}** {i['text']}\n-# {i.get('user_name', 'someone')}"
        if i.get("created"):
            line += f" · <t:{i['created']}:R>"
        if i.get("done") and i.get("note"):
            line += f" · done: {i['note']}"
        lines.append(line)

    body = "\n".join(lines)
    hidden = len(items) - len(shown)
    if hidden > 0:
        body += f"\n-# ...and {hidden} older one(s) not shown."

    embed = discord.Embed(title="Feature requests", description=body)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@client.tree.command(name="requestdone", description="Mark a feature request as done")
@app_commands.describe(id="The request number, as shown by /requests", note="Optional note about what shipped")
@app_commands.default_permissions(manage_messages=True)
async def requestdone_cmd(interaction: discord.Interaction, id: int, note: str = None):
    requests = storage.load_requests()
    items = requests.get("items", [])

    target = next((i for i in items if i.get("id") == id), None)
    if target is None:
        await interaction.response.send_message(f"No request #{id}.", ephemeral=True)
        return
    if target.get("done"):
        await interaction.response.send_message(f"#{id} is already marked done.", ephemeral=True)
        return

    target["done"] = True
    target["closed_by"] = interaction.user.display_name
    target["note"] = " ".join(note.split()) if note else None
    storage.save_requests({"next_id": requests.get("next_id", len(items) + 1), "items": items})

    tail = f" — {target['note']}" if target["note"] else ""
    await interaction.response.send_message(f"✅ Marked #{id} done{tail}.")


@client.tree.command(name="mccommands", description="List only the Minecraft in-game commands")
async def mccommands_cmd(interaction: discord.Interaction):
    embed = discord.Embed(description=f"{_MINECRAFT_COMMANDS}\n\n{_SHOP_SIGN}")
    await interaction.response.send_message(embed=embed, ephemeral=True)


if __name__ == "__main__":
    client.run(TOKEN, log_handler=None)
