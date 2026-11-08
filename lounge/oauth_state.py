"""One-use OAuth state survives serverless workers and is bound to its browser."""

import hashlib
import hmac
import secrets

COOKIE_NAME = "__Host-pjsk_oauth_state"


class OAuthStates:
    def __init__(self, pool):
        self.pool = pool

    async def issue(self):
        state = secrets.token_urlsafe(32)
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "DELETE FROM lounge_oauth_states WHERE expires_at<=now()"
            )
            await conn.execute(
                "INSERT INTO lounge_oauth_states(state_hash,expires_at) VALUES($1,now()+interval '10 minutes')",
                hashlib.sha256(state.encode()).hexdigest(),
            )
        return state

    async def consume(self, state, browser_state):
        if (
            not isinstance(state, str)
            or not isinstance(browser_state, str)
            or not 20 <= len(state) <= 200
            or not hmac.compare_digest(state, browser_state)
        ):
            return False
        async with self.pool.acquire() as conn:
            return bool(
                await conn.fetchval(
                    "DELETE FROM lounge_oauth_states WHERE state_hash=$1 AND expires_at>now() RETURNING true",
                    hashlib.sha256(state.encode()).hexdigest(),
                )
            )
