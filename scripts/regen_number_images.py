#!/usr/bin/env python3
"""Regenerate every digit's background with Gemini 3 Pro Image + Pixar-style
prompt. Each request is fired sequentially so we don't trip OpenRouter's
per-key concurrency limit, and each finished image is wired into the number
config as the ONLY enabled background (old variants are archived in-place as
disabled entries, so you can revert from the UI).

Usage: python3 scripts/regen_number_images.py [BASE_URL] [SET_ID]
"""
import json
import sys
import time
import urllib.request

BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://hsb1.lan:8081"
SET_ID = sys.argv[2] if len(sys.argv) > 2 else "v2-komplett"
MODEL = "google/gemini-3-pro-image-preview"


def req(method, url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if body else {},
    )
    with urllib.request.urlopen(r, timeout=180) as resp:
        return json.load(resp) if resp.headers.get("Content-Type","").startswith("application/json") else resp.read()


def slugify(text):
    out = []
    for c in text.lower():
        out.append({"ä":"ae","ö":"oe","ü":"ue","ß":"ss"," ":"-",",":""}.get(c, c))
    return "".join(ch for ch in "".join(out) if ch.isalnum() or ch in "-_")[:40]


def pixar_prompt(count_word, subject):
    return (
        f"Pixar-style 3D cartoon illustration of exactly {count_word} {subject}. "
        f"Vibrant saturated colors, soft studio lighting, rounded friendly shapes, "
        f"big expressive eyes. Simple clean composition, objects arranged clearly "
        f"so all {count_word} are visible with space between them, plain cream or "
        f"pastel background. No text, no numbers, no letters — only the objects. "
        f"Studio quality children's animation style, readable at 64x64 pixels."
    )


NUMBER_WORDS = {
    "0": "no", "1": "one", "2": "two", "3": "three", "4": "four",
    "5": "five", "6": "six", "7": "seven", "8": "eight", "9": "nine",
}


def main():
    print(f"→ Regen numbers {SET_ID} @ {BASE} with {MODEL}")
    numbers = req("GET", f"{BASE}/api/sets/{SET_ID}/numbers")
    for digit in sorted(numbers.keys()):
        if digit == "0":
            print(f"  [0] skip: special gray-zero bg managed separately")
            continue
        cfg = numbers[digit]
        subject = cfg.get("image_subject", "")
        if not subject:
            print(f"  [{digit}] skip: no image_subject")
            continue
        count_word = NUMBER_WORDS[digit]
        prompt = pixar_prompt(count_word, subject)
        fname = f"{digit}_{slugify(subject)}_pixar.png"
        print(f"  [{digit}] → {fname}")
        r = req("POST", f"{BASE}/api/generate/image", {
            "word": cfg.get("word", digit),
            "description": f"{count_word} {subject}",
            "prompt": prompt,
            "filename": fname,
            "model": MODEL,
        })
        if not r.get("ok"):
            print(f"    error: {r}")
            continue
        jid = r["job_id"]
        # Poll until done
        for _ in range(90):
            time.sleep(2)
            jobs = {j["id"]: j for j in req("GET", f"{BASE}/api/jobs")}
            j = jobs.get(jid)
            if j and j["status"] in ("done", "error"):
                if j["status"] == "done":
                    # Disable old bgs, add new one as the sole enabled bg
                    old_bgs = [
                        {"image": b["image"], "enabled": False}
                        for b in cfg.get("backgrounds", [])
                    ]
                    new_cfg = dict(cfg)
                    new_cfg["backgrounds"] = [{"image": fname, "enabled": True}] + old_bgs
                    req("PUT", f"{BASE}/api/sets/{SET_ID}/numbers/{digit}", new_cfg)
                    print(f"    ✓ done, wired as primary bg")
                else:
                    print(f"    ✗ error: {j.get('error')}")
                break
        else:
            print(f"    timeout")
        time.sleep(2)  # polite gap between OpenRouter calls

    print("✓ Regen pass done.")


if __name__ == "__main__":
    main()
