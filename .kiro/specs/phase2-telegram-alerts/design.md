# Phase 2 · Task 2.2 Telegram 实时激增告警 · Design

> 基于 requirements.md v1.1 的架构与接口设计。

## 1. 概述

### 1.1 目标

把 hotness_snapshots 接上 Telegram Bot，当某 entity 短窗 growth_rate 突破
阈值（默认 20.0）时，自动推送 Telegram 消息给用户。

### 1.2 三条核心设计哲学

1. **不阻塞主流程**：Telegram 不可达 / 配置缺失 → 优雅降级，hotness 继续产出
2. **进程内冷却**：用 dict 做去重，跟 SlidingCounter 一样不持久化（Phase 1
   单 worker 线程，无并发问题；重启后冷却失效，但每实体最多多发 1 次告警可接受）
3. **配置驱动开关**：`telegram_bot_token == "" or telegram_chat_id == ""`
   → 整个 AlertTriggerService 不构造，零运行时开销

### 1.3 与 Phase 1 的关系

```
Phase 1 终点（不变）        Phase 2 新增（本任务）
─────────────────────────    ─────────────────────────────────
HotnessService.run_once    
  └─> hotness_snapshots ──> AlertTriggerService.run_once
                              ├─> 读 hotness_snapshots（最新窗口）
                              ├─> 判断 growth/count/cooldown
                              └─> TelegramClient.send_text
                                    └─> https://api.telegram.org
                                          └─> 你的 Telegram 客户端
```

新增完全旁挂在 Hotness 之后，**不修改任何 Phase 1 代码**（除 main.py 注入）。

## 2. 总架构图

```
┌──────────────────────────────────────────────────────────────────────────┐
│  scheduler/jobs.py worker 主循环（Phase 1 已有，无改动）                  │
│                                                                            │
│  for svc in new_services:                                                 │
│    [Normalizer, EntityExtractor, Hotness, AlertTrigger ★新增]            │
│      svc.run_once()                                                       │
└─────────────────────────────────┬────────────────────────────────────────┘
                                  │
                                  ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  AlertTriggerService（services/l2_alert_trigger.py，新增）                │
│                                                                            │
│  状态：                                                                    │
│  - _last_processed_window_end: Optional[datetime]                         │
│  - _alert_records: dict[entity_name, AlertRecord]                         │
│    （AlertRecord 含 last_alerted_at + last_growth_rate + last_cross_source）│
│                                                                            │
│  run_once 流程：                                                           │
│  1. 查最新 window_end                                                      │
│  2. 与 _last_processed_window_end 比较，相同则跳过                         │
│  3. 读这个窗口的 records                                                   │
│  4. 筛 growth_rate ≥ threshold + count_short ≥ min + cross_source ≥ min  │
│  5. _decide_alert 智能冷却决策（首次/心跳/升级/跨源升级/重新触发）           │
│  6. 渲染消息（用 settings.alert_message_template）                        │
│  7. 调 TelegramClient.send_text                                           │
│  8. 推送成功 → 写入 _alert_records（含 growth/cross_source 快照）          │
│  9. 更新 _last_processed_window_end                                        │
└─────────────────────────────────┬────────────────────────────────────────┘
                                  │
                                  ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  TelegramClient（notifications/telegram_client.py，新增）                  │
│                                                                            │
│  - 标准库 urllib.request 发起 POST                                          │
│  - 不抛异常，所有错误返回 False + log                                       │
│  - timeout 默认 10s（可配置）                                              │
└──────────────────────────────────────────────────────────────────────────┘
```

## 3. 详细设计

### 3.1 TelegramClient（notifications/telegram_client.py）

**为什么用 urllib 不用 requests**：
- 标准库自带，零新依赖（requirements.md 硬约束）
- 调用频率低（最多每分钟几次），不需要连接池
- 简单到 50 行内能搞定

**接口**：

```python
# notifications/telegram_client.py
from dataclasses import dataclass

@dataclass(frozen=True)
class TelegramClient:
    bot_token: str
    chat_id: str
    timeout_seconds: int = 10

    def send_text(self, text: str, *, parse_mode: str | None = None) -> bool:
        """
        发送纯文本消息。
        
        参数：
        - text: 消息正文（≤ 4096 字符，Telegram 上限）
        - parse_mode: None / "Markdown" / "HTML"（Phase 2 起步全用 None）
        
        返回 True 即推送成功；任何异常（网络超时、HTTP 4xx、token 错误等）
        都返回 False + 记录 ERROR 日志。绝不抛异常给调用方。
        """
        ...
```

