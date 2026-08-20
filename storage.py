"""Generic JSON file persistence, plus the three typed stores bot.py uses.

All three used to be hand-duplicated load/save pairs in bot.py — same
read-with-fallback, write-via-tempfile-then-replace pattern, three times.
"""

import json
from pathlib import Path

LINKS_PATH = Path("/data/links.json")
LOCATIONS_PATH = Path("/data/locations.json")
JOINMESSAGES_PATH = Path("/data/joinmessages.json")


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def load_links() -> dict:
    return load_json(LINKS_PATH)


def save_links(links: dict) -> None:
    save_json(LINKS_PATH, links)


def load_locations() -> dict:
    """{ discord_id: { location_name: {display_name, mc_username, x, y, z, color} } }"""
    return load_json(LOCATIONS_PATH)


def save_locations(locations: dict) -> None:
    save_json(LOCATIONS_PATH, locations)


def load_joinmessages() -> dict:
    """{ discord_id: { mc_username, message } }"""
    return load_json(JOINMESSAGES_PATH)


def save_joinmessages(messages: dict) -> None:
    save_json(JOINMESSAGES_PATH, messages)
