"""Release history for funkeykid (FUNK-88): the INSPR schema "inspr.release-history.v1".

A port of hausnetz's internal/releasehistory at tag v261128145301.0.0 (itself a port of Aeon
afb08c0663cd3198a346d1f21e95dd4b2dab8a1b internal/releasehistory/{schema,group,build,notes,http}.go
and docs/RELEASE.md, UI from Hausvogt 4c23de532ea18264815cefd6501e00a67f8792b8). The JSON is written
the way Go's encoding/json writes it (key order = struct order, HTML characters escaped, raw UTF-8),
so the files are byte-compatible with the other INSPR products.

- release-history/history.json is generated at release preparation from the annotated v<version> tags,
  version.json at each tag and the commits between tags, then committed. The image only carries those
  bytes; nothing is generated at runtime and no reader fetches live ticket text.
- release-notes/<version>.json ("inspr.release-note-snapshot.v1") freezes a release's public ticket
  texts (EN/DE pill + benefit) when the version is reserved (scripts/reserve_version.py --tickets).
  After tagging only the tag's copy is read.
- Releases up to LAST_UNCAPTURED were made before captures existed: they say so ("unavailable"),
  never claiming bilingual text that was not recorded then.

funkeykid differences: version.json is the head record (no reserved_at: the coordinate is the
reservation second); tag messages before FUNK-88 read "<v> (stable #N): <headline>".
"""
import datetime
import hashlib
import json
import os
import re
import subprocess

SCHEMA = "inspr.release-history.v1"
SNAPSHOT_SCHEMA = "inspr.release-note-snapshot.v1"
PRODUCT = "funkeykid"
REPOSITORY = "markus-barta/funkeykid"
SCHEMES = ("inspr-calver-3", "inspr-calendar-v2")
LAST_UNCAPTURED = "261128154900.0.0"      # the last release made before frozen captures (FUNK-88)
MAX_CHANGES = 300
HISTORY_PATH = os.path.join("release-history", "history.json")
NOTES_DIR = "release-notes"
VERSION_FILE = "version.json"
MEMBERSHIP_SOURCE = "release-manifest-tickets"
FIELD_SOURCE = "nodes.fields"

TICKET_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-[1-9]\d{0,6}\b", re.ASCII)
_VERSION_RE = re.compile(r"[1-9]\d(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])([01]\d|2[0-3])[0-5]\d[0-5]\d\.0\.0", re.ASCII)
_CONV_RE = re.compile(r"([a-z]+)(?:\(([^)]*)\))?!?:\s*(.*)", re.ASCII | re.DOTALL)
_SHA_RE = re.compile(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", re.ASCII)
_SHA256_RE = re.compile(r"[a-f0-9]{64}", re.ASCII)
_REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", re.ASCII)
_SEQ_PART_RE = re.compile(r"release_sequence\s+(\d+)", re.ASCII)
_FK_TAG_RE = re.compile(r"v?(\S+) \(([a-z]+) #(\d+)\): (.*)", re.ASCII | re.DOTALL)   # funkeykid before FUNK-88
TYPE_MAP = {"feat": "feat", "feature": "feat", "fix": "fix", "hotfix": "fix", "test": "test", "tests": "test",
            "docs": "docs", "doc": "docs", "release": "release", "refactor": "refactor", "perf": "refactor",
            "chore": "chore", "ci": "chore", "build": "chore", "style": "chore"}

GAP_MISSING = "Bilingual ticket benefits were not captured for this release."
EVIDENCE_ALWAYS = "GitHub publication time, image digest and CI runs were not recorded for this build."
EVIDENCE_NO_TAG = "No annotated release tag was recorded; publication is pending confirmation."
EVIDENCE_NO_COMMIT = "No source commit was found in main's first-parent VERSION history for this reservation."
PENDING_OFFLINE = "The running release source or a local main ref is unavailable offline."
PENDING_NOT_ANCESTOR = "The running release is not an ancestor of the recorded main ref."
PENDING_SYNTH = "Main history since the running release was not recorded in this build."


class HistoryError(ValueError):
    pass


# ── encoding ────────────────────────────────────────────────────────────────

def gojson(obj, indent=None):
    """Like Go's encoding/json Marshal / MarshalIndent(obj, "", "  "): raw UTF-8, HTML characters escaped."""
    s = json.dumps(obj, ensure_ascii=False, indent=indent, separators=(",", ":") if indent is None else (",", ": "))
    for a, b in (("&", "\\u0026"), ("<", "\\u003c"), (">", "\\u003e"), (" ", "\\u2028"), (" ", "\\u2029")):
        s = s.replace(a, b)
    return s


def dump_file(obj):
    return (gojson(obj, 2) + "\n").encode("utf-8")


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)


