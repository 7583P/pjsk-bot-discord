import json
import urllib.request
from pathlib import Path

railway_config = Path(r"C:\Users\Usuario\.railway\config.json")
if not railway_config.exists():
    print("Railway config not found")
    exit(1)

with open(railway_config, "r", encoding="utf-8") as f:
    cfg = json.load(f)

token = cfg.get("user", {}).get("token")
service_id = "45267784-6c78-41d2-9363-0ec4f4791fa2"

query = """
query GetDeployments($serviceId: String!) {
  deployments(input: {serviceId: $serviceId}, first: 5) {
    edges {
      node {
        id
        status
        createdAt
        staticUrl
      }
    }
  }
}
"""

req = urllib.request.Request(
    "https://backboard.railway.app/graphql/v2",
    data=json.dumps({"query": query, "variables": {"serviceId": service_id}}).encode("utf-8"),
    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
)

try:
    with urllib.request.urlopen(req) as resp:
        res = json.loads(resp.read().decode())
        print(json.dumps(res, indent=2))
except Exception as e:
    print(f"Error: {e}")

