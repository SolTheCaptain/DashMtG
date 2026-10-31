# Временная проба EchoMTG: смотрим, что отдаёт API по FRA/FRC
import json, os, urllib.request, urllib.error

TOKEN = os.environ.get("ECHOMTG_TOKEN", "").strip()
BASE = "https://api.echomtg.com/api/"
URLS = [
    "data/set/?set_code=FRA",
    "data/set/?set_code=FRC",
    "data/set/?set_code=fra&minified=false",
    "data/set_list/",
    "data/sets/",
    "data/trending/",
    "data/movers/",
    "data/winners/",
]

def get(path):
    req = urllib.request.Request(BASE + path, headers={
        "Authorization": "Bearer " + TOKEN, "User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, repr(e)

out = {}
for p in URLS:
    code, body = get(p)
    body = body.replace(TOKEN, "***") if TOKEN else body
    rec = {"status": code, "len": len(body)}
    try:
        j = json.loads(body)
        rec["keys"] = list(j.keys())[:40] if isinstance(j, dict) else type(j).__name__
        items = None
        if isinstance(j, dict):
            for k in ("items", "data", "set", "cards", "results"):
                v = j.get(k)
                if isinstance(v, dict):
                    v = v.get("items", v.get("cards", v))
                if isinstance(v, list) and v:
                    items = v; break
        elif isinstance(j, list):
            items = j
        if items:
            rec["count"] = len(items)
            rec["item_keys"] = list(items[0].keys()) if isinstance(items[0], dict) else None
            rec["samples"] = items[:3]
        else:
            rec["head"] = body[:3000]
    except Exception:
        rec["head"] = body[:1500]
    out[p] = rec

os.makedirs("probe", exist_ok=True)
with open("probe/result.json", "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1)
# КОНЕЦ ФАЙЛА