def rfc3339(dt):
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── versions, tickets, commits ──────────────────────────────────────────────

def valid_version(v):
    if not isinstance(v, str) or not _VERSION_RE.fullmatch(v):
        return False
    try:
        d = datetime.datetime.strptime("20" + v[:12], "%Y%m%d%H%M%S")
    except ValueError:
        return False
    return 2010 <= d.year <= 2099 and d.strftime("%Y%m%d%H%M%S")[2:] == v[:12]


def version_instant(v):
    """The reservation second a coordinate names, as RFC 3339 UTC."""
    d = datetime.datetime.strptime("20" + v[:12], "%Y%m%d%H%M%S").replace(tzinfo=datetime.timezone.utc)
    return rfc3339(d)


def tickets(*texts):
    out = []
    for t in texts:
        for m in TICKET_RE.findall(t or ""):
            if m not in out:
                out.append(m)
    return out


def classify(subject):
    m = _CONV_RE.fullmatch((subject or "").strip())
    if not m:
        return "other", ""
    return TYPE_MAP.get(m.group(1), "other"), m.group(2) or ""


_RANK = {"other": 0, "features": 1, "fixes": 2}


def group_change(ctype, ticket_keys, meta):
    if ctype == "release":
        return ""
    if ctype == "feat":
        return "features"
    if ctype == "fix":
        return "fixes"
    if ctype in ("test", "docs", "refactor", "chore"):
        return "other"
    best = "other"
    for t in ticket_keys:
        m = meta.get(t)
        if m is None:
            continue
        g = "fixes" if m["bug"] else ("features" if m["benefit"] else "other")
        if _RANK[g] > _RANK[best]:
            best = g
    return best


# ── notes and snapshots ─────────────────────────────────────────────────────

def missing_notes():
    return {"source": "unavailable", "fallback": "historical-tag-headline", "snapshot_sha256": "", "captured_at": None,
            "release_revision": 0, "items": [], "gaps": [GAP_MISSING], "hidden": 0}


def _text(v):
    return v.strip() if isinstance(v, str) else ""


def _is_bug(export):
    if isinstance(export.get("type"), str) and export["type"].strip().lower() == "bug":
        return True
    tags = export.get("tags")
    if isinstance(tags, list):
        for t in tags:
            name = t.get("name") if isinstance(t, dict) else t
            if isinstance(name, str) and name.strip().lower() == "bug":
                return True
    return False


