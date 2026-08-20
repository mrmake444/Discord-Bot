"""Tracker.gg Rocket League profile lookups, used by /rlstats."""

import os
import discord
import aiohttp

TRN_API_KEY = os.getenv("TRN_API_KEY", "046ef1ba-317b-4f63-9642-e58570b9a7d0")
TRN_BASE = "https://public-api.tracker.gg/v2/rocket-league/standard/profile"

# Tracker returns a segment per playlist. These are the ones worth showing;
# add or remove freely.
PLAYLISTS = [
    "Ranked Duel 1v1",
    "Ranked Doubles 2v2",
    "Ranked Standard 3v3",
    "Tournament Matches",
]


class TrackerError(Exception):
    """Anything that went wrong talking to Tracker.gg."""


async def fetch_profile(session: aiohttp.ClientSession, platform: str, name: str) -> dict:
    """Pull a player profile from Tracker.gg. `session` must already carry
    the TRN-Api-Key header (set on RLBot's session in bot.py)."""
    url = f"{TRN_BASE}/{platform}/{name}"

    async with session.get(url) as resp:
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
