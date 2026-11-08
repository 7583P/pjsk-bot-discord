import json
import httpx

with open("C:/Users/Usuario/.railway/config.json") as f:
    token = json.load(f)["user"]["token"]

headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
deployment_id = "802ae685-3adc-44da-8696-f093f6311942"

q = """
query GetLogs($deploymentId: String!) {
  deploymentLogs(deploymentId: $deploymentId, limit: 100) {
    message
    timestamp
    severity
  }
}
"""

r = httpx.post(
    "https://backboard.railway.com/graphql/v2",
    json={"query": q, "variables": {"deploymentId": deployment_id}},
    headers=headers
)
print("Status:", r.status_code)
try:
    data = r.json()
    logs = data.get("data", {}).get("deploymentLogs", [])
    if not logs:
        print("No logs in deploymentLogs, trying buildLogs...")
    for log in logs[-30:]:
        print(f"[{log.get('timestamp')}] {log.get('message')}")
except Exception as e:
    print("Error:", e, r.text)
