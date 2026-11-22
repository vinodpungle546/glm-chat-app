#!/usr/bin/env python3
"""Terminal chat with GLM-5.3-Flash via the Globant GAIA API.

Credentials are read from the environment:
  - CODA_GEAI_BASE_URL (default: https://api.clients.globant.com)
  - CODA_GEAI_API_KEY

Usage:
    python3 chat.py

Commands inside the chat:
    /reset  - clear conversation history
    /exit   - quit (Ctrl+D also works)
"""

import json
import os
import sys

import requests

BASE_URL = os.environ.get("CODA_GEAI_BASE_URL", "https://api.clients.globant.com").rstrip("/")
API_KEY = os.environ.get("CODA_GEAI_API_KEY", "")
MODEL = os.environ.get("GLM_MODEL", "globant_dgx/GLM-5.3-Flash")
ENDPOINT = f"{BASE_URL}/v1/chat/completions"

SYSTEM_PROMPT = "You are a helpful assistant. Answer concisely."


def stream_response(messages: list[dict]) -> str:
    """Send the conversation to the model, streaming the reply to the terminal."""
    payload = {
        "model": MODEL,
        "messages": messages,
        "stream": True,
    }
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }

    reply_parts: list[str] = []
    with requests.post(ENDPOINT, headers=headers, json=payload, stream=True, timeout=120) as response:
        response.raise_for_status()
        for line in response.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data: "):
                continue
            data = line[len("data: "):]
            if data.strip() == "[DONE]":
                break
            chunk = json.loads(data)
            delta = chunk.get("choices", [{}])[0].get("delta", {}).get("content")
            if delta:
                print(delta, end="", flush=True)
                reply_parts.append(delta)

    print()  # newline after streamed reply
    return "".join(reply_parts)


def main() -> None:
    """Runs the terminal chat loop: reads a line, sends the whole conversation to the model, prints the streamed
    reply. /exit quits and /reset clears the history.
    """
    if not API_KEY:
        sys.exit("Error: CODA_GEAI_API_KEY is not set in the environment.")

    print(f"Chat with {MODEL} (type /exit to quit, /reset to clear history)")
    print("-" * 60)

    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]

    while True:
        try:
            user_input = input("\nYou > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye!")
            break

        if not user_input:
            continue
        if user_input == "/exit":
            print("Bye!")
            break
        if user_input == "/reset":
            messages = [{"role": "system", "content": SYSTEM_PROMPT}]
            print("Conversation history cleared.")
            continue

        messages.append({"role": "user", "content": user_input})

        print("GLM  > ", end="", flush=True)
        try:
            reply = stream_response(messages)
        except requests.RequestException as exc:
            print(f"\n[request failed: {exc}]")
            messages.pop()  # drop the failed turn so history stays consistent
            continue

        messages.append({"role": "assistant", "content": reply})


if __name__ == "__main__":
    main()
