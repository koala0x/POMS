from __future__ import annotations

"""
多渠道推送客户端：把消息同时发到多个通道（Telegram + PushPlus + 未来更多）。

对外接口与 TelegramClient 完全一致（send_text → bool），
让 AlertTriggerService / DigestPusherService 不需要知道有几个通道——
它们只调一个 client.send_text()，MultiClient 内部帮你广播。

设计契约：
- 任一通道成功 → 返回 True（至少有一个渠道收到了）
- 全部失败 → 返回 False
- 单个通道失败不影响其他通道（互相隔离）
"""

from dataclasses import dataclass, field
from typing import Any, Protocol

from loguru import logger


class _PushClient(Protocol):
    """推送客户端的最小接口契约。"""

    def send_text(self, text: str, **kwargs: Any) -> bool: ...


@dataclass
class MultiClient:
    """
    多渠道广播客户端。

    构造时传入一组 client（都实现 send_text(text, **kwargs) → bool）。
    调用 send_text 时逐个调用，任一成功即返回 True。

    使用：
        multi = MultiClient(clients=[telegram_client, pushplus_client])
        multi.send_text("hello")  # 同时推 Telegram + 微信
    """

    clients: list[Any] = field(default_factory=list)

    def send_text(self, text: str, **kwargs: Any) -> bool:
        """
        广播消息到所有通道。

        - 逐个调用，互相隔离（一个失败不影响其他）
        - 任一成功 → True
        - 全部失败 → False
        """
        if not self.clients:
            logger.error("multi_client: 没有配置任何推送通道")
            return False

        any_ok = False
        for client in self.clients:
            try:
                ok = client.send_text(text, **kwargs)
                if ok:
                    any_ok = True
            except Exception as e:
                # 单通道异常不传播
                logger.error(
                    "multi_client: {} 推送异常: {}",
                    type(client).__name__,
                    e,
                )
        return any_ok
