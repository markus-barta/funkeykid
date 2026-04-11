#!/usr/bin/env python3
"""Retry missing TTS for digits whose sound is still empty. Sequential with delay."""
import json, sys, time, urllib.request

def req(method, url, body=None):
    data = json.dumps(body).encode() if body else None
    r = urllib.request.Request(url, data=data, method=method,
        headers={"Content-Type":"application/json"} if body else {})
    with urllib.request.urlopen(r, timeout=120) as resp:
        return json.load(resp) if resp.headers.get("Content-Type","").startswith("application/json") else resp.read()

def slugify(text):
    out = []
    for c in text.lower():
        out.append({"ä":"ae","ö":"oe","ü":"ue","ß":"ss"," ":"-"}.get(c,c))
    return "".join(ch for ch in "".join(out) if ch.isalnum() or ch in "-_")

base = sys.argv[1].rstrip("/") if len(sys.argv)>1 else "http://hsb1.lan:8081"
set_id = sys.argv[2] if len(sys.argv)>2 else "v2-komplett"

numbers = req("GET", f"{base}/api/sets/{set_id}/numbers")
missing = [d for d,cfg in numbers.items() if not cfg.get("sound")]
print(f"→ Missing TTS for: {missing}")

for digit in sorted(missing):
    cfg = numbers[digit]
    word = cfg["word"]
    fname = f"{digit}_{slugify(word)}.mp3"
    print(f"  [{digit}] {word} → {fname}")
    r = req("POST", f"{base}/api/generate/tts",
            {"text": word, "word": word, "filename": fname})
    if not r.get("ok"):
        print(f"    error: {r}")
        continue
    jid = r["job_id"]
    # Poll until this one is done
    for _ in range(60):
        time.sleep(2)
        jobs = {j["id"]:j for j in req("GET", f"{base}/api/jobs")}
        j = jobs.get(jid)
        if j and j["status"] in ("done","error"):
            if j["status"] == "done":
                new_cfg = dict(cfg); new_cfg["sound"] = fname
                req("PUT", f"{base}/api/sets/{set_id}/numbers/{digit}", new_cfg)
                print(f"    ✓ done, wrote sound={fname}")
            else:
                print(f"    ✗ error: {j.get('error')}")
            break
    else:
        print(f"    timeout")
    time.sleep(3)  # polite delay between requests

print("✓ Retry pass done.")
