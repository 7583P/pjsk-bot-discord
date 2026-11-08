import json
import httpx

with open("C:/Users/Usuario/.railway/config.json") as f:
    token = json.load(f)["user"]["token"]

headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

q = """
query GetVariables($projectId: String!, $environmentId: String!, $serviceId: String!) {
  variables(projectId: $projectId, environmentId: $environmentId, serviceId: $serviceId)
}
"""

r = httpx.post(
    "https://backboard.railway.com/graphql/v2",
    json={"query": q, "variables": {
        "projectId": "f8993ba3-8ccf-4886-b73e-15df02dda477",
        "environmentId": "2306315c-5efc-46fe-ba37-f52eabc4b7ce",
        "serviceId": "45267784-6c78-41d2-9363-0ec4f4791fa2"
    }},
    headers=headers,
    timeout=10.0
)
print("Variables:")
vars_dict = r.json().get("data", {}).get("variables", {})
for k, v in vars_dict.items():
    if "PASS" in k or "SECRET" in k or "TOKEN" in k:
        print(f"  {k} = [REDACTED]")
    else:
        print(f"  {k} = {v}")

