from __future__ import annotations

"""
L5 LLM 定向简报服务（Phase 2.7 新增）。

职责（对应 requirements.md Req 1~10 / design.md §3）：
- 每 15 分钟对齐到 :00 / :15 / :30 / :45 触发一次（与 HotnessService 同款）
- 取最新 1h 榜 Top-N（默认 5），筛 growth_rate >= min_growth 的实体
- 跳过同窗口已生成 briefing 的实体（ON CONFLICT DO NOTHING 兜底 + fetch_for_entity 提前过滤）
- 拉每个 entity 的最近 1h 代表消息（最多 evidence_count 条）
- 渲染 prompt → 调 OllamaClient.chat() → JSON.loads → 写 entity_briefings

★ 与 Phase 1 / Phase 2.x"零 LLM"硬约束的关系：
- 本服务**明确突破**该约束，但只在"信号已经产生"之后调 LLM 加解释
- LLM 输出**不反向影响**信号产生链路（hotness/cooccur/alert 决策都不读 briefing）
- 详细论证见 docs/faq_design_decisions.md Q11

调度顺序：必须排在所有写库 service（Normalizer / EntityExtractor / 全部
HotnessService / AlertTriggerService / CooccurrenceService）**之后**——
LLM 推理慢（CPU 模式 ~30s/次，Top-5 一轮 ~2 分钟），不能阻塞实时管道。

异常隔离：
- 单 entity LLM 失败（超时 / 连接错 / JSON 解析失败）→ log.warning，不写表，
  下一轮 entity 仍可重试
- 整轮无论成功失败都更新 `_last_processed_window_end`，避免反复扫同一窗口
  （已处理 entity 走 fetch_for_entity 命中跳过即可）
"""

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from loguru import logger

from db.connection import Database
from db.models import EntityMention, NormalizedMessage
from db.repositories.briefings_repo import BriefingsRepo
from db.repositories.cooccurrence_repo import CooccurrenceRepo
from db.repositories.entity_mentions_repo import EntityMentionsRepo
from db.repositories.hotness_snapshots_repo import HotnessSnapshotsRepo
from db.repositories.normalized_messages_repo import NormalizedMessagesRepo
from llm.ollama_client import OllamaClient
from services.l2_hotness import align_to_quarter
from sqlalchemy import select


# 期望的 JSON 字段集合（_parse_json 用于浅校验；缺字段不 raise，缺一个填 None）
_EXPECTED_KEYS = {"narrative", "catalyst", "fund_logic", "sentiment", "confidence"}

# sentiment 合法值（不在此集合的统一归 None；不 raise）
_SENTIMENT_VALUES = {"bullish", "bearish", "neutral"}

# 单条消息文本截断长度（spec Req 4.4：Twitter 一条 280 / 长文截断到 300）
_MAX_MSG_TEXT_LEN = 300


# Ollama 结构化输出（JSON Schema）。
# - 传给 OllamaClient.chat(format=...)，Ollama 解码时强制约束字段名 / 类型 / 枚举值，
#   能从源头消除本地小模型常见的"sentiment: neutral"（裸枚举值无引号）失败模式
# - 所有字段都允许 null（spec Req 3.3）
# - sentiment 严格走 enum，避免出现 "Bullish"/"中性" 等离群值
# - 见 https://ollama.com/blog/structured-outputs
_BRIEFING_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "narrative": {"type": ["string", "null"]},
        "catalyst": {"type": ["string", "null"]},
        "fund_logic": {"type": ["string", "null"]},
        "sentiment": {
            "type": ["string", "null"],
            "enum": ["bullish", "bearish", "neutral", None],
        },
        "confidence": {"type": ["number", "null"]},
    },
    "required": [
        "narrative",
        "catalyst",
        "fund_logic",
        "sentiment",
        "confidence",
    ],
    "additionalProperties": False,
}


# 非法 JSON 兜底：把 "sentiment": neutral 这类裸枚举值修成 "neutral"
# 仅在 json.loads 失败时启用，命中即重试一次解析（不影响正常路径）
_BARE_ENUM_RE = re.compile(
    r'("sentiment"\s*:\s*)(bullish|bearish|neutral)(\s*[,}\]])'
)


