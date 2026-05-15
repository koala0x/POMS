from __future__ import annotations

"""
LLM（Ollama）配置分组。

由 `config/_legacy.py` 重构而来——老链路（Level1Service / Level2Service）
已于 2026-05 淘汰，本文件只保留**仍在使用的** Ollama 相关字段：

- `ollama_base_url`：Ollama HTTP 地址
- `ollama_model_level5` / `ollama_timeout_level5`：Phase 2.7 BriefingService 用

未来 Phase 3 如果加更多 LLM 模型 / 任务，统一往本文件追加字段，便于集中管理。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class LLMSettings:
    # ==========================================================================
    # Ollama 服务（Phase 2.7+ 共享）
    # --------------------------------------------------------------------------
    # 服务端地址，BriefingService 通过它构造 OllamaClient 调用 LLM 生成简报。
    # ==========================================================================

    # Ollama 监听地址。格式 http://host:port，末尾不要加斜杠 / 路径
    # （client 自己拼 /api/chat）。
    # ★ 必须保证 Ollama 监听在 0.0.0.0 而非 127.0.0.1，否则跨机器访问会被拒绝。
    # 配置方式：在跑 Ollama 的机器上 `OLLAMA_HOST=0.0.0.0:11434 ollama serve`。
    ollama_base_url: str = "http://192.168.1.219:11434"

    # ----- 五次摘要（level5）：Phase 2.7 LLM 定向简报使用，每 15 分钟整点对齐 -----
    # qwen3:8b 实测（2026-05-14 prompt 工程）：5 entity 全合法 JSON，平均 30s/次，
    # Top-5 一轮 ~2.5 分钟。30b 模型质量更高但 CPU 推理 90s+，单轮 7.5 分钟，
    # 会拖累 worker 节奏；保持 8b 即可。
    ollama_model_level5: str = "qwen3:8b"

    # level5 单次请求超时（秒）。
    # 实测平均 30s，给 600s 留足余量（CPU 推理偶尔会因消息长拖到 60~90s）。
    ollama_timeout_level5: int = 600
