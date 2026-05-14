import json, os, urllib.request, urllib.parse

path = os.path.expanduser("~/.claude/.credentials.json")
d = json.load(open(path))
oauth = d["claudeAiOauth"]

refresh_token = oauth["refreshToken"]

# Claude Code uses Anthropic's OAuth endpoint
data = urllib.parse.urlencode({
    "grant_type": "refresh_token",
    "refresh_token": refresh_token,
    "client_id": "9d1c250a-e61b-44d9-88ed-5944d1962f5e",  # Claude Code client ID
}).encode()

req = urllib.request.Request(
    "https://auth.anthropic.com/oauth/token",
    data=data,
    method="POST",
    headers={"Content-Type": "application/x-www-form-urlencoded"}
)

try:
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read())
        print("SUCCESS, new token type:", result.get("token_type"))
        print("expires_in:", result.get("expires_in"))
        # Update credentials
        oauth["accessToken"] = result["access_token"]
        if "refresh_token" in result:
            oauth["refreshToken"] = result["refresh_token"]
        import time
        oauth["expiresAt"] = int((time.time() + result.get("expires_in", 3600)) * 1000)
        json.dump(d, open(path, "w"), indent=2)
        print("Credentials updated.")
except Exception as e:
    print("FAILED:", e)