@dataclass
class BriefingService:
    """
    L5 LLM 定向简报生成器。

    ★ 与 HotnessService / AlertTriggerService 共享 hotness_repo / mentions_repo
    引用（main.py 注入），但**不**共享任何冷却 dict / 状态——本服务的幂等完全
    走 DB（uq_entity_briefings_entity_window 唯一约束），不依赖进程内状态。

    默认值与 NewPipelineSettings 的 `briefing_*` + LegacySettings 的
    `ollama_model_level5` / `ollama_timeout_level5` 字段对齐；main.py 显式传所有
    参数，单测可省略部分参数走默认。
    """

    db: Database
    hotness_repo: HotnessSnapshotsRepo
    mentions_repo: EntityMentionsRepo
    normalized_repo: NormalizedMessagesRepo
    briefing_repo: BriefingsRepo
    ollama: OllamaClient
    prompt_path: Path

    # 可选：共现网络 hint（design §3.5 协同）。None → prompt 里填"（暂无）"
    cooccur_repo: Optional[CooccurrenceRepo] = None

    # 触发参数
    top_n: int = 5
    min_growth: float = 30.0
    evidence_count: int = 10

    timezone: ZoneInfo = field(default_factory=lambda: ZoneInfo("UTC"))

    # 运行时状态（不持久化；进程重启清零）
    _last_processed_window_end: Optional[datetime] = None

    # ----------------------------------------------------------------------
    # 公共 API
    # ----------------------------------------------------------------------

    def run_once(self) -> bool:
        """
        执行一轮 briefing 生成。

        返回值：
        - True：本轮至少为 1 个 entity 生成了 briefing（已落库）
        - False：跳过（无新窗口 / 无合格 entity / 全失败）

        跳过场景（参考 spec Req 2.3）：
        1. 取不到最新 hotness window_end（hotness_snapshots 空）
        2. 当前 window_end == _last_processed_window_end：同一整点已扫过
        3. Top-N 全部 growth < min_growth：本轮无值得 brief 的实体
        4. 所有实体都已有 briefing（fetch_for_entity 命中）：跳过
        5. 所有 LLM 调用失败：log.warning 不写表，整轮 False
        """
        # ------ Step 1：取最新 hotness 1h 榜 window_end ------
        with self.db.get_session() as session:
            latest = self.hotness_repo.fetch_latest_window_end(session, "1h")
        if latest is None:
            return False

        # ------ Step 2：同窗口已扫过 → 跳过 ------
        if (
            self._last_processed_window_end is not None
            and latest == self._last_processed_window_end
        ):
            logger.info("briefing skipped: latest window already processed")
            return False

        # ------ Step 3：拉 Top-N 候选 ------
        with self.db.get_session() as session:
            top_records = self.hotness_repo.fetch_top_k(
                session, window_end=latest, window_type="1h", k=self.top_n
            )

        # 过滤 growth >= min_growth
        eligible = [
            r for r in top_records
            if r.growth_rate is not None and r.growth_rate >= self.min_growth
        ]

        if not eligible:
            logger.info(
                "briefing skipped: no eligible entity in Top-{} (min_growth={})",
                self.top_n,
                self.min_growth,
            )
            # 记录窗口已扫，避免下一轮反复
            self._last_processed_window_end = latest
            return False

        # ------ Step 4：逐个 entity 生成 briefing ------
        sent = 0
        for rec in eligible:
            entity = rec.entity

            # 过滤已生成（避免重复调 LLM）
            with self.db.get_session() as session:
                existing = self.briefing_repo.fetch_for_entity(
                    session, entity=entity, window_end=latest
                )
            if existing is not None:
                logger.info(
                    "briefing skipped: entity={} 同窗口已生成", entity
                )
                continue

            # 单 entity 全程 try/except 隔离（spec Req 5.4）
            try:
                ok = self._generate_one(entity=entity, window_end=latest)
                if ok:
                    sent += 1
            except Exception as e:
                # 任何未预期异常都不传播给 worker
                logger.warning(
                    "briefing LLM call failed: entity={} err={}", entity, e
                )

        # ------ Step 5：标记本窗口已扫 ------
        # 不论是否真的成功生成都标记，避免下一轮反复扫同一窗口
        # 已成功的进 fetch_for_entity 命中跳过；失败的下一轮 window_end 改变后再试
        self._last_processed_window_end = latest

        return sent > 0

    # ----------------------------------------------------------------------
    # 内部方法
    # ----------------------------------------------------------------------

    def _generate_one(self, *, entity: str, window_end: datetime) -> bool:
        """
        为单个 entity 生成 briefing 并落库。

        返回 True 表示已成功写入；False 表示跳过/失败（不抛异常）。

        失败场景：
        - evidence 为空：return False，不调 LLM
        - LLM 抛异常：log.warning + return False
        - JSON 解析失败：log.warning + return False
        - 写库 rowcount=0（同窗口已存在）：return False（理论上 fetch_for_entity 已过滤，兜底）
        """
        # 选 evidence
        with self.db.get_session() as session:
            evidence = self._select_evidence(
                session, entity=entity, window_end=window_end
            )
        if not evidence:
            logger.info("briefing skipped: entity={} 无 evidence", entity)
            return False

        # 拉共现 hint（可选）
        cooccur_hint = self._build_cooccur_hint(entity=entity, window_end=window_end)

        prompt = self._render_prompt(
            entity=entity, evidence=evidence, cooccur_hint=cooccur_hint
        )

        # 调 LLM（self.ollama.chat 内部已 try/except，失败抛 RuntimeError）
        # 传 JSON Schema 走 Ollama 结构化输出：保证返回的字符串本身就是合法 JSON，
        # 且 sentiment 字段被约束在 {bullish, bearish, neutral, null} 内，
        # 从源头规避"sentiment: neutral"（裸枚举值缺引号）这类解析失败
        t0 = time.time()
        try:
            response = self.ollama.chat(
                prompt, response_format=_BRIEFING_JSON_SCHEMA
            )
        except Exception as e:
            logger.warning("briefing LLM call failed: entity={} err={}", entity, e)
            return False
        elapsed = time.time() - t0

        # 解析
        try:
            parsed = self._parse_json(response)
        except ValueError as e:
            logger.warning(
                "briefing JSON parse failed: entity={} err={} 原始响应前 200 字: {!r}",
                entity,
                e,
                response[:200],
            )
            return False

        # 落库
        evidence_msg_ids = [int(m.id) for m in evidence]
        # raw_response 保存原始字符串（spec Req 1.4 强制）
        # 用 dict 包一层，让 JSONB 列存"原文 + 解析后"两份信息
        raw_response_jsonb: dict[str, Any] = {
            "raw_text": response,
            "parsed": parsed,
        }
        try:
            with self.db.get_session() as session:
                rowcount = self.briefing_repo.upsert_one(
                    session,
                    entity=entity,
                    window_end=window_end,
                    fields={
                        "narrative": parsed.get("narrative"),
                        "catalyst": parsed.get("catalyst"),
                        "fund_logic": parsed.get("fund_logic"),
                        "sentiment": parsed.get("sentiment"),
                        "confidence": parsed.get("confidence"),
                        "evidence_msg_ids": evidence_msg_ids,
                        "raw_response": raw_response_jsonb,
                    },
                )
                session.commit()
        except Exception as e:
            logger.warning("briefing upsert failed: entity={} err={}", entity, e)
            return False

        if rowcount == 0:
            # 兜底：fetch_for_entity 早已过滤过，但并发 / 同窗口被其他进程写了仍可能 0
            logger.info(
                "briefing skipped (race): entity={} 同窗口已存在", entity
            )
            return False

        logger.info(
            "briefing generated: entity={} narrative={!r} catalyst={!r} "
            "confidence={} elapsed={:.1f}s",
            entity,
            parsed.get("narrative"),
            parsed.get("catalyst"),
            parsed.get("confidence"),
            elapsed,
        )
        return True

    def _select_evidence(
        self,
        session,
        *,
        entity: str,
        window_end: datetime,
    ) -> list[NormalizedMessage]:
        """
        选择 evidence 消息（spec Req 4）。

        策略：
        1. 候选集 = entity_mentions 里 entity=X AND ts ∈ [window_end - 1h, window_end)
           对应的 normalized_messages
        2. 排序：当 engagement 全 0（Phase 2 真实情况）→ ORDER BY random()；
                 否则按 engagement DESC（未来抓取层升级支持）
        3. 取 Top-evidence_count（默认 10）

        返回 NormalizedMessage 列表（id / author / ts / text 已就绪）。
        """
        short_start = window_end - timedelta(hours=1)
        # 一次 SQL 拿 evidence：JOIN normalized_messages 提取需要的字段
        # 简化版：先按 engagement DESC，再按 random()——这样无 engagement 时
        # 等价随机；有 engagement 时优先取高互动
        # 注：normalized_messages.engagement 也存了一份，避免 JOIN 双倍工作
        # 这里直接走 NormalizedMessage（与 entity_mentions JOIN 保证只取该 entity 提及的消息）
        from sqlalchemy import desc, func

        stmt = (
            select(NormalizedMessage)
            .join(EntityMention, EntityMention.msg_id == NormalizedMessage.id)
            .where(
                EntityMention.entity == entity,
                EntityMention.ts >= short_start,
                EntityMention.ts < window_end,
            )
            .order_by(
                # engagement 高的优先；同 engagement 走 random 打散（避免每次结果一样）
                desc(NormalizedMessage.engagement),
                func.random(),
            )
            .limit(self.evidence_count)
        )
        return list(session.scalars(stmt).all())

    def _build_cooccur_hint(
        self, *, entity: str, window_end: datetime
    ) -> str:
        """
        构造共现 hint 字符串塞进 prompt（spec design §3.5 / requirements 协同）。

        没接 cooccur_repo 或查不到 → 返回 "（暂无）"，prompt 里看到这一段就忽略。
        """
        if self.cooccur_repo is None:
            return "（暂无）"

        # 取最新 24h 共现窗口（与 cooccur_service 写入对齐）
        # 这里简化：用同 window_end 查 24h 窗口 → 共现服务写入时间通常领先 alert 几秒
        try:
            with self.db.get_session() as session:
                neighbors = self.cooccur_repo.fetch_neighbors(
                    session,
                    entity=entity,
                    window_end=window_end,
                    k=5,
                )
        except Exception:
            return "（暂无）"

        if not neighbors:
            return "（暂无）"

        parts: list[str] = []
        for n in neighbors[:3]:  # 最多 3 个邻居避免 hint 过长
            other = n.entity_b if n.entity_a == entity else n.entity_a
            parts.append(f"{other}（PMI {n.pmi:.2f}）")
        return "该实体最近 24h 与以下实体共振：" + "、".join(parts)

    def _render_prompt(
        self,
        *,
        entity: str,
        evidence: list[NormalizedMessage],
        cooccur_hint: str = "（暂无）",
    ) -> str:
        """
        渲染 prompt 模板（spec Req 3）。

        - 加载 self.prompt_path 模板
        - 替换 {entity} / {n_msgs} / {messages} / {cooccur_hint} 占位符
        - 每条 evidence 用 [author @ posted_at] text[:300] 三段拼接
        """
        template = self.prompt_path.read_text(encoding="utf-8")

        msg_block = "\n\n".join(
            f"[{(m.author or 'anon')} @ {m.ts:%Y-%m-%d %H:%M}] "
            f"{(m.text or '').strip()[:_MAX_MSG_TEXT_LEN]}"
            for m in evidence
        )
        return template.format(
            entity=entity,
            n_msgs=len(evidence),
            cooccur_hint=cooccur_hint,
            messages=msg_block,
        )

    def _parse_json(self, response: str) -> dict[str, Any]:
        """
        解析 LLM 返回的 JSON 字符串（spec Req 3 + 5）。

        - response 里可能混杂 markdown 代码块（虽然 prompt 禁止），剥掉
          ```json ... ``` 包裹再 json.loads
        - 解析后做轻量校验：
            - 必须是 dict，否则 raise ValueError
            - sentiment 不在 {bullish, bearish, neutral} → 归一到 None
            - confidence 转 float，不是数字 → None
            - 缺字段不 raise，按 None 处理（spec Req 3.3 fields 都允许 null）
        - 返回标准化 dict（5 个 _EXPECTED_KEYS 全部出现，缺的填 None）

        失败 raise ValueError，由调用方捕获 + log.warning 后跳过（不写表）。
        """
        text = (response or "").strip()
        if not text:
            raise ValueError("LLM 返回空字符串")

        # 剥 markdown 代码块（容错：prompt 已禁止，但 qwen3 偶尔会加）
        if text.startswith("```"):
            # 形式：```json\n{...}\n```  或  ```\n{...}\n```
            lines = text.splitlines()
            if len(lines) >= 2 and lines[-1].strip().startswith("```"):
                text = "\n".join(lines[1:-1]).strip()
            else:
                # 起始有 ``` 但没找到结尾，直接去掉首行
                text = "\n".join(lines[1:]).strip()

        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            # 兜底：本地小模型偶尔会输出 "sentiment": neutral（枚举值漏引号），
            # 在传 JSON Schema 之后通常被 Ollama 屏蔽，但旧版 / 兼容层可能漏掉。
            # 这里只针对已知模式做一次保守修复，命中失败仍按原异常上抛
            repaired = _BARE_ENUM_RE.sub(r'\1"\2"\3', text)
            if repaired != text:
                try:
                    data = json.loads(repaired)
                except json.JSONDecodeError:
                    raise ValueError(f"JSON 解析失败: {e}") from e
            else:
                raise ValueError(f"JSON 解析失败: {e}") from e

        if not isinstance(data, dict):
            raise ValueError(f"期望 JSON object，实际类型 {type(data).__name__}")

        # 标准化字段
        result: dict[str, Any] = {k: None for k in _EXPECTED_KEYS}
        for key in _EXPECTED_KEYS:
            value = data.get(key)
            if key == "sentiment":
                if isinstance(value, str) and value.strip() in _SENTIMENT_VALUES:
                    result[key] = value.strip()
                else:
                    result[key] = None
            elif key == "confidence":
                # 容错：LLM 可能返回 "0.85" 字符串
                if isinstance(value, (int, float)):
                    result[key] = float(value)
                elif isinstance(value, str):
                    try:
                        result[key] = float(value.strip())
                    except ValueError:
                        result[key] = None
                else:
                    result[key] = None
            else:
                # narrative / catalyst / fund_logic：保持 str / None
                if isinstance(value, str) and value.strip():
                    result[key] = value.strip()
                else:
                    result[key] = None
        return result


__all__ = ["BriefingService"]
