from __future__ import annotations

import time
from dataclasses import dataclass

import requests
from loguru import logger


@dataclass(frozen=True)
class OllamaClient:
    base_url: str
    model: str
    timeout_seconds: int
    retry_times: int
    retry_delay_seconds: int

    def chat(self, prompt: str) -> str:
        url = self.base_url.rstrip("/") + "/api/chat"

        last_error: Exception | None = None
        for attempt in range(1, self.retry_times + 1):
            try:
                resp = requests.post(
                    url,
                    json={
                        "model": self.model,
                        "stream": False,
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
