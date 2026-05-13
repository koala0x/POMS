from __future__ import annotations

"""
**老链路**配置（Level1Service / Level2Service + Ollama）。

只在 `disable_legacy_pipeline=False` 时被读取；当前默认设为 True，
这一组实际上是"备用"。

修改这里的字段不会影响新链路（normalizer / entity_extractor / hotness）。

如果未来彻底删除老链路（见 docs/rollback_plan.md 的反向操作），
直接删掉本文件 + Settings 多继承里去掉 LegacySettings 即可。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class LegacySettings:
    # ==========================================================================
    # Ollama 服务（仅老链路使用）
    # --------------------------------------------------------------------------
    # 服务端地址 + 两级摘要各自的模型/超时配置。
    # level1 频率高、上下文短，建议用轻量模型；
    # level2 频率低、上下文长、质量要求高，可以选大模型。
    # ==========================================================================

    # Ollama 监听地址。格式 http://host:port，末尾不要加斜杠 / 路径
    # （client 自己拼 /api/chat）。
    ollama_base_url: str = "http://192.168.1.219:11434"

    # ----- 一次摘要（level1）：高频调用 -----

    # level1 用的模型名，必须是 Ollama 已经 `ollama pull` 过的 tag。
    # 默认 qwen3:8b，在 CPU 推理场景下兼顾速度与质量。
    ollama_model_level1: str = "qwen3:8b"

    # level1 单次请求超时（秒）。超时后 Ollama 后端可能仍在生成，
    # 客户端不会自动重试（见 llm/ollama_client.py），只抛错等下一轮。
    # 本地 CPU 推理慢，默认给到 600s；推理卡可以调低。
    ollama_timeout_level1: int = 600

    # ----- 二次摘要（level2）：低频、对质量要求更高 -----

    # level2 用的模型名。想上大模型可以改成 "qwen3:30b" 这种，
    # level1 走小模型 / level2 走大模型，worker 串行执行避免频繁 swap。
    # 默认与 level1 同款是为了开箱即用。
    # ollama_model_level2: str = "qwen3:30b"
    ollama_model_level2: str = "qwen3:8b"

    # level2 单次请求超时（秒）。语义同 ollama_timeout_level1。
    ollama_timeout_level2: int = 600

    # 注：本地 Ollama 单线程推理，失败重试只会堵死模型且让请求堆积，
    # 所以客户端不做任何重试——失败直接抛错，等下一轮 worker 轮询自然重跑。

    # ==========================================================================
    # 业务参数（老链路触发阈值）
    # ==========================================================================

    # 一次摘要（level1）的批大小，同时也是触发阈值。
    # 语义：某个 source 的原始表里 is_summarized=FALSE 的条数 ≥ batch_size 才触发 LLM，
    # 触发后按 created_at 升序取最早的 batch_size 条喂给 LLM。
    # 调大：LLM 调用更少 / 每条上下文更长 / 摘要产出更慢；
    # 调小：反之。20 是 qwen3:8b 在单轮 prompt 里的安全上限。
    batch_size: int = 20

    # 二次摘要（level2）的触发阈值。
    # 语义：summary_level1 里某个 source 未做二次摘要（is_summarized_l2=FALSE）的条数
    # ≥ level2_threshold 才触发。与 level1 共用同一个 worker 串行执行。
    # 默认 5 表示每 5 次一次摘要就汇总一次；调大可以让 level2 看到更长的时间窗。
    level2_threshold: int = 5
