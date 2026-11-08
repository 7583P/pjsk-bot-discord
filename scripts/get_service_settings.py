import json
import httpx

with open("C:/Users/Usuario/.railway/config.json") as f:
    token = json.load(f)["user"]["token"]

headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
service_id = "45267784-6c78-41d2-9363-0ec4f4791fa2"

q = """
query GetService($serviceId: String!) {
  service(id: $serviceId) {
    id
    name
    serviceInstances {
      edges {
        node {
          id
          startCommand
          buildCommand
          rootDirectory
        }
      }
    }
  }
}
"""

r = httpx.post(
    "https://backboard.railway.com/graphql/v2",
    json={"query": q, "variables": {"serviceId": service_id}},
    headers=headers
)
print("Status:", r.status_code)
print(json.dumps(r.json(), indent=2))

