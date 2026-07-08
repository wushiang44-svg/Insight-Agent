from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_FAST_MODEL = "deepseek-v4-flash"
DEFAULT_PRO_MODEL = "deepseek-v4-pro"


def load_dotenv(path: str | Path = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def deepseek_api_key() -> str:
    load_dotenv()
    return os.environ.get("DEEPSEEK_API_KEY", "")


def deepseek_base_url() -> str:
    load_dotenv()
    return os.environ.get("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL)


def fast_model() -> str:
    """Cheap/quick model used for search planning, item analysis, and sufficiency checks."""
    load_dotenv()
    return os.environ.get("FAST_MODEL", DEFAULT_FAST_MODEL)


def pro_model() -> str:
    """Stronger model used only for the final merchant report, where quality matters most."""
    load_dotenv()
    return os.environ.get("PRO_MODEL", DEFAULT_PRO_MODEL)


class DeepSeekClient:
    def __init__(self, api_key: str | None = None, base_url: str | None = None, timeout_seconds: int = 45):
        self.api_key = api_key if api_key is not None else deepseek_api_key()
        self.base_url = base_url if base_url is not None else deepseek_base_url()
        self.timeout_seconds = timeout_seconds

    def available(self) -> bool:
        return bool(self.api_key)

    def json_chat(self, model: str, system: str, user: str) -> dict[str, Any]:
        if not self.api_key:
            raise RuntimeError("DEEPSEEK_API_KEY is not configured")
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }
        request = urllib.request.Request(
            f"{self.base_url.rstrip('/')}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"DeepSeek API HTTP {exc.code}: {detail}") from exc
        content = body["choices"][0]["message"]["content"]
        return parse_json_object(content)


def parse_json_object(text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            raise
        parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("LLM response must be a JSON object")
    return parsed
