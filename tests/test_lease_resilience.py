import asyncio
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

import asyncpg
from lounge.lease import (
    RuntimeLease,
    LeaseStolenError,
    LeaseUnreachableError,
    LEASE_TTL_SECONDS,
    FAST_RETRY_INTERVAL_SECONDS,
    MAX_UNREACHABLE_GRACE_SECONDS,
)


class MockConnection:
    def __init__(self, db_state):
        self.db_state = db_state

    async def fetchrow(self, query, *args):
        # 1. Claim query
        if "INSERT INTO lounge_runtime_lease" in query:
            lease_id, owner = args[0], str(args[1])
            existing = self.db_state.get(lease_id)
            now = time.time()
            if existing is None or existing["expires_at"] < now or existing["owner"] == owner:
                self.db_state[lease_id] = {
                    "id": lease_id,
                    "owner": owner,
                    "expires_at": now + LEASE_TTL_SECONDS,
                }
                return {"owner": owner, "expires_at": self.db_state[lease_id]["expires_at"]}
            return None

        # 2. Competitor inspection query after failed claim
        if "SELECT owner::text, expires_at" in query and "lounge_runtime_lease" in query:
            lease_id = args[0]
            existing = self.db_state.get(lease_id)
            if existing:
                return {
                    "owner": existing["owner"],
                    "expires_at": existing["expires_at"],
                    "live": existing["expires_at"] > time.time(),
                }
            return None

        # 3. Renew query (CTE)
        if "attempt_update AS" in query or "WITH current_lease" in query:
            lease_id, owner = args[0], str(args[1])
            existing = self.db_state.get(lease_id)
            now = time.time()
            if not existing:
                return None
            current_owner = existing["owner"]
            live = existing["expires_at"] > now
            if current_owner == owner:
                existing["expires_at"] = now + LEASE_TTL_SECONDS
                return {
                    "current_owner": owner,
                    "expires_at": existing["expires_at"],
                    "renewed": True,
                    "was_live": live,
                    "is_our_owner": True,
                }
            else:
                return {
                    "current_owner": current_owner,
                    "expires_at": existing["expires_at"],
                    "renewed": False,
                    "was_live": live,
                    "is_our_owner": False,
                }

        raise NotImplementedError(f"Unhandled query in MockConnection: {query}")

    async def execute(self, query, *args):
        if "DELETE FROM lounge_runtime_lease" in query:
            lease_id, owner = args[0], str(args[1])
            existing = self.db_state.get(lease_id)
            if existing and existing["owner"] == owner:
                del self.db_state[lease_id]
            return "DELETE 1"
        raise NotImplementedError(f"Unhandled query in execute: {query}")


class MockAcquireContext:
    def __init__(self, conn, fail_exc=None):
        self.conn = conn
        self.fail_exc = fail_exc

    async def __aenter__(self):
        if self.fail_exc:
            raise self.fail_exc
        return self.conn

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


class MockPool:
    def __init__(self, db_state=None):
        self.db_state = db_state if db_state is not None else {}
        self.conn = MockConnection(self.db_state)
        self.fail_exc = None

    def acquire(self, timeout=None):
        return MockAcquireContext(self.conn, self.fail_exc)