def freeze(scope, version, scheme, now):
    """The frozen release-note snapshot for a release's reviewed ticket exports (aeon --json issue get)."""
    if not isinstance(scope, list):
        raise HistoryError("ticket scope must be a JSON array of issue exports")
    items, gaps, hidden, seen = [], [], 0, set()
    for ex in scope:
        if not isinstance(ex, dict):
            raise HistoryError("ticket scope must be a JSON array of issue exports")
        key, hide = ex.get("issue_key"), ex.get("hide_from_release_notes")
        if hide is not None and not isinstance(hide, bool):
            raise HistoryError(f"{key}: hide_from_release_notes must be true or false")
        if hide:
            hidden += 1
            continue
        if not isinstance(key, str) or not TICKET_RE.fullmatch(key) or key in seen:
            raise HistoryError(f"not one unique ticket key: {key!r}")
        seen.add(key)
        texts = {k: _text(ex.get(k)) for k in ("pill_en", "pill_de", "benefit_en", "benefit_de")}
        if not any(texts.values()):
            gaps.append(f"{key}: no public pill or benefit was captured.")
            continue
        items.append({"key": key, **texts, "group": "fixes" if _is_bug(ex) else "features"})
        if not all(texts.values()):
            gaps.append(f"{key}: English or German release text is incomplete.")
    return {"schema": SNAPSHOT_SCHEMA, "version": version, "version_scheme": scheme, "captured_at": rfc3339(now),
            "membership_source": MEMBERSHIP_SOURCE, "field_source": FIELD_SOURCE, "frozen": True,
            "items": items, "hidden": hidden, "gaps": gaps}


_SNAPSHOT_KEYS = ("schema", "version", "version_scheme", "captured_at", "membership_source", "field_source",
                  "frozen", "items", "hidden", "gaps")
_ITEM_KEYS = ("key", "pill_en", "pill_de", "benefit_en", "benefit_de", "group")


def notes_from_snapshot(raw, version, source):
    """Strictly read a frozen snapshot (unknown or missing keys, trailing data: rejected) into release notes."""
    try:
        s = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise HistoryError(f"{source}: not a snapshot ({e})")
    if not isinstance(s, dict) or set(s) != set(_SNAPSHOT_KEYS):
        raise HistoryError(f"{source}: unexpected snapshot fields")
    if s["schema"] != SNAPSHOT_SCHEMA or s["frozen"] is not True or s["version"] != version or not valid_version(version):
        raise HistoryError(f"{source}: not a frozen snapshot of {version}")
    if s["version_scheme"] not in SCHEMES or s["membership_source"] != MEMBERSHIP_SOURCE or s["field_source"] != FIELD_SOURCE:
        raise HistoryError(f"{source}: unexpected scheme or sources")
    if not _is_time(s["captured_at"]) or type(s["hidden"]) is not int or s["hidden"] < 0:
        raise HistoryError(f"{source}: bad capture time or hidden count")
    if not isinstance(s["items"], list) or not isinstance(s["gaps"], list) or not all(isinstance(g, str) for g in s["gaps"]):
        raise HistoryError(f"{source}: items and gaps must be lists")
    keys = set()
    for it in s["items"]:
        if not isinstance(it, dict) or not set(it) <= set(_ITEM_KEYS) or not set(_ITEM_KEYS[:5]) <= set(it):
            raise HistoryError(f"{source}: unexpected item fields")
        if not all(isinstance(it[k], str) for k in _ITEM_KEYS[:5]):
            raise HistoryError(f"{source}: item texts must be strings")
        if not TICKET_RE.fullmatch(it["key"]) or it["key"] in keys or it.get("group") not in ("features", "fixes", "other"):
            raise HistoryError(f"{source}: bad item {it.get('key')!r}")
        keys.add(it["key"])
    return {"source": source, "snapshot_sha256": hashlib.sha256(raw).hexdigest(), "captured_at": s["captured_at"],
            "release_revision": 0, "items": s["items"], "gaps": s["gaps"], "hidden": s["hidden"]}


def snapshot_scheme(raw):
    try:
        return json.loads(raw.decode("utf-8")).get("version_scheme")
    except (UnicodeDecodeError, ValueError, AttributeError):
        return None


def _is_time(v):
    """An RFC 3339 UTC second as these files write it: YYYY-MM-DDThh:mm:ssZ."""
    if not isinstance(v, str) or len(v) != 20 or not v.endswith("Z"):
        return False
    try:
        datetime.datetime.strptime(v, "%Y-%m-%dT%H:%M:%SZ")
        return True
    except ValueError:
        return False


