from __future__ import annotations

import json as json_module
import urllib.request

import pytest

from app.llm import DeepSeekClient, deepseek_base_url, fast_model, pro_model


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ["DEEPSEEK_BASE_URL", "FAST_MODEL", "PRO_MODEL", "DEEPSEEK_API_KEY"]:
        monkeypatch.delenv(key, raising=False)


def test_defaults_when_env_not_set(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)  # no .env file here, so load_dotenv() is a no-op
    assert deepseek_base_url() == "https://api.deepseek.com"
    assert fast_model() == "deepseek-v4-flash"
    assert pro_model() == "deepseek-v4-pro"


def test_env_vars_override_defaults(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://example.test")
    monkeypatch.setenv("FAST_MODEL", "custom-fast")
    monkeypatch.setenv("PRO_MODEL", "custom-pro")
    assert deepseek_base_url() == "https://example.test"
    assert fast_model() == "custom-fast"
    assert pro_model() == "custom-pro"


def test_json_chat_builds_endpoint_from_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, str] = {}

    class FakeResponse:
        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *args: object) -> bool:
            return False

        def read(self) -> bytes:
            return json_module.dumps({"choices": [{"message": {"content": '{"ok": true}'}}]}).encode()

    def fake_urlopen(request: urllib.request.Request, timeout: float | None = None) -> FakeResponse:
        captured["url"] = request.full_url
        return FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = DeepSeekClient(api_key="test-key", base_url="https://example.test/")

    result = client.json_chat("some-model", "system", "user")

    assert captured["url"] == "https://example.test/chat/completions"
    assert result == {"ok": True}
