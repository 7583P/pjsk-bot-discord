import os, unittest
URL = os.getenv("TEST_REROLL_DATABASE_URL")

@unittest.skipIf(not URL, "TEST_REROLL_DATABASE_URL not set")
class SchemaVersions(unittest.IsolatedAsyncioTestCase):
    async def test_new_runtime_accepts_007_and_008_but_not_unknown_or_broken(self):
        import asyncpg
        from lounge.schema import validate_schema
        c = await asyncpg.connect(URL)
        tr = c.transaction(); await tr.start()
        try:
            await validate_schema(c)  # 007 + table (current prod shape)
            await c.execute("INSERT INTO lounge_schema_migrations(version,checksum) VALUES('008_room_reroll.sql','x')")
            await validate_schema(c)  # 008 registered
            await c.execute("INSERT INTO lounge_schema_migrations(version,checksum) VALUES('009_future.sql','x')")
            with self.assertRaises(RuntimeError):
                await validate_schema(c)
            await c.execute("DELETE FROM lounge_schema_migrations WHERE version IN('009_future.sql')")
            await c.execute("ALTER TABLE lounge_room_reroll RENAME TO lounge_room_reroll_x")
            with self.assertRaises(RuntimeError):
                await validate_schema(c)
        finally:
            await tr.rollback(); await c.close()

    def test_old_runtime_check_is_exactly_007(self):
        """Documents why 008 is NOT registered yet: legacy code requires version == 007."""
        import subprocess
        old = subprocess.run(["git", "show", "clean/pre-wispbyte-20261108:lounge/schema.py"], capture_output=True, text=True).stdout
        if not old:
            self.skipTest("reference tag not available")
        self.assertIn('version != "007_publication_state.sql"', old)
