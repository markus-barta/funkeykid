#!/usr/bin/env python3
"""One-shot seeder: generate TTS + counting bg for every digit and wire
them into the active set. Run against a reachable funkeykid instance.

Usage: python3 scripts/seed_numbers.py http://hsb1.lan:8081 v2-komplett
"""
import json
import sys
import time
import urllib.request


def req(method, url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if body else {},
    )
    with urllib.request.urlopen(r, timeout=120) as resp:
        return json.load(resp) if resp.headers.get("Content-Type","").startswith("application/json") else resp.read()


def slugify(text):
    out = []
    for c in text.lower():
        out.append({"ä":"ae","ö":"oe","ü":"ue","ß":"ss"," ":"-"}.get(c, c))
    return "".join(ch for ch in "".join(out) if ch.isalnum() or ch in "-_")


def wait_jobs(base, ids, timeout=300):
    start = time.time()
    pending = set(ids)
    while pending and time.time() - start < timeout:
        jobs = req("GET", f"{base}/api/jobs")
        for j in jobs:
            if j["id"] in pending and j["status"] in ("done", "error"):
                status = j["status"]
                err = j.get("error", "")
                print(f"  {j['id']:12} {j['type']:5} {j.get('word',''):20} {status} {err}")
                pending.discard(j["id"])
        if pending:
            time.sleep(2)
    if pending:
        print(f"  TIMEOUT: {len(pending)} jobs still pending")
    return pending


def main():
    base = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://hsb1.lan:8081"
    set_id = sys.argv[2] if len(sys.argv) > 2 else "v2-komplett"
    print(f"→ Seeding numbers in set '{set_id}' at {base}")

    numbers = req("GET", f"{base}/api/sets/{set_id}/numbers")
    print(f"  Found {len(numbers)} digit(s) configured")

    job_map = {}  # filename → (digit, type)
    ids = []

    for digit in sorted(numbers.keys()):
        cfg = numbers[digit]
        word = cfg.get("word", "")
        subject = cfg.get("image_subject", "")
        if not word or not subject:
            print(f"  [{digit}] skip: missing word or image_subject")
            continue

        # TTS
        snd_filename = f"{digit}_{slugify(word)}.mp3"
        r = req("POST", f"{base}/api/generate/tts", {
            "text": word, "word": word, "filename": snd_filename,
        })
        if r.get("ok"):
            ids.append(r["job_id"])
            job_map[r["job_id"]] = (digit, "sound", snd_filename)
            print(f"  [{digit}] TTS queued  → {snd_filename}")
        else:
            print(f"  [{digit}] TTS error: {r.get('error')}")

        # Background image
        count = "no" if digit == "0" else digit
        img_filename = f"{digit}_{slugify(subject)}.png"
        prompt = (
            f"Flat bold child-friendly cartoon illustration showing exactly {count} {subject}, "
            f"arranged clearly on a plain white background, each object large and unambiguous "
            f"with clear outlines, high contrast, no text, no numbers, no shadows, "
            f"readable at 64x64 pixels. Minimalist counting picture for a toddler."
        )
        r = req("POST", f"{base}/api/generate/image", {
            "word": word,
            "description": f"{count} {subject}",
            "prompt": prompt,
            "filename": img_filename,
        })
        if r.get("ok"):
            ids.append(r["job_id"])
            job_map[r["job_id"]] = (digit, "image", img_filename)
            print(f"  [{digit}] image queued → {img_filename}")
        else:
            print(f"  [{digit}] image error: {r.get('error')}")

    print(f"\n→ Waiting for {len(ids)} jobs to finish (up to 5 min)…")
    pending = wait_jobs(base, ids)

    # Re-fetch job list to see which completed successfully
    jobs = {j["id"]: j for j in req("GET", f"{base}/api/jobs")}

    # Update numbers with filenames for successfully completed jobs
    print("\n→ Writing filenames back into number config")
    per_digit = {d: {"sound": None, "image": None} for d in numbers}
    for jid, (digit, kind, fname) in job_map.items():
        j = jobs.get(jid, {})
        if j.get("status") == "done":
            per_digit[digit][kind] = fname

    for digit, cfg in numbers.items():
        upd = per_digit[digit]
        new_cfg = dict(cfg)
        if upd["sound"]:
            new_cfg["sound"] = upd["sound"]
        if upd["image"]:
            new_cfg["backgrounds"] = [{"image": upd["image"], "enabled": True}]
        if upd["sound"] or upd["image"]:
            req("PUT", f"{base}/api/sets/{set_id}/numbers/{digit}", new_cfg)
            print(f"  [{digit}] → sound={new_cfg.get('sound','')} bg={upd['image'] or '—'}")

    print("\n✓ Done.")


if __name__ == "__main__":
    main()