def notes_headline(items):
    """The English headline of a release from its pills (first three, then "+N more"); maintenance if none."""
    pills = [it["pill_en"].strip() for g in ("features", "fixes", "other") for it in items
             if it.get("group", "other") == g and it.get("pill_en", "").strip()]
    if not pills:
        return "Maintenance and reliability"
    return " · ".join(pills[:3]) + (f" · +{len(pills) - 3} more" if len(pills) > 3 else "")


def snapshot_gaps_error(snapshot):
    if snapshot["gaps"]:
        return ("release text missing; give each ticket EN and DE pills and benefits, or hide it from release notes:\n  "
                + "\n  ".join(snapshot["gaps"]))
    return None


# ── git ─────────────────────────────────────────────────────────────────────

def _git(root, *args, check=True):
    r = subprocess.run(["git", "-C", root, *args], capture_output=True)
    if check and r.returncode != 0:
        raise HistoryError(f"git {' '.join(args)}: {r.stderr.decode('utf-8', 'replace').strip()}")
    return r


def _git_text(root, *args):
    return _git(root, *args).stdout.decode("utf-8", "replace")


def _show(root, rev, path):
    r = _git(root, "show", f"{rev}:{path}", check=False)
    return r.stdout if r.returncode == 0 else None


def require_full_history(root):
    if not os.path.exists(os.path.join(root, ".git")):
        raise HistoryError("release history requires a full-depth checkout with tags: no .git")
    if _git_text(root, "rev-parse", "--is-shallow-repository").strip() != "false":
        raise HistoryError("release history requires a full-depth checkout with tags: shallow clone")
    if not _git_text(root, "for-each-ref", "--count=1", "--format=%(refname)", "refs/tags/v*").strip():
        raise HistoryError("release history requires a full-depth checkout with tags: no v* tags")


def _version_record(raw):
    try:
        d = json.loads(raw)
        return d if isinstance(d, dict) else None
    except (ValueError, TypeError):
        return None


def parse_tag_message(message):
    """(headline, channel, sequence) from an INSPR annotation, or funkeykid's earlier form; else the first line."""
    first = (message or "").strip().split("\n", 1)[0]
    parts = first.split(" · ")
    if len(parts) >= 5:
        for i, p in enumerate(parts):
            m = _SEQ_PART_RE.fullmatch(p)
            if m and i >= 1:
                return " · ".join(parts[i + 1:]), parts[i - 1], int(m.group(1))
    m = _FK_TAG_RE.fullmatch(first)
    if m:
        return m.group(4), m.group(2), int(m.group(3))
    return first, "", 0


def _changes(root, frm, to):
    rng = f"{frm}..{to}" if frm else to
    out = _git_text(root, "log", "--no-merges", "--format=%H%x1f%s%x1f%aI%x1e", rng)
    changes = []
    for rec in out.split("\x1e"):
        rec = rec.strip()
        if not rec:
            continue
        f = rec.split("\x1f")
        if len(f) != 3:
            continue
        ctype, scope = classify(f[1])
        changes.append({"commit": f[0], "subject": f[1], "type": ctype, "scope": scope,
                        "tickets": tickets(scope, f[1]), "at": f[2]})
    return changes[:MAX_CHANGES], max(0, len(changes) - MAX_CHANGES)


def _empty_release(v):
    return {"version_scheme": "inspr-calver-3", "notes": missing_notes(), "version": v, "tag": "",
            "release_channel": "stable", "release_sequence": 0, "state": "reserved",
            "reserved_at": version_instant(v), "tagged_at": None, "published_at": None, "headline": "",
            "tickets": [], "changes": [], "changes_omitted": 0,
            "evidence": {"source_commit": "", "source_url": "", "image": None, "ci": None, "release_run": None,
                         "release_url": "", "unavailable": []}}


def _ordered(r):
    """A release in the schema's key order (presentation only when there is one)."""
    out = {"version_scheme": r["version_scheme"], "notes": r["notes"]}
    if r.get("presentation"):
        out["presentation"] = r["presentation"]
    for k in ("version", "tag", "release_channel", "release_sequence", "state", "reserved_at", "tagged_at",
              "published_at", "headline", "tickets", "changes", "changes_omitted", "evidence"):
        out[k] = r[k]
    return out


