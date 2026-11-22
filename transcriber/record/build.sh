#!/usr/bin/env bash
# Builds transcriber/bin/Quorum Recorder.app (the call recorder) from quorum_record.swift. Ad-hoc signed, so macOS
# keeps its own Screen & System Audio Recording and Microphone permissions for the app. macOS ties those to the
# build's hash (cdhash): rebuilding unchanged source gives the same hash and keeps them, but a changed recorder looks
# like a new app and must be allowed again (remove the old entry with − first). A name-only requirement
# (-r 'designated => identifier …') is not accepted by macOS for screen recording, so don't use one.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
app="$here/../bin/Quorum Recorder.app"
rm -rf "$app.tmp" && mkdir -p "$app.tmp/Contents/MacOS"
cp "$here/Info.plist" "$app.tmp/Contents/Info.plist"
swiftc -O -swift-version 5 "$here/quorum_record.swift" -o "$app.tmp/Contents/MacOS/quorum-record"
codesign --force --sign - "$app.tmp" >/dev/null 2>&1 || true
rm -rf "$app" && mv "$app.tmp" "$app"
echo "$app"
