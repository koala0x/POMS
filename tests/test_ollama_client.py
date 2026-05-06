from __future__ import annotations

"""
OllamaClient 的单元测试。

测试目标：
- 不依赖真实的 Ollama 服务，通过 mock requests.post 来验证：
  - 成功返回时能正确解析 message.content
  - 失败时会按重试次数重试，最终抛出 RuntimeError
"""

from unittest.mock import Mock, patch

import pytest

from llm.ollama_client import OllamaClient


def test_chat_success() -> None:
    # 单次成功：retry_times=1，避免测试中出现额外重试分支。
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

    # 用 mock 的 HTTP 响应替代真实网络请求。
    with patch("requests.post", return_value=mock_resp) as post:
        out = client.chat("hi")

    # 期望拿到 message.content，并且只调用一次 requests.post。
    assert out == "ok"
    post.assert_called_once()


def test_chat_retries_then_fails() -> None:
    # 固定重试次数为 2，并把 retry_delay_seconds=0，避免测试变慢。
    client = OllamaClient(
        base_url="http://localhost:11434",
        model="qwen3:30b",
        timeout_seconds=1,
        retry_times=2,
        retry_delay_seconds=0,
    )

    # requests.post 持续抛异常，最终应由 OllamaClient 抛 RuntimeError。
    with patch("requests.post", side_effect=RuntimeError("boom")):
        with pytest.raises(RuntimeError):
            client.chat("hi")
