from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

from llm.ollama_client import OllamaClient


def test_chat_success() -> None:
    client = OllamaClient(
        base_url="http://localhost:11434",
        model="qwen3:30b",
        timeout_seconds=1,
        retry_times=1,
        retry_delay_seconds=0,
    )

    mock_resp = Mock()
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = {"message": {"content": "ok"}}

    with patch("requests.post", return_value=mock_resp) as post:
        out = client.chat("hi")

    assert out == "ok"
    post.assert_called_once()


def test_chat_retries_then_fails() -> None:
    client = OllamaClient(
        base_url="http://localhost:11434",
        model="qwen3:30b",
        timeout_seconds=1,
        retry_times=2,
        retry_delay_seconds=0,
    )

    with patch("requests.post", side_effect=RuntimeError("boom")):
        with pytest.raises(RuntimeError):
            client.chat("hi")
