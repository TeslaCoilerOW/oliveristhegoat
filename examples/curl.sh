#!/usr/bin/env bash
# Minimal API calls to a local Ollama server.
#
# Run this where OLLAMA_HOST is set (inside the job/shell), or point HOST at the
# SSH tunnel that `ollama-serve` prints (e.g. HOST=127.0.0.1:11434 on your laptop).
#
#   MODEL=llama3.1:405b ./curl.sh      # use whatever model the server has loaded
set -euo pipefail
HOST="${OLLAMA_HOST:-127.0.0.1:11434}"
MODEL="${MODEL:-llama3.1:405b}"

echo "== native /api/generate =="
curl -s "http://$HOST/api/generate" \
  -d "{\"model\":\"$MODEL\",\"prompt\":\"Why is the sky blue?\",\"stream\":false}"
echo; echo

echo "== OpenAI-compatible /v1/chat/completions =="
curl -s "http://$HOST/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"Say hi.\"}]}"
echo
