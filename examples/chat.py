#!/usr/bin/env python3
"""Minimal chat against a local Ollama server, using the OpenAI-compatible API.

Where to run it:
  * INSIDE the same job/shell where the server is running (OLLAMA_HOST is
    already set for you), or
  * on your LAPTOP through the SSH tunnel that `ollama-serve` prints -- then set
    OLLAMA_HOST=127.0.0.1:11434 (the tunnel's local port).

    pip install openai
    python chat.py "Write a haiku about GPUs."
"""
import os
import sys

from openai import OpenAI

host = os.environ.get("OLLAMA_HOST", "127.0.0.1:11434")
model = os.environ.get("MODEL", "llama3.1:405b")  # match whatever the server loaded
prompt = " ".join(sys.argv[1:]) or "Say hello in one sentence."

# The API key is required by the client but ignored by Ollama.
client = OpenAI(base_url=f"http://{host}/v1", api_key="ollama")

resp = client.chat.completions.create(
    model=model,
    messages=[{"role": "user", "content": prompt}],
)
print(resp.choices[0].message.content)