**实现要点**：

```python
import json
import urllib.request
import urllib.parse
from urllib.error import URLError, HTTPError
from loguru import logger


def send_text(self, text, *, parse_mode=None):
    if not self.bot_token or not self.chat_id:
        logger.error("telegram send failed: bot_token / chat_id 未配置")
        return False
    
    # Telegram 消息上限 4096
    if len(text) > 4000:
        text = text[:3997] + "..."
    
    url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
    payload = {"chat_id": self.chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    
    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    
    try:
        with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
            body = resp.read().decode("utf-8")
            result = json.loads(body)
            if result.get("ok") is True:
                return True
            else:
                logger.error("telegram api 返回非 ok: {}", result)
                return False
    except HTTPError as e:
        # 4xx / 5xx 都走这里
        logger.error("telegram http error: {} {}", e.code, e.reason)
        return False
    except URLError as e:
        # 网络层错误（DNS / 连接拒绝 / 超时）
        logger.error("telegram network error: {}", e.reason)
        return False
    except Exception as e:
        logger.error("telegram unexpected error: {}", e)
        return False
```

### 3.2 AlertTriggerService（services/l2_alert_trigger.py）

#### 3.2.1 智能冷却策略（核心逻辑）

不简单地"60 分钟内不告警"，而是用 **AlertRecord** 记录每个 entity 的上次告警状态，
判断本次是否构成"质变"。判断逻辑（4 选 1）：

```python
def _decide_alert(rec, current, now) -> tuple[bool, str]:
    """
    返回 (是否告警, 触发原因标签)。
    
    rec: 上次告警记录（None 表示从未告警过）
    current: 本次 hotness record（含 growth_rate / cross_source / window_end）
    now: 当前时刻（datetime）
    """
    if rec is None:
        return True, "[首次]"
    
    elapsed = now - rec.last_alerted_at
    
    # 心跳：距上次 > heartbeat_hours，即便没质变也再发一次
    if elapsed >= timedelta(hours=heartbeat_hours):
        hours = int(elapsed.total_seconds() // 3600)
        return True, f"[持续 {hours}h]"
    
    # growth 升级：本次 ≥ 上次 × multiplier（默认 1.5x）
    if current.growth_rate >= rec.last_growth_rate * escalation_growth_multiplier:
        ratio = current.growth_rate / rec.last_growth_rate
        return True, f"[升级 → growth ×{ratio:.1f}]"
    
    # 跨源升级：cross_source 变多
    if current.cross_source > rec.last_cross_source:
        delta = current.cross_source - rec.last_cross_source
        return True, f"[跨源升级 +{delta}]"
    
    # 常规冷却：60 分钟内 + 没质变 → 不告警
    if elapsed < timedelta(minutes=cooldown_minutes):
        return False, ""
    
    # 60 分钟外 + 没质变 + 仍达阈值 → 视为重新触发
    return True, "[重新触发]"
```

#### 3.2.2 完整接口

