import json
import httpx
from pathlib import Path

# 1. Read DISCORD_TOKEN from private file
cred_path = Path(r"C:\Users\Usuario\.pjsk_secure_backups\lounge_credentials.env")
discord_token = None

with open(cred_path, "r", encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if line.startswith("DISCORD_TOKEN="):
            discord_token = line.split("=", 1)[1].strip().strip('"').strip("'")
            break

if not discord_token or discord_token.startswith("DISABLED"):
    print("ERROR: DISCORD_TOKEN not found or disabled in lounge_credentials.env")
    exit(1)

print(f"[OK] DISCORD_TOKEN read safely from private storage. Length: {len(discord_token)}")

# 2. Configure safely in Railway worker variables
railway_config = Path(r"C:\Users\Usuario\.railway\config.json")
with open(railway_config, "r", encoding="utf-8") as f:
    rw_cfg = json.load(f)

token = rw_cfg.get("user", {}).get("token")
headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

mutation = """
mutation UpsertVariable($input: VariableUpsertInput!) {
  variableUpsert(input: $input)
}
"""

r = httpx.post(
    "https://backboard.railway.com/graphql/v2",
    json={
        "query": mutation,
        "variables": {
            "input": {
                "projectId": "f8993ba3-8ccf-4886-b73e-15df02dda477",
                "environmentId": "2306315c-5efc-46fe-ba37-f52eabc4b7ce",
                "serviceId": "45267784-6c78-41d2-9363-0ec4f4791fa2",
                "name": "DISCORD_TOKEN",
                "value": discord_token
            }
        }
    },
    headers=headers,
    timeout=10.0
)

print(f"[OK] Railway variableUpsert result: {r.status_code}, data: {r.text}")

