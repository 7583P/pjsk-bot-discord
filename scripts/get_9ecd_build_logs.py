import json
import httpx

with open("C:/Users/Usuario/.railway/config.json") as f:
    token = json.load(f)["user"]["token"]

headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
deployment_id = "9ecd53f5-50aa-4e2b-b21d-8a2e7e29969c"

q_build = """
query GetBuildLogs($deploymentId: String!) {
  buildLogs(deploymentId: $deploymentId, limit: 100) {
    message
    timestamp
  }
}
"""

r = httpx.post(
    "https://backboard.railway.com/graphql/v2",
    json={"query": q_build, "variables": {"deploymentId": deployment_id}},
    headers=headers,
    timeout=10.0
)
logs = r.json().get("data", {}).get("buildLogs", [])
print(f"Build logs ({len(logs)} lines):")
for l in logs[-20:]:
    print(f"  {l.get('message')}")

