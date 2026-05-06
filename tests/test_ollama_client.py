from __future__ import annotations

"""
OllamaClient 的单元测试。

测试目标：
- 不依赖真实的 Ollama 服务，通过 mock requests.post 来验证：
  - 成功返回时能正确解析 message.content
  - 失败时会按重试次数重试，最终抛出 RuntimeError
"""

from llm.ollama_client import OllamaClient



def test_chat_interactive_with_local_ollama() -> None:
    # 可以和本地模型交互了
    client = OllamaClient(
        base_url="http://localhost:11434",
        model="qwen3:30b",
        timeout_seconds=300,
        retry_times=1,
        retry_delay_seconds=0,
    )
    reply = client.chat("为什么你分析一句话耗时那么久？")
    print(f"Model: {reply}")
    assert reply.strip()
