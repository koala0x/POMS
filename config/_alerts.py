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
    # PushPlus 微信推送（与 Telegram 并行，同时推两个渠道）
    # --------------------------------------------------------------------------
    # PushPlus（https://www.pushplus.plus）通过微信公众号推消息到你的微信。
    # 免费版 200 条/天，足够本项目使用。
    #
    # 启用条件：pushplus_token 非空字符串。
    # 为空 → PushPlusClient 不构造，只走 Telegram 通道（向后兼容）。
    # ==========================================================================

    # PushPlus token：登录 https://www.pushplus.plus 后在"一对一推送"页面获取。
    # 留空字符串 = 不启用微信推送
    pushplus_token: str = "422b9ccebf94453d91ca644120fbbeeb"

    # PushPlus 消息模板：markdown / html / txt
    # 推荐 markdown，微信端渲染效果好（加粗/代码块都支持）
    pushplus_template: str = "markdown"

    # PushPlus HTTP 超时（秒）
    pushplus_timeout_seconds: int = 10

    # ==========================================================================
    # 告警触发条件（基础门槛）
    # --------------------------------------------------------------------------
    # 必须同时满足三条门槛才进入"智能冷却"判断；任一不满足直接跳过。
    # ==========================================================================

    # growth_rate ≥ 此值才告警；默认 20 倍（"够热"）。
    # 调小 → 告警更频繁；调大 → 只接 super hot。
    # ★ 2026-05-15 调参：tune_helper 推荐 ~10 条/天 = 1.5
    alert_growth_threshold: float = 1.5

    # count_short ≥ 此值；避免 1 次提及就告警（噪音）。
    # 流量起来后改回 3；当前 2 = 至少被提到 2 次才算
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
    alert_cooldown_minutes: int = 30

    # growth 升级倍数：本次 growth ≥ 上次告警时 growth × 此倍数 → 立刻升级告警。
    # 默认 1.5 倍（上次告警 growth=20，本次 ≥ 30 即重发）；调大 → 升级更难触发。
    alert_escalation_growth_multiplier: float = 3

    # 心跳提醒间隔（小时）：持续热点最长不告警时长，超过此值即便没质变也再发一次。
    # 设计意图：6 小时一次"我还在烧"的提醒，避免用户以为告警系统挂了。
    alert_heartbeat_hours: int = 3

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
    realtime_burst_threshold: int = 20

    # growth_rate ≥ 此值才告警（与整点 alert_growth_threshold 独立配置）。
    # 30 比整点严——分钟级窗口的 growth 抖动比整点榜大，
    # 把噪音放进去会把 Telegram 灌死。
    realtime_growth_threshold: float = 40.0

    # count_short ≥ 此值才告警（与整点 alert_min_count_short 独立配置）。
    # 5 比整点严，过滤"3 条偶然提及就触发"——
    # 分钟级窗口里 3 条可能就是同一个 KOL 转发同话题。
    realtime_min_count_short: int = 5

    # ==========================================================================
    # Phase 2.8 决策树调优：cooldown 内 growth 累积升级
    # --------------------------------------------------------------------------
    # 老决策树要求 growth ≥ 上次 × escalation_growth_multiplier(=1.5) 才升级，
    # 低流量场景下 growth 翻倍很难，导致大量 entity 落进"60min 内无质变"被静默。
    #
    # 新增软门槛：cooldown 内只要 growth 比上次告警时涨 ≥ growth_delta_pct，
    # 就升级为 [growth +X%] 告警。1.5x 仍保留，命中"剧烈翻倍"时的强信号。
    # ==========================================================================

    # cooldown 内 growth 增长百分比阈值（0.0 = 关闭，0.3 = 涨 30% 即升级）。
    # 默认 0.3：比 escalation_growth_multiplier(1.5) 宽松一档，
    # 让"温和但持续走高"的实体也能在 cooldown 内被推送一次。
    alert_growth_delta_pct: float = 1

    # ==========================================================================
    # Phase 2.8 多窗口告警（per-window 阈值参数化）
    # --------------------------------------------------------------------------
    # 老 AlertTriggerService 写死只读 1h 榜；6h/24h 榜虽然在写库但不告警。
    # Phase 2.8 让 main.py 给每个窗口独立构造一个 AlertTriggerService 实例，
    # 各自独立 growth_threshold / min_count_short，覆盖"短期突变 / 中期趋势
    # / 宏观叙事"全谱告警。
    #
    # 任一窗口的 *_enabled = False → main.py 跳过对应实例构造，零开销。
    # ==========================================================================

    # 6h 窗口告警是否启用（默认 ON：中期趋势是 alpha 的核心信号源）
    alert_6h_enabled: bool = True
    # 6h 窗口 growth_rate 告警阈值。比 1h 严一档（噪音少 → 阈值低也安全），
    # 但绝对值仍要求"明显异常"
    alert_6h_growth_threshold: float = 1.6
    alert_6h_min_count_short: int = 5
    alert_6h_min_cross_source: int = 1

    # 3h 窗口告警（Phase 2.8 新增）。阈值介于 1h 与 6h 之间。
    alert_3h_enabled: bool = True
    alert_3h_growth_threshold: float = 1.5
    alert_3h_min_count_short: int = 5
    alert_3h_min_cross_source: int = 1

    # 24h 窗口告警是否启用（默认 ON：宏观叙事每天就那么 1~2 个，告警不会刷屏）
    alert_24h_enabled: bool = True
    # 24h growth 阈值最低（基线最稳定 → 任何明显变化都值得通知）
    alert_24h_growth_threshold: float = 8.9
    alert_24h_min_count_short: int = 10
    alert_24h_min_cross_source: int = 1

    # ==========================================================================
    # Phase 2.8 告警黑名单（与 hotness 黑名单解耦）
    # --------------------------------------------------------------------------
    # hotness_exclude_entities 控制"哪些 entity 进 Top-K 表"：
    #   - 1h / 6h 屏蔽 BTC/ETH/SOL/BNB + 稳定币（小币聚焦）
    #   - 24h 只屏蔽稳定币（让宏观事件能上榜，给 Digest 看）
    #
    # 但"上榜"和"告警"应该分离：
    #   - 24h 榜里 BTC growth=3 是宏观信号，值得在 Digest 看见
    #   - 但不应该 push 一条 [首次] BTC 告警——用户视角"大币告警没用"
    #
    # alert_exclude_entities 是**所有 alert 实例**（1h/6h/24h/realtime）共用的
    # 黑名单：被屏蔽的 entity **永远不会触发 Telegram push**，但仍可能出现在
    # Digest 推送 / hotness_snapshots 表 / 数据库查询里。
    #
    # 默认包含 BTC/ETH/SOL/BNB + 稳定币，覆盖"提到很多但你不想被 push 通知"
    # 的所有大币。命中比较时大小写不敏感（service 内部 .upper()）。
    # ==========================================================================
    alert_exclude_entities: tuple[str, ...] = (
        "BTC", "ETH", "SOL", "BNB",
        "USDT", "USDC", "DAI", "OP"
    )

    # ==========================================================================
    # Phase 2.8 定期热榜 Digest 推送（DigestPusherService）
    # --------------------------------------------------------------------------
    # 周期性把 1h/6h/24h 三窗口最新 Top-N 拼成一条消息推 Telegram。
    # 与 AlertTriggerService 互补：alert 看突变（事件触发），digest 看全貌（周期触发）。
    # 老链路（Level1Service / Level2Service）淘汰后这条通道一直缺位，本字段补回。
    # ==========================================================================

    # digest 推送总开关。False → main.py 跳过 service 构造；与 telegram_*
    # 任一为空时也自动禁用（与 alert 一样）
    digest_enabled: bool = True

    # 每次 digest 取每个窗口的 Top-N。
    # 默认 10 ≈ 一条消息 ~1500 字符（含 Markdown 排版），远低于 Telegram 4000 上限。
    # 调大需注意：top_n=20 × 3 个窗口 ≈ 4000 字符，可能触发自动截断。
    digest_top_n: int = 10

    # 推送间隔（整刻钟数）：
    #   1 = 每 15min 一次（HotnessService 写完就推）
    #   2 = 每 30min 一次（半小时整点）
    #   4 = 每小时一次（每小时 :00 整点；默认）
    # 默认 4 平衡"信息密度"和"刷屏感"；用户嫌少改 2，调试改 1
    digest_push_every_quarters: int = 4

    # digest 推送哪些窗口（按 tuple 顺序拼接到同一条消息）。
    # 默认 ("1h","3h","6h","24h") 四个全推；用户可只推 1h 或只推 24h
    digest_window_types: tuple[str, ...] = ("1h", "3h", "6h", "24h")
