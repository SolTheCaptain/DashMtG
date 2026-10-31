# Временная проба EchoMTG: история цен FRA+FRC
import json, os, time, urllib.request, urllib.error

TOKEN = os.environ.get("ECHOMTG_TOKEN", "").strip()
BASE = "https://api.echomtg.com/api/"

def get(path):
    req = urllib.request.Request(BASE + path, headers={
        "Authorization": "Bearer " + TOKEN, "User-Agent": "Mozilla/5.0"})
    for _ in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:
            err = repr(e); time.sleep(2)
    return {"error": err}

out = []
for code in ("FRA", "FRC"):
    items = get("data/set/?set_code=" + code).get("set", {}).get("items", [])
    for it in items:
        h = get("data/item_history/?emid=%s" % it["emid"]).get("data") or {}
        out.append({"set": code, "emid": it["emid"], "name": it["name"],
                    "rarity": it["rarity"], "date": h.get("date"),
                    "regular": h.get("regular"), "foil": h.get("foil")})
        time.sleep(0.15)

with open("probe/result.json", "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False)
# КОНЕЦ ФАЙЛА
