from __future__ import annotations

"""
**Phase 2** 告警配置（Telegram 实时激增告警）。

只在 `telegram_bot_token` 与 `telegram_chat_id` 都非空时被 main.py 读取并
构造 AlertTriggerService；任一为空 → 整个告警系统禁用，不影响 hotness 主流程。

修改这里的字段不会影响新链路 / 老链路任何 service 的工作（hotness_snapshots
照常产出），只影响是否往 Telegram 推送。

字段全部带 `alert_` / `telegram_` 前缀，跨 4 个分组（Database/Runtime/Legacy/
NewPipeline）字段名唯一，多继承时不会与其他分组冲突。

Token 存放策略（用户决策方案 A）：
- 直接硬编码本文件的 telegram_bot_token / telegram_chat_id 后 commit
- 仓库非公开 + 泄露后 BotFather /revoke 5 秒重发
- 不走环境变量 / 单独本地配置文件方案（Phase 3 视情况再升级）
"""

from dataclasses import dataclass


# 默认消息模板（见 requirements.md Req 3.3 / design.md §3.3）
# 注意：{is_new_entity_mark} 字段在 _render_message 中渲染为
# "★ 新实体（基线为 0）\n" 或空串，这里末尾 \n 由它自己负责。
_DEFAULT_TEMPLATE = (
    "🔥 {alert_type}\n"
    "实体: {entity} ({entity_type})\n"
    "增长: {growth_rate}x（基于过去 7 天基线）\n"
    "提及: {count_short} 次 / 1h\n"
    "跨源: {cross_source}\n"
    "{is_new_entity_mark}"
    "rank: #{rank} @ {window_end}"
)


