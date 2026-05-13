from __future__ import annotations

"""
TelegramClient 单元测试（Phase 2 Task 1.3，对应 requirements.md Req 1 / 6.1）。

测试策略：
- 用 monkeypatch 替换 `notifications.telegram_client.urllib.request.urlopen`，
  让它返回伪造的 response（含 .read / __enter__ / __exit__）或抛指定异常。
- 不真的发任何 HTTP 请求，CI 完全离线可跑。

覆盖 5 个用例（design.md §5 测试矩阵 telegram_client 部分）：
- test_send_text_200_ok               → True
- test_send_text_http_error_returns_false → False（HTTPError 401）
- test_send_text_network_error_returns_false → False（URLError）
- test_send_text_unexpected_error_returns_false → False（任意 Exception）
- test_send_text_truncates_long_message → 检查 payload 长度 ≤ 4000
"""

import json
import urllib.parse
from io import BytesIO
from urllib.error import HTTPError, URLError

import pytest

from notifications import telegram_client as tc_module
from notifications.telegram_client import TelegramClient


class _FakeResponse:
    """模拟 urllib.request.urlopen 返回的 response 对象（支持 with 语句 + read）。"""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self) -> bytes:
        return self._body


def _make_ok_response() -> _FakeResponse:
    return _FakeResponse(json.dumps({"ok": True, "result": {"message_id": 1}}).encode("utf-8"))


def test_send_text_200_ok(monkeypatch) -> None:
    """
    Req 1.3：HTTP 200 + ok=True → 返回 True。
    """
    captured: dict = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["data"] = req.data
        captured["timeout"] = timeout
        return _make_ok_response()

    monkeypatch.setattr(tc_module.urllib.request, "urlopen", fake_urlopen)

    client = TelegramClient(bot_token="TEST_TOKEN", chat_id="123456", timeout_seconds=5)
    assert client.send_text("hello world") is True

    # 校验请求参数
    assert "TEST_TOKEN/sendMessage" in captured["url"]
    parsed = urllib.parse.parse_qs(captured["data"].decode("utf-8"))
    assert parsed["chat_id"] == ["123456"]
    assert parsed["text"] == ["hello world"]
    assert captured["timeout"] == 5


def test_send_text_http_error_returns_false(monkeypatch) -> None:
    """
    Req 1.3：HTTP 4xx/5xx（typically Token 错误 / Bot 被拉黑）→ 返回 False，不抛异常。
    """
    def fake_urlopen(req, timeout=None):
        raise HTTPError(
            url=req.full_url,
            code=401,
            msg="Unauthorized",
            hdrs=None,  # type: ignore[arg-type]
            fp=BytesIO(b'{"ok":false,"error_code":401,"description":"Unauthorized"}'),
        )

    monkeypatch.setattr(tc_module.urllib.request, "urlopen", fake_urlopen)

    client = TelegramClient(bot_token="BAD_TOKEN", chat_id="123456")
    assert client.send_text("anything") is False


def test_send_text_network_error_returns_false(monkeypatch) -> None:
    """
    Req 1.3：网络层错误（DNS 失败、连接拒绝、超时）→ 返回 False，不抛异常。
    """
    def fake_urlopen(req, timeout=None):
        raise URLError("Name or service not known")

    monkeypatch.setattr(tc_module.urllib.request, "urlopen", fake_urlopen)

    client = TelegramClient(bot_token="TEST_TOKEN", chat_id="123456")
    assert client.send_text("anything") is False


def test_send_text_unexpected_error_returns_false(monkeypatch) -> None:
    """
    Req 1.3：意外异常（JSON 解析失败、库内部 RuntimeError 等）也不能抛出，
    必须返回 False。
    """
    def fake_urlopen(req, timeout=None):
        raise RuntimeError("simulated unexpected failure")

    monkeypatch.setattr(tc_module.urllib.request, "urlopen", fake_urlopen)

    client = TelegramClient(bot_token="TEST_TOKEN", chat_id="123456")
    assert client.send_text("anything") is False


def test_send_text_truncates_long_message(monkeypatch) -> None:
    """
    Telegram API 单条消息上限 4096 字符，TelegramClient 在超过 4000 时截断到
    3997 + "..."，避免发送时 API 报错。
    """
    captured: dict = {}

    def fake_urlopen(req, timeout=None):
        parsed = urllib.parse.parse_qs(req.data.decode("utf-8"))
        captured["text"] = parsed["text"][0]
        return _make_ok_response()

    monkeypatch.setattr(tc_module.urllib.request, "urlopen", fake_urlopen)

    client = TelegramClient(bot_token="TEST_TOKEN", chat_id="123456")
    long_text = "A" * 5000
    assert client.send_text(long_text) is True

    sent = captured["text"]
    assert len(sent) <= 4000, f"截断后长度应 ≤ 4000，实际 {len(sent)}"
    assert sent.endswith("...")
    # 头部仍是原文
    assert sent[:10] == "A" * 10
