from __future__ import annotations

"""
Ollama HTTP 客户端封装。

目标:
- 业务层只关心"给 prompt -> 得到文本摘要",不需要理解 Ollama HTTP 细节
- 把超时、空内容判定等异常处理集中在一处

失败策略:
- 本地 Ollama 单线程推理,任何重试都只会把请求堆积在模型前面,
  反而让整条流水线堵死。所以本客户端**不做任何重试**:
  一次调用失败直接抛错,由上层 worker 跳过本轮,等下一次轮询再处理。
"""

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
    - enable_thinking: qwen3 系列默认会输出 <think>...</think> 推理链,
      让响应非常慢且 message.content 里夹杂大段思考。
      置 False 通过 Ollama API 的 `think` 字段关闭,响应速度提升数倍。
    - num_ctx: 模型上下文 token 数。Ollama 默认 4096,
      对一次摘要(50 条 prompt 约 3700 token)留给输出的空间太小,
      会触发 context shifting,生成速度急剧下降。
      默认 16384 足够 50 条输入 + 长结构化输出,且 8b 模型在 CPU 上的内存开销可控。
    - num_predict: 输出 token 数上限。-1 表示不限制,模型自然结束生成。
      本地模型场景默认放开,避免长摘要被截断。
    """

    base_url: str
    model: str
    timeout_seconds: int
    enable_thinking: bool = False
    num_ctx: int = 16384
    num_predict: int = -1

    def chat(self, prompt: str) -> str:
        """
        调用 Ollama 生成回复文本。

        请求体采用 messages 格式,便于未来升级为多轮对话(当前只用 user 单轮)。
        期望响应形态(Ollama 常见返回):
        {
          "message": { "role": "...", "content": "..." },
          ...
        }

        失败行为:任何异常(超时 / 连接错 / 5xx / JSON 解析失败 / 空内容)都
        **直接抛错、不重试**。重试只会让本地模型前的请求堆积,整条流水线卡死。
        上层 worker 捕获后跳过本轮,下次轮询到这一批数据再试一次即可。
        """
        url = self.base_url.rstrip("/") + "/api/chat"

        try:
            # stream=False:一次性返回完整文本,便于后续写库/标记幂等
            # think=False:关闭 qwen3 推理链,直接给答案,推理时长缩短数倍
            # num_ctx / num_predict:见 docstring,关键性能参数
            resp = requests.post(
                url,
                json={
                    "model": self.model,
                    "stream": False,
                    "think": self.enable_thinking,
                    "options": {
                        "num_ctx": self.num_ctx,
                        "num_predict": self.num_predict,
                    },
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
            # 空字符串也视为失败:避免写入无意义摘要并错误地标记为已处理
            if not content or not str(content).strip():
                raise ValueError("Ollama 返回空内容")
            return str(content).strip()
        except requests.exceptions.ReadTimeout as e:
            logger.error(
                "Ollama 推理超时({}s),跳过本轮。建议:减小 batch_size 或 num_predict",
                self.timeout_seconds,
            )
            raise RuntimeError(f"Ollama 推理超时({self.timeout_seconds}s)") from e
        except Exception as e:
            logger.error("Ollama 调用失败,跳过本轮:{}", e)
            raise RuntimeError(f"Ollama 调用失败:{e}") from e

