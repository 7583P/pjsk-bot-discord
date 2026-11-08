import json
import httpx

with open("C:/Users/Usuario/.railway/config.json") as f:
    token = json.load(f)["user"]["token"]

headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

# 1. Get latest deployment
q_latest = """
query GetDeployments($serviceId: String!, $environmentId: String!) {
  deployments(input: {serviceId: $serviceId, environmentId: $environmentId}, first: 1) {
    edges {
      node {
        id
        status
        createdAt
      }
    }
  }
}
"""

r = httpx.post(
    "https://backboard.railway.com/graphql/v2",
    json={"query": q_latest, "variables": {
        "serviceId": "45267784-6c78-41d2-9363-0ec4f4791fa2",
        "environmentId": "2306315c-5efc-46fe-ba37-f52eabc4b7ce"
    }},
    headers=headers,
    timeout=10.0
)

latest = r.json()["data"]["deployments"]["edges"][0]["node"]
dep_id = latest["id"]
dep_status = latest["status"]
print(f"Latest deployment: {dep_id} ({dep_status})")

# 2. Get logs for latest deployment
q_logs = """
query GetLogs($deploymentId: String!) {
  deploymentLogs(deploymentId: $deploymentId, limit: 100) {
    message
    timestamp
  }
}
"""

r2 = httpx.post(
    "https://backboard.railway.com/graphql/v2",
    json={"query": q_logs, "variables": {"deploymentId": dep_id}},
    headers=headers,
    timeout=10.0
)

logs = r2.json().get("data", {}).get("deploymentLogs", [])
print(f"=== LOGS FOR DEPLOYMENT {dep_id} ({len(logs)} lines) ===")
for l in logs[-35:]:
    msg = str(l.get('message', ''))
    print(f"[{l.get('timestamp')}] {msg.encode('ascii', errors='replace').decode()}")
