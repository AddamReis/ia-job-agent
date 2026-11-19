import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.llm.prompts import extraction_messages

URL = "http://10.10.10.2:8011/v1/chat/completions"
HEADERS = {"Authorization": "Bearer vllm"}


def call(label, messages, max_tokens, timeout=60, no_think=False):
    payload = {
        "model": "qwen3.8-27b-nvfp4",
        "messages": messages,
        "temperature": 0.1,
    }
    if max_tokens:
        payload["max_tokens"] = max_tokens
    if no_think:
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    t = time.time()
    try:
        response = httpx.post(URL, headers=HEADERS, json=payload, timeout=timeout)
        data = response.json()
        content = data["choices"][0]["message"]["content"]
        print(label, "secs", round(time.time() - t, 1), "status", response.status_code, "len", len(content), repr(content[:80]), flush=True)
    except Exception as exc:
        print(label, "FAILED", round(time.time() - t, 1), type(exc).__name__, str(exc)[:120], flush=True)


call("simple-32", [{"role": "user", "content": "Responda apenas: OK"}], 32)
call("simple-512", [{"role": "user", "content": "Responda apenas: OK"}], 512)
call("long-512", extraction_messages("Senior Backend Engineer", "ACME", "Requires C#, .NET 8, AWS, SQL Server, microservices. Senior, 7+ years. Salary 120000-150000 USD. Remote US, English required."), 512)
call("long-512-nothink", extraction_messages("Senior Backend Engineer", "ACME", "Requires C#, .NET 8, AWS, SQL Server, microservices. Senior, 7+ years. Salary 120000-150000 USD. Remote US, English required."), 512, no_think=True)
