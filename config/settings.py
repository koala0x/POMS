from __future__ import annotations

"""
配置中心。

所有运行时配置直接写在这里,**不依赖外部 .env 文件**。
- 改配置:直接修改本文件 Settings 类里的字段默认值
- 新增配置:在 Settings 里加字段并填默认值,使用方 get_settings() 读取

本服务只做"读原始表 → 调 LLM → 写摘要表"的 AI 数据清洗,
HTTP 接入 / Twitter 抓取等配置已随对应代码移出本仓库。
"""

from dataclasses import dataclass, field
from functools import lru_cache
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Settings:
    """
    服务运行所需的全部配置项。

    配置分为五组:
    1. PostgreSQL 连接信息
    2. Ollama 服务地址 + level1/level2 两套模型参数
    3. 业务参数(轮询间隔、批大小、二次摘要阈值)
    4. 日志路径与保留策略
    5. 业务时区

    约定:
    - 所有时间计算以 timezone 作为"业务时区",写入数据库时保留 tz 信息
    - 一次摘要(level1)与二次摘要(level2)使用各自独立的 Ollama 配置,
      这样可以给低频高质量的 level2 单独配大模型/更长 timeout
    - level1 / level2 共用同一个 worker 串行循环触发(避免 Ollama 同时加载
      多个模型导致反复 swap),触发条件分别由 batch_size 与 level2_threshold 控制
    """

    # ==========================================================================
    # 1. PostgreSQL 连接信息
    # --------------------------------------------------------------------------
    # 本服务不建表,只读原始表 + 读写两张 summary 表。
    # 表结构由上游 API/迁移服务维护,字段定义可参考 db/models.py。
    # ==========================================================================

    # DB 所在主机。可以是域名、IP 或容器名。
    db_host: str = "192.168.1.219"
    # PostgreSQL 端口,默认 5432。
    db_port: int = 5432
    # 业务库名,应与上游 API 服务使用同一个库(共享原始表)。
    db_name: str = "all_new"
    # 连接用户名。最小权限需要:SELECT / UPDATE 原始表 + 全权限 summary_level1 / summary_level2。
    db_user: str = "all_new"
    # 连接密码。生产环境建议改成从环境变量或密钥管理系统读取,当前版本为了简单直接写明文。
    db_password: str = "123qwe"

    # ==========================================================================
    # 2. Ollama 服务
    # --------------------------------------------------------------------------
    # 服务端地址 + 两级摘要各自的模型/超时/重试配置。
    # level1 频率高、上下文短,建议用轻量模型;
    # level2 频率低、上下文长、质量要求高,可以选大模型。
    # ==========================================================================

    # Ollama 监听地址。格式: http://host:port,末尾不要加斜杠 / 路径(client 自己拼 /api/chat)。
    ollama_base_url: str = "http://192.168.1.219:11434"

    # ------ 一次摘要(level1):高频调用,每 poll_interval_seconds 触发一次 ------

    # level1 用的模型名,必须是 Ollama 已经 `ollama pull` 过的 tag。
    # 默认 qwen3:8b,在 CPU 推理场景下兼顾速度与质量。
    ollama_model_level1: str = "qwen3:8b"
    # level1 单次请求超时(秒)。超时后 Ollama 后端可能仍在生成,
    # 客户端不会自动重试(见 llm/ollama_client.py),只抛错等下一轮。
    # 本地 CPU 推理慢,默认给到 600s;推理卡可以调低。
    ollama_timeout_level1: int = 600

    # ------ 二次摘要(level2):低频、对质量要求更高 ------

    # level2 用的模型名。想上大模型时可以改成例如 "qwen3:30b",
    # 这样 level1 走小模型、level2 走大模型,worker 串行执行避免频繁 swap。
    # 默认与 level1 同款是为了开箱即用。
    # ollama_model_level2: str = "qwen3:30b"
    ollama_model_level2: str = "qwen3:8b"
    # level2 单次请求超时(秒)。语义同 ollama_timeout_level1。
    ollama_timeout_level2: int = 600

    # ------ 重试策略(level1 / level2 共用) ------

    # 单次 LLM 调用的最大尝试次数(含首次)。
    # ReadTimeout 不走重试(Ollama 后端仍可能在生成,重试只会让请求堆积);
    # 其它异常(JSON 解析失败 / 连接错 / 5xx)会按此次数重试。
    # 本地 Ollama 基本没网络抖动,失败多半是模型 / 输入问题,
    # 默认 1 = 只跑一次不重试,避免 CPU 雪上加霜。
    ollama_retry_times: int = 1
    # 每次重试之间的固定间隔(秒)。retry_times=1 时此配置无作用。
    ollama_retry_delay_seconds: int = 0

    # ==========================================================================
    # 3. 业务参数
    # --------------------------------------------------------------------------
    # 控制 worker 何时触发 level1 / level2、空闲时多久轮询一次。
    # ==========================================================================

    # worker 空闲轮询间隔(秒)。
    # 当 level1 / level2 所有 service 都"数据不足无事可做"时,worker sleep 这么久再查一遍;
    # 只要本轮有任一 service 真处理了数据,就**立刻**进下一轮不 sleep(把积压尽快消化)。
    poll_interval_seconds: int = 30

    # 一次摘要(level1)的批大小,同时也是触发阈值。
    # 语义:某个 source 的原始表里 is_summarized=FALSE 的条数 ≥ batch_size 才触发 LLM,
    # 触发后按 created_at 升序取最早的 batch_size 条喂给 LLM。
    # 调大:LLM 调用更少 / 每条上下文更长 / 摘要产出更慢;
    # 调小:反之。20 是 qwen3:8b 在单轮 prompt 里的安全上限。
    batch_size: int = 20

    # 二次摘要(level2)的触发阈值。
    # 语义:summary_level1 里某个 source 未做二次摘要(is_summarized_l2=FALSE)的条数
    # ≥ level2_threshold 才触发。与 level1 共用同一个 worker 串行执行。
    # 默认 5 表示每 5 次一次摘要就汇总一次;调大可以让 level2 看到更长的时间窗。
    level2_threshold: int = 5

    # ==========================================================================
    # 4. 日志
    # --------------------------------------------------------------------------
    # loguru 负责控制台 + 文件双输出,文件按天滚动。
    # ==========================================================================

    # 日志文件路径。启动时会自动创建上级目录。
    # 相对路径基于进程启动时的 cwd,建议部署时改成绝对路径避免歧义。
    log_path: str = "./logs/service.log"

    # 日志文件保留天数。按天滚动(rotation="00:00"),超过保留期的旧文件会被自动清理。
    log_retention_days: int = 30

    # ==========================================================================
    # 5. 业务时区
    # --------------------------------------------------------------------------
    # 写入 TIMESTAMPTZ 字段时统一带 tz,避免 PG 端做隐式转换。
    # ==========================================================================

    # 业务时区。影响:
    # - summary_level1.created_at / summary_level2.created_at 写入时使用的 tz
    # - summary_level2.period_start / period_end 同上
    # 写库字段是 TIMESTAMPTZ,设成 UTC 以外的 tz 只影响显示,不影响存储语义。
    # frozen dataclass 里可变默认必须用 default_factory。
    timezone: ZoneInfo = field(default_factory=lambda: ZoneInfo("UTC"))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    返回单例 Settings。

    通过 lru_cache 保证整个进程只构造一次,避免重复实例化。
    测试中如需修改配置,可以在调用 get_settings 之前 monkeypatch Settings 的字段默认值,
    或者直接 get_settings.cache_clear() 后重建实例。
    """
    return Settings()
