import json, os, datetime
path = os.path.expanduser("~/.claude/.credentials.json")
d = json.load(open(path))
oauth = d["claudeAiOauth"]
expires = oauth.get("expiresAt")
print("expiresAt:", expires)
if expires:
    exp = datetime.datetime.fromtimestamp(expires / 1000)
    now = datetime.datetime.now()
    print("expires:", exp)
    print("now:", now)
    print("expired:", exp < now)