def _to_utc(s):
    try:
        return rfc3339(datetime.datetime.fromisoformat(s.strip().replace("Z", "+00:00")))
    except (ValueError, AttributeError):
        return None


def build(root, now=None):
    """The history from Git: the annotated v<version> tags, version.json at each, the commits between."""
    now = now or utc_now()
    require_full_history(root)
    head = _version_record(open(os.path.join(root, VERSION_FILE), "rb").read())
    if not head or not valid_version(head.get("version")) or head.get("version_scheme") not in SCHEMES:
        raise HistoryError("version.json: no valid calendar version")
    recorded = None
    hp = os.path.join(root, HISTORY_PATH)
    if os.path.exists(hp):
        recorded = json.loads(open(hp, "rb").read())
        if recorded.get("schema") != SCHEMA or recorded.get("product") != PRODUCT or recorded.get("repository") != REPOSITORY:
            raise HistoryError("existing history.json is for another product or schema")
    rec_by_v = {r["version"]: r for r in (recorded or {}).get("releases", [])}

    fmt = "%(refname:short)%1f%(objecttype)%1f%(taggerdate:iso-strict)%1f%(*objectname)%1f%(objectname)%1f%(committerdate:iso-strict)%1f%(contents)%1e"
    raw_tags = _git_text(root, "for-each-ref", "--sort=refname", f"--format={fmt}", "refs/tags/v*")
    rels = {}
    for rec in raw_tags.split("\x1e"):
        rec = rec.lstrip("\n")
        if not rec.strip():
            continue
        f = rec.split("\x1f", 6)
        if len(f) != 7:
            continue
        name, otype, tdate, peeled, obj, cdate, contents = f
        v = name[1:] if name.startswith("v") else ""
        if otype not in ("tag", "commit") or not valid_version(v):
            continue
        at_tag = _version_record(_show(root, name, VERSION_FILE))
        if otype == "commit":
            if not at_tag or at_tag.get("version") != v:
                continue
            commit, message, date = obj, "", cdate
        else:
            commit, message, date = peeled, contents, tdate
        r = _empty_release(v)
        r.update(tag=name, tagged_at=_to_utc(date), state="published")
        headline, channel, seq = parse_tag_message(message)
        r["headline"] = headline
        r["release_channel"] = channel or "stable"
        r["release_sequence"] = seq
        if at_tag and at_tag.get("version") == v:
            if at_tag.get("version_scheme") in SCHEMES:
                r["version_scheme"] = at_tag["version_scheme"]
            if at_tag.get("release_channel"):
                r["release_channel"] = at_tag["release_channel"]
            if not r["release_sequence"] and isinstance(at_tag.get("release_sequence"), int):
                r["release_sequence"] = at_tag["release_sequence"]
        r["tickets"] = tickets(headline)
        r["evidence"]["source_commit"] = commit
        path = f"{NOTES_DIR}/{v}.json"
        if _git_text(root, "ls-tree", "--name-only", name, "--", path).strip():
            raw = _show(root, name, path)
            r["notes"] = notes_from_snapshot(raw, v, f"{name}:{path}")
            if snapshot_scheme(raw) != r["version_scheme"]:
                raise HistoryError(f"{name}:{path}: scheme differs from the release")
        rels[v] = r
    for v, r in rec_by_v.items():
        if r.get("tag") and v not in rels:
            raise HistoryError(f"recorded tag {r['tag']} is missing; fetch all release tags")

    hv = head["version"]
    r = rels.get(hv) or _empty_release(hv)
    r["version_scheme"] = head["version_scheme"]
    r["release_channel"] = head.get("release_channel") or "stable"
    r["release_sequence"] = int(head.get("release_sequence") or 0)
    rels[hv] = r

    for c in _git_text(root, "log", "--first-parent", "--format=%H", "--", VERSION_FILE).split():
        rec = _version_record(_show(root, c, VERSION_FILE))
        v = rec.get("version") if rec else None
        if v in rels and not rels[v]["evidence"]["source_commit"]:
            rels[v]["evidence"]["source_commit"] = c
            if rec.get("version_scheme") in SCHEMES:
                rels[v]["version_scheme"] = rec["version_scheme"]
            if isinstance(rec.get("release_sequence"), int):
                rels[v]["release_sequence"] = rec["release_sequence"]
            if rec.get("release_channel"):
                rels[v]["release_channel"] = rec["release_channel"]
    head_commit = rels[hv]["evidence"]["source_commit"]

    prev_pub = prev_any = ""
    for v in sorted(rels):
        r = rels[v]
        sc = r["evidence"]["source_commit"]
        if sc:
            r["changes"], r["changes_omitted"] = _changes(root, prev_any if r["state"] == "reserved" else prev_pub, sc)
            prev_any = sc
            if r["state"] == "published":
                prev_pub = sc
        path = os.path.join(root, NOTES_DIR, f"{v}.json")
        if not r["tag"] and os.path.exists(path):
            raw = open(path, "rb").read()
            r["notes"] = notes_from_snapshot(raw, v, f"{NOTES_DIR}/{v}.json")
            if snapshot_scheme(raw) != r["version_scheme"]:
                raise HistoryError(f"{NOTES_DIR}/{v}.json: scheme differs from the release")
        old = rec_by_v.get(v)
        if old and old["notes"].get("captured_at") and old["notes"].get("snapshot_sha256") != r["notes"].get("snapshot_sha256"):
            raise HistoryError(f"frozen notes changed for {v}")
        if v > LAST_UNCAPTURED and not r["notes"].get("captured_at"):
            raise HistoryError(f"{v} needs {NOTES_DIR}/{v}.json captured at reservation")
        meta = {it["key"]: {"bug": it.get("group") == "fixes", "benefit": it.get("group") == "features", "note": it}
                for it in r["notes"]["items"] if it.get("key")}
        for ch in r["changes"]:
            g = group_change(ch["type"], ch["tickets"], meta)
            if g:
                ch["group"] = g
            if g in ("features", "fixes"):
                linked = []
                for t in ch["tickets"]:
                    if t in meta and t not in [x["key"] for x in linked]:
                        linked.append(dict(meta[t]["note"], key=t, group="fixes" if meta[t]["bug"] else "features"))
                if linked:
                    ch["linked_tickets"] = linked
        r["tickets"] = tickets(*r["tickets"], *[it["key"] for it in r["notes"]["items"] if it.get("key")], r["headline"])
        if r["notes"].get("captured_at"):
            r["presentation"] = {"headline": notes_headline(r["notes"]["items"]), "intro": ""}
            if not r["headline"]:
                r["headline"] = r["presentation"]["headline"]
        r["evidence"]["source_url"] = f"https://github.com/{REPOSITORY}/commit/{sc}" if sc else ""
        un = [EVIDENCE_ALWAYS]
        if not r["tag"]:
            un.append(EVIDENCE_NO_TAG)
        if not sc:
            un.append(EVIDENCE_NO_COMMIT)
        r["evidence"]["unavailable"] = un

    pending = {"base_version": hv, "source_commit": head_commit, "source_ref": "", "changes": [], "changes_omitted": 0,
               "unavailable": []}
    main_ref = ""
    for ref in ("refs/heads/main", "refs/remotes/origin/main"):
        if _git(root, "rev-parse", "--verify", "--quiet", ref, check=False).returncode == 0:
            main_ref = ref
            break
    if not head_commit or not main_ref:
        pending.update(source_commit="", source_ref="", unavailable=[PENDING_OFFLINE])
    else:
        main_sha = _git_text(root, "rev-parse", main_ref).strip()          # the recorded main tip
        pending.update(source_commit=main_sha, source_ref=main_ref)
        if _git(root, "merge-base", "--is-ancestor", head_commit, main_ref, check=False).returncode != 0:
            pending["unavailable"] = [PENDING_NOT_ANCESTOR]
        else:
            pending["changes"], pending["changes_omitted"] = _changes(root, head_commit, main_sha)

    h = {"schema": SCHEMA, "product": PRODUCT, "repository": REPOSITORY, "version_scheme": head["version_scheme"],
         "generated_at": rfc3339(now), "source": "git",
         "releases": [_ordered(rels[v]) for v in sorted(rels, reverse=True)], "pending": pending}
    validate(h)
    return h