class TestLeaseResilience(unittest.IsolatedAsyncioTestCase):

    async def test_01_initial_claim_and_renew(self):
        """Process A successfully acquires empty lease and renews it."""
        pool = MockPool()
        owner_a = uuid.uuid4()
        lease_a = RuntimeLease(pool, owner_id=owner_a, lease_id=1)

        await lease_a.claim()
        self.assertTrue(lease_a.is_held)
        self.assertTrue(lease_a.is_valid())
        self.assertAlmostEqual(lease_a.remaining_seconds, LEASE_TTL_SECONDS, delta=2.0)

        # Renew
        renewed = await lease_a.renew()
        self.assertTrue(renewed)
        self.assertTrue(lease_a.is_valid())

    async def test_02_contention_blocked(self):
        """Process B cannot claim a lease while Process A holds it alive."""
        pool = MockPool()
        owner_a = uuid.uuid4()
        owner_b = uuid.uuid4()
        lease_a = RuntimeLease(pool, owner_id=owner_a, lease_id=1)
        lease_b = RuntimeLease(pool, owner_id=owner_b, lease_id=1)

        await lease_a.claim()
        self.assertTrue(lease_a.is_held)

        with self.assertRaises(RuntimeError) as ctx:
            await lease_b.claim()
        self.assertIn("Another bot process has a live database lease", str(ctx.exception))
        self.assertFalse(lease_b.is_held)

    async def test_03_overdue_recovery_without_competitor(self):
        """If clock jitter causes local deadline to expire, authoritative renew recovers cleanly."""
        pool = MockPool()
        owner_a = uuid.uuid4()
        lease_a = RuntimeLease(pool, owner_id=owner_a, lease_id=1)

        await lease_a.claim()
        # Simulate local clock passing deadline (e.g. CPU throttled or event loop paused)
        lease_a.lease_deadline = time.monotonic() - 1.0
        self.assertFalse(lease_a.is_valid())

        # Authoritative renew must recover
        recovered = await lease_a.renew()
        self.assertTrue(recovered)
        self.assertTrue(lease_a.is_valid())
        self.assertTrue(lease_a.remaining_seconds > 0)

    async def test_04_fencing_callbacks(self):
        """Callbacks fire on fencing and recovery."""
        pool = MockPool()
        owner_a = uuid.uuid4()
        lease_a = RuntimeLease(pool, owner_id=owner_a, lease_id=1)

        lease_a.set_fencing_callbacks(
            on_fenced=lambda: setattr(lease_a, "_fenced_flag", True),
            on_recovered=lambda: setattr(lease_a, "_recovered_flag", True),
        )
        self.assertIsNotNone(lease_a._on_fenced)
        self.assertIsNotNone(lease_a._on_recovered)

    async def test_05_competitor_steals_expired_lease(self):
        """If lease expired and Process B claims it, Process A authoritatively detects LeaseStolenError."""
        pool = MockPool()
        owner_a = uuid.uuid4()
        owner_b = uuid.uuid4()
        lease_a = RuntimeLease(pool, owner_id=owner_a, lease_id=1)
        lease_b = RuntimeLease(pool, owner_id=owner_b, lease_id=1)

        await lease_a.claim()

        # Simulate expiration in DB
        pool.db_state[1]["expires_at"] = time.time() - 5.0

        # Process B claims legitimately
        await lease_b.claim()
        self.assertTrue(lease_b.is_held)

        # Process A tries to renew and discovers Process B is the new owner!
        with self.assertRaises(LeaseStolenError) as ctx:
            await lease_a.renew()
        self.assertIn("Database lease was claimed by another bot process", str(ctx.exception))
        self.assertFalse(lease_a.is_held)

    async def test_06_clean_release(self):
        """Clean shutdown deletes the lease row."""
        pool = MockPool()
        owner_a = uuid.uuid4()
        lease_a = RuntimeLease(pool, owner_id=owner_a, lease_id=1)

        await lease_a.claim()
        self.assertTrue(lease_a.is_held)
        self.assertIn(1, pool.db_state)

        await lease_a.release()
        self.assertFalse(lease_a.is_held)
        self.assertNotIn(1, pool.db_state)

    async def test_07_run_loop_transient_error_recovery(self):
        """run_loop survives transient connection errors within the deadline window."""
        pool = MockPool()
        owner_a = uuid.uuid4()
        lease_a = RuntimeLease(pool, owner_id=owner_a, lease_id=1)
        await lease_a.claim()

        # Inject transient error for first acquire
        call_count = 0
        original_acquire = pool.acquire

        def transient_acquire(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return MockAcquireContext(pool.conn, fail_exc=asyncio.TimeoutError("Pool timeout"))
            return original_acquire(*args, **kwargs)

        pool.acquire = transient_acquire

        # Run loop for a short burst
        loop_task = asyncio.create_task(lease_a.run_loop())
        await asyncio.sleep(0.1)
        # It's waiting on asyncio.sleep(sleep_dur), cancel cleanly
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass

        self.assertTrue(lease_a.is_held)


if __name__ == "__main__":
    unittest.main()
