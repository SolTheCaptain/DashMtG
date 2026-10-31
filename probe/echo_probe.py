# Временная проба EchoMTG: два последних устоявшихся сета + их командеры
import json, os, time, datetime as dt, urllib.request

TOKEN = os.environ.get("ECHOMTG_TOKEN", "").strip()
BASE = "https://api.echomtg.com/api/"

def get(path):
    req = urllib.request.Request(BASE + path, headers={
        "Authorization": "Bearer " + TOKEN, "User-Agent": "Mozilla/5.0"})
    err = None
    for _ in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:
            err = repr(e); time.sleep(2)
    return {"error": err}

sets = get("data/sets/").get("data", [])
slim = [{k: s.get(k) for k in ("name", "set_code", "type", "block_type", "release_date", "date", "is_group", "total_cards")} for s in sets]
out = {"sets": slim, "items": []}
today = dt.date.today()
def rd(s):
    try: return dt.date.fromisoformat((s.get("release_date") or s.get("date") or "")[:10])
    except Exception: return None
names = {c.get("name") for c in sets}
main = [s for s in sets if not s.get("is_group") and s.get("type") == "block"
        and "Tokens" not in s.get("name", "") and "Commander: " + s.get("name", "") in names
        and rd(s) and rd(s) <= today - dt.timedelta(days=10)]
main.sort(key=rd, reverse=True)
picked = []
for s in main[:2]:
    picked.append(s["set_code"])
    for c in sets:
        if c.get("name") == "Commander: " + s["name"]:
            picked.append(c["set_code"])
out["picked"] = picked
for code in picked:
    items = get("data/set/?set_code=" + code).get("set", {}).get("items", [])
    for it in items:
        h = get("data/item_history/?emid=%s" % it["emid"]).get("data") or {}
        out["items"].append({"set": code, "emid": it["emid"], "name": it["name"],
                             "date": h.get("date"), "regular": h.get("regular"), "foil": h.get("foil")})
        time.sleep(0.15)

with open("probe/result.json", "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False)
# КОНЕЦ ФАЙЛА
