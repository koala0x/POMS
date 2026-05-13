from __future__ import annotations

"""
Telegram Bot 推送客户端（Phase 2 Task 2.2 新增）。

只做一件事：把一段文本通过 Bot API `sendMessage` 推到指定 chat_id。
零新依赖（用标准库 urllib.request），不抛异常给调用方——任何错误（HTTP
4xx/5xx、网络超时、API ok=False、Bot Token 错误）一律记 ERROR 日志后
返回 False，让 AlertTriggerService 这一轮跳过该 entity，下一轮再试。

调用频率约束：
- 单实体每小时最多 1 次（智能冷却）；最坏情况单轮榜单全炸也不超过 Top-K 次
- 远低于 Telegram 的 30 msg/s 限制，不需要专门限流

为什么不用 requests / httpx：
- 标准库自带，零新依赖（requirements.md 硬约束）
- 调用频率极低（< 1 req/s），不需要连接池
- 简单到 50 行内能搞定，便于 mock 测试
"""

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from urllib.error import HTTPError, URLError

from loguru import logger

# Telegram 单条消息长度上限（API 强制 4096，留 96 字符余量给 truncate 标记）
_TELEGRAM_MAX_TEXT_LENGTH = 4000
_TELEGRAM_TRUNCATE_TAIL = "..."


@dataclass(frozen=True)
class TelegramClient:
    """
    Telegram Bot 客户端。

    构造时给两个必填参数（bot_token / chat_id），可选 timeout_seconds
    （默认 10s，Telegram API 一般 < 1s 响应，10s 足够防卡死）。

    使用：
        client = TelegramClient(bot_token="123:abc", chat_id="6789012345")
        ok = client.send_text("hello world")  # True or False，绝不抛异常
    """

    bot_token: str
    chat_id: str
    timeout_seconds: int = 10

    def send_text(self, text: str, *, parse_mode: str | None = None) -> bool:
        """
        发送纯文本消息。

        参数：
            text: 消息正文。超过 4000 字符自动截断到 3997 + "..."（Telegram
                  API 上限 4096，留余量给截断标记和 emoji 多字节字符）。
            parse_mode: None / "Markdown" / "HTML"。Phase 2 起步全用 None
                  纯文本，避免特殊字符转义麻烦。

        返回：
            True  推送成功（HTTP 200 + API ok=True）
            False 任何异常（网络错误、HTTP 4xx/5xx、API ok=False、Token 错误等）

        约定：
            **绝不抛异常给调用方**，所有错误都 log.error 后返回 False。
        """
        # 配置缺失即拒绝（main.py 已经会跳过构造，这里是双保险）
        if not self.bot_token or not self.chat_id:
            logger.error("telegram send failed: bot_token / chat_id 未配置")
            return False

        # 截断超长消息
        if len(text) > _TELEGRAM_MAX_TEXT_LENGTH:
            head_len = _TELEGRAM_MAX_TEXT_LENGTH - len(_TELEGRAM_TRUNCATE_TAIL)
            text = text[:head_len] + _TELEGRAM_TRUNCATE_TAIL

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload: dict[str, str] = {
            "chat_id": self.chat_id,
            "text": text,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode

        data = urllib.parse.urlencode(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")

        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                body = resp.read().decode("utf-8")
                result = json.loads(body)
                if result.get("ok") is True:
                    return True
                # API 返回 200 但 ok=False（典型场景：chat_id 错误、被拉黑）
                logger.error(
                    "telegram api 返回非 ok: description={!r} error_code={}",
                    result.get("description"),
                    result.get("error_code"),
                )
                return False
        except HTTPError as e:
            # 4xx / 5xx：Token 错误、Bot 被踢出群、API 限流等
            logger.error("telegram http error: {} {}", e.code, e.reason)
            return False
        except URLError as e:
            # 网络层错误：DNS 失败、连接拒绝、超时等
            logger.error("telegram network error: {}", e.reason)
            return False
        except Exception as e:
            # 兜底：JSON 解析失败、unicode 编码失败等小概率事件
            logger.error("telegram unexpected error: {}", e)
            return False