```python
# services/l2_alert_trigger.py
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from loguru import logger

from db.connection import Database
from db.repositories.hotness_snapshots_repo import HotnessSnapshotsRepo
from notifications.telegram_client import TelegramClient


@dataclass(frozen=True)
class AlertRecord:
    """每个 entity 上次告警时的快照，用于判断本次是否质变。"""
    last_alerted_at: datetime
    last_growth_rate: float
    last_cross_source: int


@dataclass
class AlertTriggerService:
    db: Database
    hotness_repo: HotnessSnapshotsRepo
    telegram_client: TelegramClient
    
    # 触发阈值
    growth_threshold: float = 20.0
    min_count_short: int = 3
    min_cross_source: int = 1
    
    # 智能冷却参数
    cooldown_minutes: int = 60
    escalation_growth_multiplier: float = 1.5
    heartbeat_hours: int = 6
    
    # 消息模板
    message_template: str = (
        "🔥 {alert_type}\n"
        "实体: {entity} ({entity_type})\n"
        "增长: {growth_rate}x（基于过去 7 天基线）\n"
        "提及: {count_short} 次 / 1h\n"
        "跨源: {cross_source}\n"
        "{is_new_entity_mark}"
        "rank: #{rank} @ {window_end}"
    )
    
    # ----- 运行时状态 -----
    _last_processed_window_end: Optional[datetime] = None
    _alert_records: dict[str, AlertRecord] = field(default_factory=dict)
    
    def run_once(self) -> bool:
        # 1. 查最新窗口
        with self.db.get_session() as session:
            latest = self.hotness_repo.fetch_latest_window_end(session, "1h")
        
        if latest is None:
            return False
        
        if (
            self._last_processed_window_end is not None
            and latest <= self._last_processed_window_end
        ):
            return False
        
        # 2. 读这个窗口的 records
        with self.db.get_session() as session:
            records = self.hotness_repo.fetch_top_k(
                session, window_end=latest, window_type="1h", k=100
            )
        
        # 3. 智能冷却 + 推送
        sent = 0
        now = datetime.now(timezone.utc)
        for rec in records:
            if not self._is_eligible(rec):
                continue
            
            should_alert, alert_type = self._decide_alert(rec, now)
            if not should_alert:
                logger.info(
                    "alert skipped: entity={} 60min 内无质变",
                    rec.entity,
                )
                continue
            
            text = self._render_message(rec, alert_type)
            if self.telegram_client.send_text(text):
                # 推送成功才更新告警记录
                self._alert_records[rec.entity] = AlertRecord(
                    last_alerted_at=now,
                    last_growth_rate=rec.growth_rate,
                    last_cross_source=rec.cross_source,
                )
                sent += 1
                logger.info(
                    "alert sent: entity={} growth={:.1f} type={}",
                    rec.entity, rec.growth_rate, alert_type,
                )
        
        # 不论是否真的发出告警，都标记本窗口已处理
        self._last_processed_window_end = latest
        return sent > 0
    
    def _is_eligible(self, rec) -> bool:
        return (
            rec.growth_rate is not None
            and rec.growth_rate >= self.growth_threshold
            and rec.count_short is not None
            and rec.count_short >= self.min_count_short
            and rec.cross_source is not None
            and rec.cross_source >= self.min_cross_source
        )
    
    def _decide_alert(self, rec, now: datetime) -> tuple[bool, str]:
        """智能冷却决策。返回 (是否告警, 触发类型标签)。"""
        last = self._alert_records.get(rec.entity)
        
        # 首次告警
        if last is None:
            return True, "[首次]"
        
        elapsed = now - last.last_alerted_at
        
        # 心跳提醒：超过 heartbeat_hours
        if elapsed >= timedelta(hours=self.heartbeat_hours):
            hours = int(elapsed.total_seconds() // 3600)
            return True, f"[持续 {hours}h]"
        
        # growth 翻倍升级
        if (
            last.last_growth_rate > 0
            and rec.growth_rate >= last.last_growth_rate * self.escalation_growth_multiplier
        ):
            ratio = rec.growth_rate / last.last_growth_rate
            return True, f"[升级 → growth ×{ratio:.1f}]"
        
        # 跨源升级
        if rec.cross_source > last.last_cross_source:
            delta = rec.cross_source - last.last_cross_source
            return True, f"[跨源升级 +{delta}]"
        
        # 常规冷却：60 分钟内 + 没质变 → 不告警
        if elapsed < timedelta(minutes=self.cooldown_minutes):
            return False, ""
        
        # 60 分钟外 + 仍达阈值但无明显升级 → 重新触发
        return True, "[重新触发]"
    
    def _render_message(self, rec, alert_type: str) -> str:
        is_new_mark = "★ 新实体（基线为 0）\n" if rec.is_new_entity else ""
        try:
            return self.message_template.format(
                alert_type=alert_type,
                entity=rec.entity,
                entity_type=rec.entity_type or "<n/a>",
                growth_rate=f"{rec.growth_rate:.1f}",
                count_short=rec.count_short,
                cross_source=rec.cross_source,
                is_new_entity_mark=is_new_mark,
                window_end=rec.window_end.strftime("%Y-%m-%d %H:%M"),
                rank=rec.rank,
            )
        except KeyError as e:
            logger.warning("alert template missing key: {}, 用默认模板", e)
            return f"🔥 {rec.entity} growth={rec.growth_rate:.1f} rank={rec.rank}"
```

### 3.3 配置（config/_alerts.py）

