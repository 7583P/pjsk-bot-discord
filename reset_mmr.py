"""Compatibility entry point for the guarded Season 5 operation (dry-run by default)."""

import asyncio
import argparse
from dotenv import load_dotenv
from scripts.activate_season_5 import main

if __name__ == "__main__":
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--backup-manifest")
    asyncio.run(main(parser.parse_args()))
