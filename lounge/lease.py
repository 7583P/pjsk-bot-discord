"""Database-backed lease fences accidental duplicate bot processes with safe transient recovery.

Preserves strict single-writer exclusivity while eliminating unnecessary bot restarts
caused by transient network timeouts, CPU throttling, or connection drops.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid

import asyncpg

log = logging.getLogger("lease")

LEASE_TTL_SECONDS = 45.0
HEARTBEAT_INTERVAL_SECONDS = 10.0
FAST_RETRY_INTERVAL_SECONDS = 2.0
MAX_UNREACHABLE_GRACE_SECONDS = 60.0


class LeaseStolenError(RuntimeError):
    """Raised when another bot process is confirmed to have acquired the lease."""
    pass


class LeaseUnreachableError(RuntimeError):
    """Raised when the database has been continuously unreachable past the grace period."""
    pass


class RuntimeLease:
    def __init__(self, pool: asyncpg.Pool, owner_id: uuid.UUID | None = None, lease_id: int = 1):
        self.pool = pool
        self.owner = owner_id or uuid.uuid4()
        self.lease_id = lease_id
        self.is_held = False
        self.last_renewed: float = 0.0
        self.lease_deadline: float = 0.0
        self._on_fenced = None
        self._on_recovered = None

    def set_fencing_callbacks(self, on_fenced=None, on_recovered=None) -> None:
        """Register callbacks invoked when lease exclusivity is temporarily unconfirmed or restored."""
        self._on_fenced = on_fenced
        self._on_recovered = on_recovered

    @property
    def remaining_seconds(self) -> float:
        """Seconds remaining on the locally confirmed lease deadline."""
        return max(0.0, self.lease_deadline - time.monotonic())

    def is_valid(self) -> bool:
        """Returns True if the lease is currently confirmed and within its valid time window."""
        return self.is_held and (time.monotonic() < self.lease_deadline)

    async def claim(self) -> None:
        """Initial claim on bot startup.

        Inserts the lease row if empty, or claims if expired or owned by the same owner.
        Raises RuntimeError if another live bot process holds the lease.
        """
        async with self.pool.acquire(timeout=10) as c:
            row = await c.fetchrow(
                """
                INSERT INTO lounge_runtime_lease(id, owner, expires_at)
                VALUES ($1, $2, now() + interval '45 seconds')
                ON CONFLICT (id) DO UPDATE
                SET owner = excluded.owner,
                    expires_at = excluded.expires_at
                WHERE lounge_runtime_lease.expires_at < clock_timestamp()
                   OR lounge_runtime_lease.owner = excluded.owner
                RETURNING owner::text, expires_at;
                """,
                self.lease_id,
                self.owner,
            )
            if not row or row["owner"] != str(self.owner):
                current = await c.fetchrow(
                    "SELECT owner::text, expires_at, expires_at > clock_timestamp() as live FROM lounge_runtime_lease WHERE id=$1",
                    self.lease_id,
                )
                competitor = current["owner"] if current else "unknown"
                raise RuntimeError(
                    f"Another bot process has a live database lease (owner: {competitor})"
                )

        now = time.monotonic()
        self.last_renewed = now
        self.lease_deadline = now + LEASE_TTL_SECONDS
        self.is_held = True
        log.info("Runtime lease acquired successfully (owner=%s, ttl=%.0fs)", self.owner, LEASE_TTL_SECONDS)

    async def renew(self) -> bool:
        """Attempt to renew the lease in PostgreSQL.

        Returns True if renewed successfully.
        Raises LeaseStolenError if another process is authoritatively confirmed as the owner.
        Raises OSError / asyncpg exceptions if a transient communication failure occurs.
        """
        query = """
        WITH current_lease AS (
            SELECT owner, expires_at, expires_at > clock_timestamp() AS live
            FROM lounge_runtime_lease
            WHERE id = $1
        ),
        attempt_update AS (
            UPDATE lounge_runtime_lease
            SET expires_at = now() + interval '45 seconds'
            WHERE id = $1 AND owner = $2
            RETURNING owner, expires_at, true AS renewed
        )
        SELECT
            COALESCE(u.owner, c.owner)::text AS current_owner,
            COALESCE(u.expires_at, c.expires_at) AS expires_at,
            COALESCE(u.renewed, false) AS renewed,
            c.live AS was_live,
            c.owner = $2 AS is_our_owner
        FROM current_lease c
        FULL OUTER JOIN attempt_update u ON true;
        """
        async with self.pool.acquire(timeout=8) as c:
            row = await c.fetchrow(query, self.lease_id, self.owner)

        if not row:
            # Table is empty (re-claim)
            await self.claim()
            return True

        if row["renewed"]:
            now = time.monotonic()
            self.last_renewed = now
            self.lease_deadline = now + LEASE_TTL_SECONDS
            self.is_held = True
            return True

        current_owner = row["current_owner"]
        if current_owner and current_owner != str(self.owner):
            self.is_held = False
            raise LeaseStolenError(
                f"Database lease was claimed by another bot process (competitor owner: {current_owner})"
            )

        # Re-claim if owned by us but not renewed
        await self.claim()
        return True

    async def heartbeat(self) -> None:
        """Single heartbeat call (compatible with existing caller)."""
        await self.renew()

    async def run_loop(self) -> None:
        """Maintains the lease with intelligent transient recovery."""
        deadline_breached_at: float | None = None

        while True:
            try:
                now = time.monotonic()
                time_to_deadline = self.lease_deadline - now
                sleep_dur = FAST_RETRY_INTERVAL_SECONDS if time_to_deadline < 15.0 else HEARTBEAT_INTERVAL_SECONDS
                await asyncio.sleep(sleep_dur)

                try:
                    await self.renew()
                    if deadline_breached_at is not None:
                        log.info("Runtime lease recovered authoritatively! Resuming normal operations.")
                        deadline_breached_at = None
                        if self._on_recovered:
                            try:
                                self._on_recovered()
                            except Exception:
                                log.exception("Error in on_recovered callback")

                except LeaseStolenError:
                    self.is_held = False
                    if self._on_fenced:
                        try:
                            self._on_fenced()
                        except Exception:
                            pass
                    raise

                except (OSError, asyncio.TimeoutError, asyncpg.PostgresError) as exc:
                    now = time.monotonic()
                    remaining = self.lease_deadline - now
                    if remaining > 0:
                        log.warning(
                            "Transient lease renewal error: %s (%s). %.1fs remaining on deadline. Retrying in %.0fs...",
                            type(exc).__name__,
                            exc,
                            remaining,
                            FAST_RETRY_INTERVAL_SECONDS,
                        )
                    else:
                        if deadline_breached_at is None:
                            deadline_breached_at = now
                            self.is_held = False
                            log.error(
                                "Lease deadline elapsed without renewal! Fencing competitive writes while attempting DB recovery..."
                            )
                            if self._on_fenced:
                                try:
                                    self._on_fenced()
                                except Exception:
                                    pass

                        unreachable_duration = now - deadline_breached_at
                        if unreachable_duration > MAX_UNREACHABLE_GRACE_SECONDS:
                            raise LeaseUnreachableError(
                                f"Database continuously unreachable for {unreachable_duration:.0f}s past deadline ({exc})"
                            )
                        log.warning(
                            "Overdue lease renewal attempt failed (%s: %s). Overdue by %.1fs. Retrying in %.0fs...",
                            type(exc).__name__,
                            exc,
                            unreachable_duration,
                            FAST_RETRY_INTERVAL_SECONDS,
                        )

            except asyncio.CancelledError:
                raise

    async def release(self) -> None:
        """Release the lease on graceful shutdown."""
        if not self.is_held:
            return
        try:
            async with self.pool.acquire(timeout=5) as c:
                await c.execute(
                    "DELETE FROM lounge_runtime_lease WHERE id=$1 AND owner=$2", self.lease_id, self.owner
                )
            self.is_held = False
            log.info("Runtime lease released gracefully (owner=%s)", self.owner)
        except Exception as e:
            log.warning("Could not cleanly release lease on shutdown: %s", e)