```python
# config/_alerts.py
from dataclasses import dataclass


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
    # ----- Telegram Bot -----
    
    # Bot Token，由 BotFather /newbot 创建后给你的字符串
    # 留空字符串 → 整个告警系统禁用（main.py 跳过 Service 构造）
    telegram_bot_token: str = ""
    
    # 接收消息的 chat_id，从 getUpdates API 拿到
    # 私聊一般是正整数；群组是负整数
    telegram_chat_id: str = ""
    
    # HTTP 超时（秒）。Telegram API 一般 < 1s 响应，10s 够防卡死
    telegram_timeout_seconds: int = 10
    
    # ----- 告警触发条件 -----
    
    # growth_rate 必须 ≥ 此值才告警；默认 20 倍
    # 调小 → 告警更频繁；调大 → 只接 super hot
    alert_growth_threshold: float = 20.0
    
    # count_short 必须 ≥ 此值；避免 1 次提及就告警
    alert_min_count_short: int = 3
    
    # cross_source 必须 ≥ 此值；Phase 1 跨源命中率低，默认 1（单源也告警）
    alert_min_cross_source: int = 1
    
    # ----- 智能冷却参数 -----
    
    # 常规冷却期（分钟）：同实体在此期间内只在"质变"时才再告警
    alert_cooldown_minutes: int = 60
    
    # growth 升级倍数：本次 growth ≥ 上次告警时 growth × 此倍数 → 立刻升级告警
    # 默认 1.5 倍，比如上次告警时 growth=20，本次 ≥ 30 即重发
    alert_escalation_growth_multiplier: float = 1.5
    
    # 心跳提醒间隔（小时）：持续热点最长不告警时长，过此值即便没质变也再发一次
    alert_heartbeat_hours: int = 6
    
    # 消息模板（见 Req 3.2 字段）
    alert_message_template: str = _DEFAULT_TEMPLATE
```

### 3.4 main.py 注入

在 Phase 1 现有的 7 步注入流程后追加：

```python
# main.py（在 Step 5c HotnessService 构造之后）

# Step 5d：AlertTriggerService（如配置就绪）
alert_service = None
if settings.telegram_bot_token and settings.telegram_chat_id:
    from notifications.telegram_client import TelegramClient
    from services.l2_alert_trigger import AlertTriggerService
    
    telegram_client = TelegramClient(
        bot_token=settings.telegram_bot_token,
        chat_id=settings.telegram_chat_id,
        timeout_seconds=settings.telegram_timeout_seconds,
    )
    alert_service = AlertTriggerService(
        db=db,
        hotness_repo=hotness_repo,
        telegram_client=telegram_client,
        growth_threshold=settings.alert_growth_threshold,
        min_count_short=settings.alert_min_count_short,
        min_cross_source=settings.alert_min_cross_source,
        cooldown_minutes=settings.alert_cooldown_minutes,
        escalation_growth_multiplier=settings.alert_escalation_growth_multiplier,
        heartbeat_hours=settings.alert_heartbeat_hours,
        message_template=settings.alert_message_template,
    )
    logger.info(
        "AlertTriggerService 启动：growth_threshold={} cooldown={}min "
        "escalation×{} heartbeat={}h",
        settings.alert_growth_threshold,
        settings.alert_cooldown_minutes,
        settings.alert_escalation_growth_multiplier,
        settings.alert_heartbeat_hours,
    )
else:
    logger.info("Telegram 告警未配置（token/chat_id 为空），已禁用")

# new_services 列表追加
new_services = [normalizer_service, entity_extractor, hotness_service]
if alert_service is not None:
    new_services.append(alert_service)

# Jobs 构造（已有）
```

## 4. 文件清单

```
新增：
  notifications/                                [新目录]
    __init__.py                                  [空]
    telegram_client.py                           [TelegramClient + send_text]
  config/_alerts.py                              [AlertSettings]
  services/l2_alert_trigger.py                   [AlertTriggerService]
  tests/test_telegram_client.py                  [覆盖 Req 6.1，5 cases]
  tests/test_l2_alert_trigger.py                 [覆盖 Req 6.2，11 cases]
  .kiro/specs/phase2-telegram-alerts/            [本 spec 目录]
    requirements.md
    design.md
    tasks.md

修改：
  config/settings.py                             [Settings 多继承追加 AlertSettings]
  main.py                                        [追加 Step 5d]
```

## 5. 测试矩阵

