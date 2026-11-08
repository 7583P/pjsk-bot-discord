import time
import json
import httpx

with open("C:/Users/Usuario/.railway/config.json") as f:
    token = json.load(f)["user"]["token"]

headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
deployment_id = "99d14ecb-2329-49bb-aab7-311277eaf4e1"

q_deploy = """
query GetDeployments($serviceId: String!, $environmentId: String!) {
  deployments(input: {serviceId: $serviceId, environmentId: $environmentId}, first: 3) {
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

q_logs = """
query GetLogs($deploymentId: String!) {
  deploymentLogs(deploymentId: $deploymentId, limit: 60) {
    message
    timestamp
  }
}
"""

for i in range(16):
    time.sleep(5)
    try:
        r = httpx.post(
            "https://backboard.railway.com/graphql/v2",
            json={"query": q_deploy, "variables": {
                "serviceId": "45267784-6c78-41d2-9363-0ec4f4791fa2",
                "environmentId": "2306315c-5efc-46fe-ba37-f52eabc4b7ce"
            }},
            headers=headers,
            timeout=10.0
        )
        edges = r.json().get("data", {}).get("deployments", {}).get("edges", [])
        status = "UNKNOWN"
        for e in edges:
            if e["node"]["id"] == deployment_id:
                status = e["node"]["status"]
                break
        print(f"[{i*5}s] Deployment status: {status}", flush=True)
        if status in ("SUCCESS", "FAILED", "CRASHED"):
            break
    except Exception as e:
        print(f"[{i*5}s] Query error: {e}", flush=True)

# Fetch logs
try:
    r_logs = httpx.post(
        "https://backboard.railway.com/graphql/v2",
        json={"query": q_logs, "variables": {"deploymentId": deployment_id}},
        headers=headers,
        timeout=10.0
    )
    logs = r_logs.json().get("data", {}).get("deploymentLogs", [])
    print(f"Logs ({len(logs)} lines):", flush=True)
    for l in logs[-30:]:
        msg = str(l.get('message', ''))
        print(f"  {l.get('timestamp')}: {msg.encode('ascii', errors='replace').decode()}", flush=True)
except Exception as e:
    print(f"Logs query error: {e}", flush=True)
