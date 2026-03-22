# Rename: pidicon-light → pixdcon — DO THIS

pixdcon is already deployed and running on hsb1 with new MQTT topics.
These are **only text/doc changes** — no runtime impact, no coordination needed.

## Steps

### 1. funkeykid.py — two docstring edits
```
Line ~6:   "pidicon-light" → "pixdcon"
Line ~153: "pidicon-light picks this up" → "pixdcon picks this up"
```

### 2. static/index.html — one dropdown label
```
Line ~370: <option value="mqtt">MQTT (pidicon-light)</option>
        →  <option value="mqtt">MQTT (pixdcon)</option>
```

### 3. DEVELOPMENT.md — search-replace `pidicon-light` → `pixdcon`
8 occurrences across architecture diagrams, section headers, mount paths, image cache notes.

### 4. README.md — search-replace `pidicon-light` → `pixdcon`
2 occurrences: MQTT reference and architecture diagram.

### 5. Commit + push
```bash
git add funkeykid.py static/index.html DEVELOPMENT.md README.md
git commit -m "docs: pidicon-light → pixdcon (rename complete)"
git push
```

### 6. Delete this file
```bash
trash rename_to-do.markdown
```

That's it. All four edits are string replacements, no logic changes.
