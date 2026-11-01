# Временная проба EchoMTG: жив ли токен
import json, os, urllib.request, urllib.error

TOKEN = os.environ.get("ECHOMTG_TOKEN", "").strip()
req = urllib.request.Request("https://api.echomtg.com/api/data/set/?set_code=FRC",
    headers={"Authorization": "Bearer " + TOKEN, "User-Agent": "Mozilla/5.0"})
try:
    with urllib.request.urlopen(req, timeout=60) as r:
        code, body = r.status, r.read().decode("utf-8", "replace")
except urllib.error.HTTPError as e:
    code, body = e.code, e.read().decode("utf-8", "replace")
try:
    j = json.loads(body)
    out = {"status": code, "cards": len((j.get("set") or {}).get("items") or []),
           "message": j.get("message"), "api_status": j.get("status")}
except Exception:
    out = {"status": code, "head": body[:500].replace(TOKEN, "***")}
with open("probe/result.json", "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False)
# КОНЕЦ ФАЙЛА
