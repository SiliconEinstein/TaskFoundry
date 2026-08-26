#!/bin/sh
set -eu

mkdir -p /logs/verifier
reward=0
if [ -f /app/outputs/overlay-smoke.txt ] && \
   [ "$(cat /app/outputs/overlay-smoke.txt)" = "portable codex overlay ok" ]; then
  reward=1
fi
if [ ! -f /logs/artifacts/overlay-bootstrap.txt ] || \
   [ "$(cat /logs/artifacts/overlay-bootstrap.txt)" != "portable codex overlay ok" ]; then
  reward=0
fi
printf '%s\n' "$reward" > /logs/verifier/reward.txt
