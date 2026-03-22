#!/usr/bin/env bash
# Generate missing funkeykid sounds via ElevenLabs Sound Effects API
#
# Usage: ELEVENLABS_API_KEY=sk-... ./scripts/generate-missing-sounds.sh [target-dir]
#
# Requires: curl, jq

set -euo pipefail

API_KEY="${ELEVENLABS_API_KEY:?Set ELEVENLABS_API_KEY}"
TARGET="${1:-./sounds/de-AT}"
mkdir -p "$TARGET"

generate() {
    local name="$1" prompt="$2" duration="${3:-3}"
    local outfile="$TARGET/$name"

    if [[ -f "$outfile" ]]; then
        echo "  SKIP: $name (already exists)"
        return
    fi

    echo "  Generating: $name — \"$prompt\" (${duration}s)..."
    curl -s -X POST "https://api.elevenlabs.io/v1/sound-generation" \
        -H "xi-api-key: $API_KEY" \
        -H "Content-Type: application/json" \
        -d "{
            \"text\": \"$prompt\",
            \"duration_seconds\": $duration,
            \"prompt_influence\": 0.5
        }" \
        -o "$outfile"

    # Check if we got valid audio (not JSON error)
    if file "$outfile" | grep -q "MPEG\|Audio\|MP3\|data"; then
        local size=$(stat -f%z "$outfile" 2>/dev/null || stat -c%s "$outfile" 2>/dev/null)
        echo "    OK: $name (${size} bytes)"
    else
        echo "    ERROR: $name — got non-audio response:"
        head -c 200 "$outfile"
        echo ""
        rm "$outfile"
    fi
}

echo "Generating missing sounds via ElevenLabs..."
echo "Target: $TARGET"
echo ""

# A - Affe (monkey chattering)
generate "affe.mp3" "Playful monkey chattering and hooting, cute chimpanzee sounds" 4

# B - Biene (bee buzzing)
generate "biene.mp3" "Bee buzzing close up, single honeybee flying past" 3

# C - Clown (circus horn honk)
generate "clown.mp3" "Funny circus clown horn honking, comedic party horn" 2

# I - Igel (hedgehog snuffling)
generate "igel.mp3" "Small hedgehog snuffling and rustling through leaves" 3

# O - Orgel (church organ)
generate "orgel.mp3" "Grand church organ playing a majestic chord, pipe organ" 4

# X - Xylophon (xylophone melody)
generate "xylophon.mp3" "Xylophone playing a short cheerful melody, bright mallet percussion" 3

echo ""
echo "Done! Deploy to hsb1:"
echo "  rsync -avz $TARGET/ mba@hsb1.lan:/var/lib/funkeykid-sounds/"
