import subprocess
import json

base_url = "https://pjsk-page-d9upju7sb-pmt76ks-projects.vercel.app"
endpoints = [
    "/api/seasons",
    "/api/players?season_id=7",
    "/api/matches?season_id=7",
    "/api/hall-of-fame",
    "/api/season-cutoffs",
    "/auth02",
    "/auth02/me"
]

results = []

for ep in endpoints:
    url = f"{base_url}{ep}"
    cmd = ["npx.cmd", "vercel", "curl", "-i", url]
    try:
        proc = subprocess.run(cmd, cwd=r"d:\Projects\PJSK Bot\web", capture_output=True, text=True, timeout=25)
        stdout = proc.stdout
        # Extract HTTP status
        first_line = ""
        for line in stdout.splitlines():
            if line.startswith("HTTP/"):
                first_line = line
                break
        results.append({
            "endpoint": ep,
            "status_line": first_line,
            "stdout_len": len(stdout),
            "preview": stdout[:200]
        })
        print(f"DONE: {ep} -> {first_line}")
    except Exception as e:
        print(f"ERROR: {ep} -> {e}")
        results.append({"endpoint": ep, "error": str(e)})

with open("vercel_preview_test_results.json", "w", encoding="utf-8") as f:
    json.dump(results, f, indent=2)
print("Saved vercel_preview_test_results.json")

