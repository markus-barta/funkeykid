#!/usr/bin/env bash
# Copy and rename sound files from Sound Ideas Series 1000 library
# Source: imac0 ~/Music/Diverse/sfx/Sound Effects Library .../Sound Ideas .../
# Run on imac0 to create the sounds directory for deployment to hsb1
#
# Usage: ./scripts/copy-sounds-from-library.sh [target-dir]
#   target-dir defaults to ./sounds/de-AT/

set -euo pipefail

LIB="/Users/markus/Music/Diverse/sfx/Sound Effects Library (28Cds) plus bonuses/Sound Ideas Sound Effects Library 1000 Series (28 CDs)"
TARGET="${1:-./sounds/de-AT}"

mkdir -p "$TARGET"

echo "Copying sounds from Series 1000 library..."
echo "Source: $LIB"
echo "Target: $TARGET"
echo ""

# Format: target_name source_cd source_track
copy_track() {
    local name="$1" cd="$2" track="$3"
    local src="$LIB/$cd/Track $(printf '%02d' "$track").mp3"
    if [[ -f "$src" ]]; then
        cp "$src" "$TARGET/$name"
        echo "  OK: $name ← $cd/Track $(printf '%02d' "$track").mp3"
    else
        echo "  MISSING: $src"
    fi
}

# A - Affe (Gibbon)
copy_track "affe.mp3" "1002 Animals" 37

# B - Biene (Bees)
copy_track "biene.mp3" "1006 Birds, Boxing, Buzzers" 15

# D - Donner (Thunder)
copy_track "donner.mp3" "1026 Rain, Thunder, Tennis, Taxis, Trains, Umbrellas, Vacuum Cleaners, Vendors" 13

# E - Elefant (Elephant)
copy_track "elefant.mp3" "1002 Animals" 31

# F - Frosch (Frog)
copy_track "frosch.mp3" "1002 Animals" 32

# G - Glocke (Church Bell)
copy_track "glocke.mp3" "1005 Baseball, Bells, Bicycles, Cameras, Clocks" 19

# H - Hammer (Construction)
copy_track "hammer.mp3" "1008 Construction" 1

# J - Jaguar (Tiger sound)
copy_track "jaguar.mp3" "1002 Animals" 52

# K - Katze (Cat)
copy_track "katze.mp3" "1002 Animals" 1

# L - Löwe (Lion)
copy_track "loewe.mp3" "1002 Animals" 39

# M - Muh-Kuh (Cow)
copy_track "kuh.mp3" "1002 Animals" 6

# N - Nachtigall (Birds)
copy_track "nachtigall.mp3" "1006 Birds, Boxing, Buzzers" 1

# P - Pferd (Horse)
copy_track "pferd.mp3" "1002 Animals" 61

# Q - Quaken (Duck)
copy_track "quaken.mp3" "1002 Animals" 28

# R - Regen (Rain)
copy_track "regen.mp3" "1026 Rain, Thunder, Tennis, Taxis, Trains, Umbrellas, Vacuum Cleaners, Vendors" 1

# S - Schwein (Pig)
copy_track "schwein.mp3" "1002 Animals" 40

# T - Telefon (Telephone bell)
copy_track "telefon.mp3" "1025 Stores, Swimming, Subways, Telephones" 44

# U - Uhu (Night bird — no owl in library, using thrushes as placeholder)
copy_track "uhu.mp3" "1006 Birds, Boxing, Buzzers" 24

# V - Vogel (Robin)
copy_track "vogel.mp3" "1006 Birds, Boxing, Buzzers" 17

# W - Wasser (Water stream)
copy_track "wasser.mp3" "1028 Water, Whistles, Wind, Zippers" 1

# Y - Yak (Cow variant)
copy_track "yak.mp3" "1002 Animals" 8

# Z - Ziege (Goat)
copy_track "ziege.mp3" "1002 Animals" 38

echo ""
echo "Notes:"
echo "  - C (Clown), I (Igel), O (Orgel), X (Xylophon) need manual sourcing or TTS-only"
echo "  - CD directory names may vary — check ls output if tracks are MISSING"
echo "  - Deploy to hsb1: rsync -avz $TARGET/ mba@hsb1.lan:/var/lib/funkeykid-sounds/"
