import json
import httpx

with open("C:/Users/Usuario/.railway/config.json") as f:
    token = json.load(f)["user"]["token"]

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
                "name": "DB_CA_FILE",
                "value": "/app/certs/supabase_ca_bundle.crt"
            }
        }
    },
    headers=headers,
    timeout=10.0
)

print("Upsert DB_CA_FILE:", r.status_code, r.text)

