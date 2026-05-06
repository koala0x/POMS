from __future__ import annotations

"""
Ollama HTTP 客户端封装。

目标：
- 业务层只关心“给 prompt -> 得到文本摘要”，不需要理解 Ollama HTTP 细节
- 把超时、重试、空内容判定等异常处理集中在一处
"""

import time
from dataclasses import dataclass

import requests
from loguru import logger


@dataclass(frozen=True)
class OllamaClient:
    """
    Ollama /api/chat 的最小封装。

    - base_url: 例如 http://localhost:11434
    - model: 例如 qwen3:30b
    - timeout_seconds: 单次请求超时(模型推理慢时需要更大)
    - retry_times / retry_delay_seconds: 失败重试策略(网络/服务抖动时更稳)
    - enable_thinking: qwen3 系列默认会输出 <think>...</think> 推理链,
      让响应非常慢且 message.content 里夹杂大段思考。
      置 False 通过 Ollama API 的 `think` 字段关闭,响应速度提升数倍。
    """

    base_url: str
    model: str
    timeout_seconds: int
    retry_times: int
    retry_delay_seconds: int
    enable_thinking: bool = False

    def chat(self, prompt: str) -> str:
        """
        调用 Ollama 生成回复文本。

        请求体采用 messages 格式,便于未来升级为多轮对话(当前只用 user 单轮)。
        期望响应形态(Ollama 常见返回):
        {
          "message": { "role": "...", "content": "..." },
          ...
        }
        """
        url = self.base_url.rstrip("/") + "/api/chat"

        last_error: Exception | None = None
        for attempt in range(1, self.retry_times + 1):
            try:
                # stream=False:一次性返回完整文本,便于后续写库/标记幂等
                # think=False:关闭 qwen3 推理链,直接给答案,推理时长缩短数倍
                resp = requests.post(
                    url,
                    json={
                        "model": self.model,
                        "stream": False,
                        "think": self.enable_thinking,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                    timeout=self.timeout_seconds,
                )
                resp.raise_for_status()
                data = resp.json()
                content = (
                    data.get("message", {}).get("content")
                    if isinstance(data, dict)
                    else None
                )
                # 空字符串也视为失败：避免写入无意义摘要并错误地标记为已处理
                if not content or not str(content).strip():
                    raise ValueError("Ollama 返回空内容")
                return str(content).strip()
            except Exception as e:
                last_error = e
                if attempt < self.retry_times:
                    logger.warning(
                        "Ollama 调用失败，准备重试（{}/{}）：{}",
                        attempt,
                        self.retry_times,
                        e,
                    )
                    time.sleep(self.retry_delay_seconds)
                else:
                    logger.error("Ollama 调用失败（{}/{}）：{}", attempt, self.retry_times, e)

        raise RuntimeError(f"Ollama 调用失败：{last_error}") from last_error
