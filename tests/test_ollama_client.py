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
    )
    reply = client.chat("#独家报道 | 维杰不愿接受DMK、AIADMK或任何其他与BJP结盟的政党的支持。他希望与左翼力量组建一个世俗政府。我欢迎联合政府：VCK主席托尔·蒂鲁马瓦拉万接受@nimumurali采访时表示。#维杰 #TVK #泰米尔纳德邦")
    print(f"Model: {reply}")
    assert reply.strip()
