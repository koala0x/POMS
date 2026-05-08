from __future__ import annotations

"""
配置中心。

所有运行时配置直接写在这里,**不依赖外部 .env 文件**。
- 改配置:直接修改本文件 Settings 类里的字段默认值
- 新增配置:在 Settings 里加字段并填默认值,使用方 get_settings() 读取
"""

from dataclasses import dataclass, field
from functools import lru_cache
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Settings:
    """
    服务运行所需的全部配置项。

    约定:
    - 所有时间计算以 timezone 作为"业务时区",写入数据库时保留 tz 信息
    - DB_* 用于连接 PostgreSQL
    - OLLAMA_* 用于调用本地 Ollama 的 /api/chat 接口
    - 一次摘要(level1)与二次摘要(level2)使用各自的 Ollama 配置:
      level1 高频调用,推荐轻量模型;level2 频率较低,可上大模型
    - level1 / level2 共用同一个 worker 串行循环触发(避免 Ollama 上多个模型 swap),
      触发条件分别由 batch_size 与 level2_threshold 控制
    """

    # ------------------------------ PostgreSQL ------------------------------
    db_host: str = "127.0.0.1"
    db_port: int = 5432
    db_name: str = "all_new"
    db_user: str = "all_new"
    db_password: str = "123qwe"

    # ------------------------------ Ollama 服务 ------------------------------
    ollama_base_url: str = "http://localhost:11434"

    # 一次摘要(每 30s 触发,频繁) → 推荐轻量模型
    ollama_model_level1: str = "qwen3:8b"
    ollama_timeout_level1: int = 600

    # 二次摘要(频率较低,可上大模型)
    # ollama_model_level2: str = "qwen3:30b"
    ollama_model_level2: str = "qwen3:8b"
    ollama_timeout_level2: int = 600

    # 重试策略(网络/解析类异常时使用,ReadTimeout 不重试)
    # 本地 Ollama 不会有网络抖动,失败基本是模型/输入问题,重试只会让 CPU 雪上加霜,
    # 因此默认 retry_times=1(只跑一次,不重试),delay=0。
    ollama_retry_times: int = 1
    ollama_retry_delay_seconds: int = 0

    # ------------------------------ 业务参数 --------------------------------
    poll_interval_seconds: int = 30   # worker 空闲时的轮询间隔(level1+level2 都没数据可处理时)
    batch_size: int = 20              # 一次摘要的批大小
    # 二次摘要触发阈值:summary_level1 累计未二次摘要 ≥ 该值即触发。
    # 与 level1 串行在同一个 worker 里执行,避免 Ollama 上多个模型来回 swap。
    level2_threshold: int = 5

    # ------------------------------ 日志 -----------------------------------
    log_path: str = "./logs/service.log"
    log_retention_days: int = 30

    # ------------------------------ HTTP 接入 -------------------------------
    # api_main.py 启动的数据接入服务监听地址。0.0.0.0 表示对外暴露,
    # 内网/本机部署可改成 127.0.0.1 仅限本地访问。
    api_host: str = "0.0.0.0"
    api_port: int = 18089

    # ------------------------------ Twitter List 抓取 -----------------------
    # api_main.py 启动后,后台线程会按 twitter_list_fetch_interval_seconds 周期
    # 调 twitterapi.io 拉某个 List 的推文,经 IngestService 直接写入 twitter_posts。
    # 启动时立即跑首轮,之后每 interval 一次。
    #
    # twitterapi_io_key:在 https://twitterapi.io/dashboard 拿到的 API Key。
    #   占位是 "YOUR_API_KEY",**部署前请替换为真实 key**;占位值时 fetcher 自动跳过。
    # twitter_list_id:目标 List 的数字 ID,例如 https://x.com/i/lists/<id> 的 <id>。
    # twitter_list_max_pages:单轮最多翻多少页(每页 ~20 条),控制单次成本上限。
    # twitter_list_fetch_interval_seconds:轮询间隔,默认 1800(30 分钟)。
    twitterapi_io_key: str = "new1_3c489f5d8d7d497f8fd6f1a64ef20134"
    twitter_list_id: str = "1898760983553974442"
    twitter_list_max_pages: int = 20
    twitter_list_fetch_interval_seconds: int = 1800

    # ------------------------------ 时区 -----------------------------------
    # 时间戳写库时统一带 tz;period_start/period_end 等字段也用它构造。
    # frozen dataclass 中可变默认必须用 default_factory
    timezone: ZoneInfo = field(default_factory=lambda: ZoneInfo("UTC"))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    返回单例 Settings。

    通过 lru_cache 保证整个进程只构造一次,避免重复实例化。
    """
    return Settings()