# ── validation ──────────────────────────────────────────────────────────────

def _strs(v):
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def _check_changes(changes, where):
    if not isinstance(changes, list):
        raise HistoryError(f"{where}: changes must be a list")
    for c in changes:
        if not isinstance(c, dict) or not all(isinstance(c.get(k), str) for k in ("commit", "subject", "type", "scope", "at")):
            raise HistoryError(f"{where}: bad change")
        if not _SHA_RE.fullmatch(c["commit"]) or not _strs(c.get("tickets")) or not isinstance(c.get("group", ""), str):
            raise HistoryError(f"{where}: bad change {c.get('commit')!r}")
        if c.get("group", "") not in ("", "features", "fixes", "other"):
            raise HistoryError(f"{where}: bad change group")
        if "linked_tickets" in c:
            _check_items(c["linked_tickets"], where)


def _check_items(items, where):
    if not isinstance(items, list):
        raise HistoryError(f"{where}: items must be a list")
    for it in items:
        if not isinstance(it, dict) or not all(isinstance(it.get(k), str) for k in _ITEM_KEYS[:5]):
            raise HistoryError(f"{where}: bad note item")
        if it.get("group") not in ("features", "fixes", "other"):
            raise HistoryError(f"{where}: bad note group")


def validate(h):
    """The schema as Go's decoder and Validate check it: every field's type, every time, every list non-null."""
    def fail(msg):
        raise HistoryError(msg)
    if not isinstance(h, dict) or h.get("schema") != SCHEMA or h.get("product") != PRODUCT:
        fail("not a funkeykid release history")
    if not isinstance(h.get("repository"), str) or not _REPO_RE.fullmatch(h["repository"]):
        fail("bad repository")
    if h.get("version_scheme") not in SCHEMES or not _is_time(h.get("generated_at")) or h.get("source") != "git":
        fail("bad envelope")
    if not isinstance(h.get("releases"), list):
        fail("releases must be a list")
    prev = None
    for r in h["releases"]:
        if not isinstance(r, dict):
            fail("bad release")
        v = r.get("version")
        if not valid_version(v) or r.get("version_scheme") not in SCHEMES:
            fail(f"bad release {v!r}")
        if prev is not None and not v < prev:
            fail(f"releases out of order at {v}")
        prev = v
        if not isinstance(r.get("release_channel"), str) or not r["release_channel"]:
            fail(f"{v}: bad channel")
        if type(r.get("release_sequence")) is not int or r["release_sequence"] < 0:
            fail(f"{v}: bad sequence")
        if type(r.get("changes_omitted")) is not int or r["changes_omitted"] < 0 or not isinstance(r.get("headline"), str):
            fail(f"{v}: bad headline or omitted count")
        for k in ("reserved_at", "tagged_at", "published_at"):
            if r.get(k) is not None and not _is_time(r[k]):
                fail(f"{v}: bad {k}")
        if not _strs(r.get("tickets")):
            fail(f"{v}: tickets must be strings")
        _check_changes(r.get("changes"), v)
        if r.get("state") not in ("published", "reserved") or r.get("tag") not in ("", "v" + v):
            fail(f"{v}: bad state or tag")
        if r["state"] == "published" and not r["tag"]:
            fail(f"{v}: published without a tag")
        pres = r.get("presentation")
        if pres is not None and (not isinstance(pres, dict) or not isinstance(pres.get("headline"), str) or not isinstance(pres.get("intro"), str)):
            fail(f"{v}: bad presentation")
        ev = r.get("evidence")
        if not isinstance(ev, dict) or not all(isinstance(ev.get(k), str) for k in ("source_commit", "source_url", "release_url")):
            fail(f"{v}: bad evidence")
        if not _strs(ev.get("unavailable")) or any(ev.get(k) is not None and not isinstance(ev[k], dict) for k in ("image", "ci", "release_run")):
            fail(f"{v}: bad evidence")
        if ev["source_url"] and not ev["source_commit"]:
            fail(f"{v}: source url without commit")
        if ev["source_commit"]:
            if not _SHA_RE.fullmatch(ev["source_commit"]) or ev["source_url"] != f"https://github.com/{h['repository']}/commit/{ev['source_commit']}":
                fail(f"{v}: bad source commit")
        n = r.get("notes")
        if not isinstance(n, dict) or not isinstance(n.get("source"), str) or not n["source"] or not _strs(n.get("gaps")):
            fail(f"{v}: bad notes")
        if not isinstance(n.get("snapshot_sha256"), str) or type(n.get("hidden")) is not int or n["hidden"] < 0 or type(n.get("release_revision")) is not int:
            fail(f"{v}: bad notes fields")
        if n.get("captured_at") is not None and not _is_time(n["captured_at"]):
            fail(f"{v}: bad capture time")
        if n["source"] != "unavailable" and not _SHA256_RE.fullmatch(n["snapshot_sha256"]):
            fail(f"{v}: notes without digest")
        _check_items(n.get("items"), v)
    p = h.get("pending")
    if p is not None:
        if not isinstance(p, dict) or not valid_version(p.get("base_version")) or not isinstance(p.get("source_commit"), str) \
                or not isinstance(p.get("source_ref"), str) or type(p.get("changes_omitted")) is not int or not _strs(p.get("unavailable")):
            fail("bad pending")
        _check_changes(p.get("changes"), "pending")


