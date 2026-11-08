"""JSON must preserve snowflakes and bigint player identities losslessly."""

from fastapi.responses import JSONResponse

IDENTITY_KEYS = {
    "user_id",
    "userId",
    "discord_id",
    "discordId",
    "discordID",
    "discord_user_id",
    "discordUserId",
    "player_id",
    "playerId",
    "channel_id",
    "message_id",
    "thread_id",
    "guild_id",
}


def safe_ids(value):
    if isinstance(value, list):
        return [safe_ids(v) for v in value]
    if not isinstance(value, dict):
        return value
    result = {}
    player = (
        "user_id" in value
        or "rank" in value
        or "discord_id" in value
        or "discordId" in value
    )
    for key, item in value.items():
        if (
            item is not None
            and (
                key in IDENTITY_KEYS
                or (
                    key == "id"
                    and (player or isinstance(item, int) and abs(item) > 2**53 - 1)
                )
            )
            and isinstance(item, (str, int))
        ):
            result[key] = str(item)
        else:
            result[key] = safe_ids(item)
    return result


class SafeJSONResponse(JSONResponse):
    def render(self, content):
        return super().render(safe_ids(content))
