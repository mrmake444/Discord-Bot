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

Structure: this file is the Discord-facing half only — the RLBot class and
every /command handler. Everything Minecraft-connectivity-related (RCON,
container start/stop, the chat bridge + location sync) lives in mc.py;
Tracker.gg lookups live in tracker.py; JSON persistence lives in storage.py.
"""

import asyncio
import json
import logging
import os

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
log = logging.getLogger("rlbot")

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


client = RLBot()
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
`/rlstats <platform> <player>` — everyone
`/link <username>` / `/unlink` — everyone
`/find [player] [name]` — everyone
`/setlocation <name> [color]` / `/removelocation <name>` — everyone, requires `/link` first
`/locations [player]` — everyone
`/joinmessage <text>` / `/removejoinmessage` — everyone, requires `/link` first
`/map [player] [x] [z]` — everyone
`/mcstatus` / `/tps` / `/ping` — everyone
`/start` / `/shutdown` — everyone
`/megaphone <message>` — requires the **Minecraft** Discord role
`/clear [amount]` — requires **Manage Messages** permission
`/commands` / `/mccommands` — everyone"""

_MINECRAFT_COMMANDS = """**Minecraft (in-game chat)**
`/setlocation <name> [color]` / `/removelocation <name>` — everyone
`/pin <name\\|player> [permanent]` / `/unpin <name\\|player>` — everyone, takes a saved location or a player
`/track <player> [permanent]` / `/untrack <player>` — everyone, the player-only spelling of `/pin`
`/hidepins` — everyone, toggles pins off the sidebar; they stay on Tab
`/sethome` (Essentials) — everyone, also pins "home" on the HUD automatically
`/find [player] [name]` / `/locations [player]` — everyone
`/shophelp` / `/mccommands` — everyone
`/balance` (`/bal`) / `/balancetop` / `/pay <player> <amount>` — everyone
`/moneyhelp` (`/earn`) — everyone, how money is earned, lost and gambled
`/daily` — everyone, the once-a-day payout
`/casino` / `/bet <racer> <amount>` / `/lotto [buy <n>]` — everyone
`/race` / `/start` / `/raceleave` — everyone, the ice boat race
`/casinoadmin add\\|take\\|set <amount>` — op only, the house bankroll
Shop signs (ChestShop) — everyone, no command — see the shop sign format below
Sneak + right-click glass — cycles its color, then tinted glass, then back to plain (no command)
`/eco give\\|take\\|set <player> <amount>` — op only
`/op` / `/deop` / `/lp ...` (LuckPerms) — op / console only
WorldEdit (`//wand`, `//set`, etc.) — op / builder only, not itemized here"""

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


@client.tree.command(name="mccommands", description="List only the Minecraft in-game commands")
async def mccommands_cmd(interaction: discord.Interaction):
    embed = discord.Embed(description=f"{_MINECRAFT_COMMANDS}\n\n{_SHOP_SIGN}")
    await interaction.response.send_message(embed=embed, ephemeral=True)


if __name__ == "__main__":
    client.run(TOKEN, log_handler=None)
