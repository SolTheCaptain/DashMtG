# Временная проба EchoMTG: жив ли токен
import json, os, urllib.request, urllib.error

TOKEN = os.environ.get("ECHOMTG_TOKEN", "").strip()
def run(url, auth):
  h = {"User-Agent": "Mozilla/5.0"}
  if auth: h["Authorization"] = "Bearer " + TOKEN
  req = urllib.request.Request(url, headers=h)
  try:
    with urllib.request.urlopen(req, timeout=60) as r:
        code, body = r.status, r.read().decode("utf-8", "replace")
  except urllib.error.HTTPError as e:
    code, body = e.code, e.read().decode("utf-8", "replace")
  return {"status": code, "head": body[:300].replace(TOKEN, "***") if TOKEN else body[:300]}

out = {}
for name, url in (("set", "https://api.echomtg.com/api/data/set/?set_code=FRC"),
                  ("hist", "https://api.echomtg.com/api/data/item_history/?emid=240295")):
    out[name + "_token"] = run(url, True)
    out[name + "_none"] = run(url, False)
    out[name + "_bad"] = (lambda t: (globals().__setitem__("TOKEN", "0"*40), run(url, True), globals().__setitem__("TOKEN", t))[1])(TOKEN)
with open("probe/result.json", "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False)
# КОНЕЦ ФАЙЛА
