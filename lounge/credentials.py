"""Reject known exposed credentials without storing their plaintext."""

import hashlib
import json
from pathlib import Path


def is_exposed(token: str, hashes=None) -> bool:
    if hashes is None:
        hashes = json.loads(
            Path(__file__)
            .with_name("exposed_token_hashes.json")
            .read_text(encoding="utf-8")
        )
    return bool(token) and hashlib.sha256(token.encode()).hexdigest() in hashes