@dataclass(frozen=True)
class AlertSettings:
    # ==========================================================================
    # Telegram Bot 凭据
    # --------------------------------------------------------------------------
    # 任一为空字符串 → main.py 跳过 AlertTriggerService 构造（log INFO 不 raise）。
    # 用户决策方案 A：填真值后 commit；泄露走 BotFather /revoke。
    # ==========================================================================

    # Bot Token：BotFather /newbot 创建后给的字符串，形如 "8123456789:AAH-xxx..."
    # 留空字符串 = 禁用告警
    telegram_bot_token: str = "8669613338:AAFIvUj45VJp5crpQuj51PwP0uLn2ICzq4g"

    # 接收消息的 chat_id：从 getUpdates API 拿到。
    # 私聊一般是正整数；群组是负整数（大整数）。
    # 留空字符串 = 禁用告警
    telegram_chat_id: str = "5238866626"

    # HTTP 超时（秒）。Telegram API 一般 < 1s 响应，10s 防卡死。
    # 国内服务器走 VPN 时网络抖动可能让响应到 5~8s，10s 留足余量。
    telegram_timeout_seconds: int = 10

    # ==========================================================================
    # 告警触发条件（基础门槛）
    # --------------------------------------------------------------------------
    # 必须同时满足三条门槛才进入"智能冷却"判断；任一不满足直接跳过。
    # ==========================================================================

    # growth_rate ≥ 此值才告警；默认 20 倍（"够热"）。
    # 调小 → 告警更频繁；调大 → 只接 super hot。
    # 部署后建议先观察 1 周 hotness_snapshots.growth_rate 的 99% 分位再调整。
    alert_growth_threshold: float = 20.0

    # count_short ≥ 此值；避免 1 次提及就告警（噪音）。
    # ★ 当前数据流量较低，临时调低到 2；流量起来后改回 3
    alert_min_count_short: int = 3

    # cross_source ≥ 此值；Phase 1 跨源命中率低，默认 1（单源也告警）。
    # 调成 2 → 只接多源共振信号（更高质量但告警更稀疏）。
    alert_min_cross_source: int = 1

    # ==========================================================================
    # 智能冷却参数（4 路径决策树，详见 design.md §3.2.1）
    # --------------------------------------------------------------------------
    # 同实体的告警决策按优先级：
    #   1. 首次告警 → [首次]
    #   2. elapsed >= heartbeat_hours → [持续 Nh]
    #   3. growth ≥ 上次 × escalation_growth_multiplier → [升级]
    #   4. cross_source 增加 → [跨源升级]
    #   5. cooldown 内 + 无质变 → 不告警
    #   6. cooldown 外 + 仍达阈值 → [重新触发]
    # ==========================================================================

    # 常规冷却期（分钟）：同实体在此期间内只在"质变"时才再告警。
    # 60 分钟是经验值——足够避免刷屏，又不会错过 1~2 小时维度的脉冲。
    alert_cooldown_minutes: int = 60

    # growth 升级倍数：本次 growth ≥ 上次告警时 growth × 此倍数 → 立刻升级告警。
    # 默认 1.5 倍（上次告警 growth=20，本次 ≥ 30 即重发）；调大 → 升级更难触发。
    alert_escalation_growth_multiplier: float = 1.5

    # 心跳提醒间隔（小时）：持续热点最长不告警时长，超过此值即便没质变也再发一次。
    # 设计意图：6 小时一次"我还在烧"的提醒，避免用户以为告警系统挂了。
    alert_heartbeat_hours: int = 6

    # ==========================================================================
    # 消息渲染
    # ==========================================================================

    # 消息模板，支持以下占位符（详见 requirements.md Req 3.2）：
    #   {alert_type}             触发原因标签（"[首次]" / "[升级 → growth ×2.0]" / ...）
    #   {entity}                 实体名
    #   {entity_type}            类型（ticker/chain/narrative/project，缺失时 "<n/a>"）
    #   {growth_rate}            增长倍数（已格式化为 "20.5"）
    #   {count_short}            1h 提及次数
    #   {cross_source}           跨源数
    #   {is_new_entity_mark}     "★ 新实体..." 或空串（**自带换行**）
    #   {window_end}             窗口时刻（YYYY-MM-DD HH:MM）
    #   {rank}                   当前榜单排名
    alert_message_template: str = _DEFAULT_TEMPLATE

    # ==========================================================================
    # Phase 2.4 实时触发（design.md §3.4）
    # --------------------------------------------------------------------------
    # 整点榜（:00/:15/:30/:45）的端到端最坏延迟 14~15 分钟；实时 hook 把 entity_extractor
    # 写入新提及的事件直接转成 RealtimeAlertService.notify(n_added)，到阈值就跑一次
    # 轻量计算 + Telegram 推送，把延迟压到 1~2 分钟。失败/未配置自动降级到 Phase 2.2。
    # ==========================================================================

    # 实时触发总开关。False → main.py 跳过 RealtimeAlertService 构造，
    # 行为退化为 Phase 2.2（仅整点告警，端到端最坏 14~15 分钟延迟）。
    # True → 启用实时 hook，端到端延迟降到 1~2 分钟。
    realtime_enabled: bool = True

    # 累积多少条新提及触发一次实时计算（design §3.1.1 burst_threshold）。
    # 50 是经验值：低流量期 5~10 轮（25~50 秒）攒满；高流量期单轮就触发。
    # 调小 → 触发更频繁但 CPU/Telegram 压力大；调大 → 反之。
    realtime_burst_threshold: int = 50

    # growth_rate ≥ 此值才告警（与整点 alert_growth_threshold 独立配置）。
    # 30 比整点严——分钟级窗口的 growth 抖动比整点榜大，
    # 把噪音放进去会把 Telegram 灌死。
    realtime_growth_threshold: float = 30.0

    # count_short ≥ 此值才告警（与整点 alert_min_count_short 独立配置）。
    # 5 比整点严，过滤"3 条偶然提及就触发"——
    # 分钟级窗口里 3 条可能就是同一个 KOL 转发同话题。
    realtime_min_count_short: int = 5
