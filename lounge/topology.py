"""Explicit staging-only Discord IDs; production defaults remain unchanged."""

import json
import os
from pathlib import Path


def staging_topology():
    path = os.getenv("DISCORD_TOPOLOGY_FILE")
    staging = os.getenv("PJSK_ENV", "production") == "staging"
    if not path:
        if staging:
            raise ValueError("Staging requires a separate Discord topology file")
        return None
    if not staging:
        raise ValueError("Topology overrides are only allowed in staging")
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if int(data["guild_id"]) == 1371306026917298267:
        raise ValueError("Staging must not target the production guild")
    for key, env in [("guild_id", "GUILD_ID"), ("application_id", "APPLICATION_ID")]:
        if int(data[key]) <= 0 or str(data[key]) != os.getenv(env):
            raise ValueError(
                "Topology must match explicit guild/application environment IDs"
            )
    fields = [
        "category_ids",
        "join_channel_ids",
        "results_channel_ids",
        "room_channel_ids",
        "queue_role_ids",
        "ping_role_ids",
    ]
    for key in fields:
        if set(data[key]) != {"en", "jp"}:
            raise ValueError("Both separate EN and JP mappings are required")
        data[key] = {server: int(value) for server, value in data[key].items()}
        if any(v <= 0 for v in data[key].values()) or len(set(data[key].values())) != 2:
            raise ValueError("Distinct positive EN/JP IDs required")
    channels = [
        v
        for k in ["join_channel_ids", "results_channel_ids", "room_channel_ids"]
        for v in data[k].values()
    ]
    if len(set(channels)) != len(channels):
        raise ValueError("Join/results/room channels must be distinct")
    data["rank_role_ids"] = {k: int(v) for k, v in data["rank_role_ids"].items()}
    data["season_placement_role_id"] = int(data["season_placement_role_id"])
    roles = [
        *data["rank_role_ids"].values(),
        data["season_placement_role_id"],
        *data["queue_role_ids"].values(),
        *data["ping_role_ids"].values(),
    ]
    if any(v <= 0 for v in roles) or len(set(roles)) != len(roles):
        raise ValueError("Competitive role IDs must be positive and distinct")
    return data


def apply_staging(namespace, kind):
    data = staging_topology()
    if data is None:
        return
    if kind in ("matchmaking", "players"):
        original = namespace["RANK_ROLE_IDS"]
        if set(original) != set(data["rank_role_ids"]):
            raise ValueError("Staging must preserve every original rank name")
        if set(original.values()) & set(data["rank_role_ids"].values()):
            raise ValueError("Staging cannot reuse production role IDs")
        namespace["RANK_ROLE_IDS"] = data["rank_role_ids"].copy()
        namespace["SEASON_PLACEMENT_ROLE_ID"] = data["season_placement_role_id"]
    if kind == "matchmaking":
        namespace["PLACEMENT_ROLE_ID"] = data["rank_role_ids"]["Placement"]
        for name, key in [
            ("CATEGORY_IDS", "category_ids"),
            ("JOIN_CHANNEL_IDS", "join_channel_ids"),
            ("RESULTS_CHANNEL_IDS", "results_channel_ids"),
            ("QUEUE_ROLE_IDS", "queue_role_ids"),
        ]:
            if set(namespace[name].values()) & set(data[key].values()):
                raise ValueError("Staging cannot reuse production channel/queue IDs")
            namespace[name] = data[key].copy()
        namespace["ALLOWED_JOIN_CHANNELS"] = set(data["join_channel_ids"].values())
        namespace["JOIN_TO_RESULTS"] = {
            data["join_channel_ids"][s]: data["results_channel_ids"][s]
            for s in ["en", "jp"]
        }
        namespace["PING_ROLES_BY_JOIN_CHANNEL"] = {
            data["join_channel_ids"][s]: data["ping_role_ids"][s] for s in ["en", "jp"]
        }
        names = {
            cfg["server"]: cfg["queue_role_name"]
            for cfg in namespace["JOIN_CHANNEL_CFG"].values()
        }
        namespace["JOIN_CHANNEL_CFG"] = {
            data["join_channel_ids"][s]: {
                "server": s,
                "queue_role_id": data["queue_role_ids"][s],
                "queue_role_name": names[s],
            }
            for s in ["en", "jp"]
        }
    elif kind == "rooms":
        namespace["CATEGORY_TO_ROOM_CHANNEL"] = {
            data["category_ids"][s]: data["room_channel_ids"][s] for s in ["en", "jp"]
        }