| 用例 | 文件 | 类型 | 关键 mock |
|---|---|---|---|
| send 200 OK 返回 True | test_telegram_client | 单元 | urllib.request.urlopen |
| send 4xx 返回 False | test_telegram_client | 单元 | urlopen 抛 HTTPError |
| send 网络错误返回 False | test_telegram_client | 单元 | urlopen 抛 URLError |
| send 不抛任何异常 | test_telegram_client | 单元 | urlopen 抛任意 Exception |
| send 文本超 4000 字符自动截断 | test_telegram_client | 单元 | 检查发出的 payload |
| 首次告警：触发条件全满足 → 推送 [首次] | test_l2_alert_trigger | 集成 | TelegramClient mock |
| growth 不足不触发 | test_l2_alert_trigger | 集成 | TelegramClient mock |
| count_short 不足不触发 | test_l2_alert_trigger | 集成 | TelegramClient mock |
| 60 分钟内 + 无质变 → 不重发 | test_l2_alert_trigger | 集成 | TelegramClient mock |
| **growth 翻倍 → 立刻升级告警 [升级]** | test_l2_alert_trigger | 集成 | monkeypatch datetime.now |
| **cross_source 增加 → 立刻升级告警 [跨源升级]** | test_l2_alert_trigger | 集成 | monkeypatch datetime.now |
| **6h 心跳：>heartbeat_hours 即便没质变也告警 [持续]** | test_l2_alert_trigger | 集成 | monkeypatch datetime.now |
| 60 分钟外 + 仍达阈值 → [重新触发] | test_l2_alert_trigger | 集成 | monkeypatch datetime.now |
| 同窗口不重复扫描 | test_l2_alert_trigger | 集成 | 检查调用次数 |
| send 失败时不进冷却 | test_l2_alert_trigger | 集成 | TelegramClient.send_text 返回 False |
| 空榜单返回 False | test_l2_alert_trigger | 集成 | hotness_snapshots 表为空 |

## 6. 风险与缓解

### 风险 1：Telegram API 在国内被墙

- **症状**：所有 send_text 返回 False，日志大量 "telegram network error"
- **缓解**：
  - 失败优雅降级，hotness 主流程不受影响
  - 不在 service 内做 retry（只会让告警延迟，没意义）
  - 部署时确认服务器能 curl `https://api.telegram.org/bot<token>/getMe`

### 风险 2：告警过频（threshold 太低）

- **症状**：Telegram 一直响个不停
- **缓解**：
  - 60 分钟冷却保底
  - threshold 默认 20.0（够高）
  - 部署后观察一周再调

### 风险 3：告警过少（threshold 太高）

- **症状**：一周一次告警都没有
- **缓解**：
  - settings.py 改 threshold 重启即可，零代码改动
  - SQL 看实际 growth 分布（参考下面）：

```sql
SELECT percentile_cont(0.99) WITHIN GROUP (ORDER BY growth_rate)
FROM hotness_snapshots
WHERE window_end >= now() - INTERVAL '7 days';
-- 拿到的就是"99% 分位的 growth"，threshold 设成这个数 = 每天告警 1 次左右
```

### 风险 4：进程重启冷却失效

- **症状**：重启后某 entity 在 60 分钟内被告警 2 次
- **可接受**：每实体多发 1 次告警的代价远小于"持久化冷却到 DB"的复杂度
- **未来缓解**：Phase 3 真有需求时再做"告警历史表"

## 7. 部署步骤

### 7.1 本地开发完成

1. 写代码（按 tasks.md 顺序）
2. 跑测试 `.venv/bin/python -m pytest tests/ --ignore=tests/test_ollama_client.py -q`
3. 确认 109+3+(5+11) = 128 passed

### 7.2 配置 Telegram

修改 `config/_alerts.py`，把空字符串替换成你的真实 token / chat_id：

```python
telegram_bot_token: str = "8123456789:AAH-xxx..."
telegram_chat_id: str = "6789012345"
```

或者通过环境变量 / 单独配置文件覆盖（推荐 Phase 3 改造，本任务先硬编码）。

### 7.3 重启服务

```bash
./scripts/restart.sh
```

预期日志：

```
AlertTriggerService 启动：growth_threshold=20.0 cooldown=60min escalation×1.5 heartbeat=6h
```

### 7.4 验证

最快验证：把 threshold 临时改成 1.0，重启，下一份榜出来时立刻收到 Telegram 告警。
确认收到后改回 20.0。

---

*文档版本：v1.1*
*基于：requirements.md v1.1*
*v1.0 → v1.1：§3.2 加入 _decide_alert + AlertRecord + 测试矩阵 11 项*
