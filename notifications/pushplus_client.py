from __future__ import annotations

"""
PushPlus 微信推送客户端。

PushPlus（https://www.pushplus.plus）是一个第三方消息推送服务，
通过微信公众号把消息推到你的微信。免费版 200 条/天，足够本项目使用。

使用方式：
1. 微信扫码关注 PushPlus 公众号
2. 登录 https://www.pushplus.plus 获取 token
3. 把 token 填到 config/_alerts.py 的 pushplus_token 字段
4. 重启服务即可

API 文档：https://www.pushplus.plus/doc/guide/api.html
- POST https://www.pushplus.plus/send
- Content-Type: application/json
- Body: {"token": "...", "title": "...", "content": "...", "template": "markdown"}
- 返回: {"code": 200, "msg": "请求成功", "data": "..."}

设计契约（与 TelegramClient 对齐）：
- send_text 绝不抛异常给调用方
- 任何错误 log.error 后返回 False
- 成功返回 True
"""

import json
import urllib.request
from dataclasses import dataclass
from urllib.error import HTTPError, URLError

from loguru import logger

# PushPlus 单条消息内容上限（官方文档未明确，经验值 10000 字符安全）
_PUSHPLUS_MAX_CONTENT_LENGTH = 10000
_PUSHPLUS_TRUNCATE_TAIL = "\n...(内容过长已截断)"

# PushPlus API 地址
_PUSHPLUS_API_URL = "https://www.pushplus.plus/send"


@dataclass(frozen=True)
class PushPlusClient:
    """
    PushPlus 微信推送客户端。

    构造时给 token（必填），可选 timeout_seconds。
    接口契约与 TelegramClient 完全一致：send_text(text) → bool。

    使用：
        client = PushPlusClient(token="your_pushplus_token")
        ok = client.send_text("hello world")  # True or False，绝不抛异常
    """

    token: str
    timeout_seconds: int = 10
    # 消息模板：markdown / html / txt / json。推荐 markdown，微信端渲染效果好
    template: str = "txt"

    def send_text(self, text: str, *, title: str = "PomsAI 通知", **kwargs) -> bool:
        """
        发送消息到微信。

        参数：
            text: 消息正文（支持 Markdown 格式）。超长自动截断。
            title: 消息标题（微信通知栏显示）。
            **kwargs: 兼容其他 client 的参数（如 parse_mode），静默忽略。

        返回：
            True  推送成功（API code=200）
            False 任何异常

        约定：
            **绝不抛异常给调用方**，所有错误都 log.error 后返回 False。
        """
        if not self.token:
            logger.error("pushplus send failed: token 未配置")
            return False

        # 截断超长消息
        if len(text) > _PUSHPLUS_MAX_CONTENT_LENGTH:
            head_len = _PUSHPLUS_MAX_CONTENT_LENGTH - len(_PUSHPLUS_TRUNCATE_TAIL)
            text = text[:head_len] + _PUSHPLUS_TRUNCATE_TAIL

        payload = json.dumps(
            {
                "token": self.token,
                "title": title,
                "content": text,
                "template": self.template,
            }
        ).encode("utf-8")

        req = urllib.request.Request(
            _PUSHPLUS_API_URL,
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json"},
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                body = resp.read().decode("utf-8")
                result = json.loads(body)
                if result.get("code") == 200:
                    return True
                # API 返回非 200（token 错误、频率限制等）
                logger.error(
                    "pushplus api 返回非 200: code={} msg={!r}",
                    result.get("code"),
                    result.get("msg"),
                )
                return False
        except HTTPError as e:
            logger.error("pushplus http error: {} {}", e.code, e.reason)
            return False
        except URLError as e:
            logger.error("pushplus network error: {}", e.reason)
            return False
        except Exception as e:
            logger.error("pushplus unexpected error: {}", e)
            return False