def validate_sources(root, h):
    """Offline check (no Git): the committed history matches version.json and every frozen capture on disk."""
    validate(h)
    head = _version_record(open(os.path.join(root, VERSION_FILE), "rb").read()) or {}
    top = h["releases"][0] if h["releases"] else None
    if not top or top["version"] != head.get("version") or h["version_scheme"] != head.get("version_scheme"):
        raise HistoryError("committed release history is stale: regenerate it at release preparation (scripts/release_history.py)")
    if h.get("pending") and h["pending"]["base_version"] != head["version"]:
        raise HistoryError("pending base is not the current version")
    if (top["reserved_at"] != version_instant(head["version"]) or top["release_channel"] != head.get("release_channel")
            or top["release_sequence"] != head.get("release_sequence") or top["version_scheme"] != head.get("version_scheme")):
        raise HistoryError("current release fields differ from version.json")
    for r in h["releases"]:
        v = r["version"]
        if v <= LAST_UNCAPTURED and not r["notes"].get("captured_at"):
            continue
        path = os.path.join(root, NOTES_DIR, f"{v}.json")
        if not os.path.exists(path):
            raise HistoryError(f"{NOTES_DIR}/{v}.json is missing")
        src = f"{r['tag']}:{NOTES_DIR}/{v}.json" if r["tag"] else f"{NOTES_DIR}/{v}.json"
        raw = open(path, "rb").read()
        if notes_from_snapshot(raw, v, src) != r["notes"]:
            raise HistoryError(f"frozen notes changed for {v}")
        if snapshot_scheme(raw) != r["version_scheme"]:
            raise HistoryError(f"{NOTES_DIR}/{v}.json: scheme differs from the release")


def generate(root, out=None, now=None):
    h = build(root, now)
    validate_sources(root, h)
    path = out or os.path.join(root, HISTORY_PATH)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(dump_file(h))
    os.replace(tmp, path)
    return h
