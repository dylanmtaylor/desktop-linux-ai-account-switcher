# Fake Claude Code + Codex logins and cached usage so the menu has something to draw.
import base64, json, os, subprocess, time
H = os.environ["HOME"]
def jwt(c):
    e = lambda o: base64.urlsafe_b64encode(json.dumps(o).encode()).decode().rstrip("=")
    return f"{e({'alg':'none'})}.{e(c)}.s"
def claude(email, uuid):
    os.makedirs(f"{H}/.claude", exist_ok=True)
    json.dump({"claudeAiOauth": {"accessToken": "t-"+email, "expiresAt": 9999999999999, "subscriptionType": "max", "accountUuid": uuid}}, open(f"{H}/.claude/.credentials.json", "w"))
    json.dump({"oauthAccount": {"accountUuid": uuid, "emailAddress": email, "planDisplayName": "Max"}}, open(f"{H}/.claude.json", "w"))
def codex(email):
    os.makedirs(f"{H}/.codex", exist_ok=True)
    json.dump({"tokens": {"id_token": jwt({"email": email, "https://api.openai.com/auth": {"chatgpt_plan_type": "plus"}}), "access_token": jwt({"exp": 9999999999}), "refresh_token": "r", "account_id": "a-"+email}}, open(f"{H}/.codex/auth.json", "w"))
cli = ["ai-account-switcher"]
os.environ["PATH"] = f"{H}/.local/bin:" + os.environ["PATH"]
if not os.path.exists(f"{H}/.config/ai-account-switcher/accounts.json"):
    subprocess.run(cli + ["set", "secret_backend", "file"], capture_output=True)
    claude("work@example.com", "u1"); subprocess.run(cli + ["refresh"], capture_output=True)
    claude("personal@example.com", "u2"); subprocess.run(cli + ["refresh"], capture_output=True)
    codex("me@example.com"); subprocess.run(cli + ["refresh"], capture_output=True)
p = f"{H}/.cache/ai-account-switcher/state.json"
s = json.load(open(p)); now = time.time()
W = lambda l, pct, sec: {"label": l, "percent": pct, "resets_at": now + sec}
s["usage"]["claude:personal@example.com"] = {"available": True, "fetched_at": now, "windows": [W("5h", 42, 7800), W("7d", 18, 400000)]}
s["usage"]["claude:work@example.com"] = {"available": True, "fetched_at": now, "windows": [W("5h", 96, 2400), W("7d", 71, 200000)]}
s["usage"]["codex:me@example.com"] = {"available": True, "fetched_at": now, "windows": [W("5h", 12, 9000), W("7d", 3, 500000)]}
s["last_refresh"] = now
json.dump(s, open(p, "w"))
print(subprocess.run(cli + ["status"], capture_output=True, text=True).stdout)
