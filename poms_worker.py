"""
POMS 后台 Worker（持续运行）

功能：
1) 从 PostgreSQL 多个“源表”增量拉取新数据（按自增 id）。
2) 每累计 batch_size（默认 50）条，调用本地 Ollama（千问等模型）做归纳总结，写入归纳表。
3) 每小时对归纳表在该小时内产生的观点做二次提炼，写入小时级归纳表。
4) 使用 poms_state 存储断点（每个源表 last_id、小时窗口进度），支持重启续跑。

配置：
- 支持落地 INI 配置文件（支持注释），例如 poms_worker.ini：
  - CLI：--config /path/to/poms_worker.ini
  - INI 段落：postgres / ollama / worker / sources / source.<name>
- 同时保留环境变量配置；环境变量优先级高于配置文件。
- 环境变量：
  - POMS_PG_DSN / DATABASE_URL：Postgres DSN
  - POMS_OLLAMA_BASE_URL：默认 http://localhost:11434
  - POMS_OLLAMA_MODEL：默认 qwen2.5（以你本机模型名为准）
  - POMS_SOURCES_JSON：可选，JSON 数组，定义任意源表（见 _load_sources_from_env）
  - 也可用 POMS_GMGN_* / POMS_BN_* 覆盖默认两张表的表名/列名
"""

import argparse
import configparser
import json
import logging
import os
import re
import signal
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional


_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _quote_ident(identifier: str) -> str:
    # 仅允许安全的 SQL 标识符（表名/列名），避免被环境变量或配置注入 SQL。
    if not _IDENT_RE.match(identifier):
        raise ValueError(f"Invalid SQL identifier: {identifier!r}")
    return f'"{identifier}"'


def _utc_now() -> datetime:
    # 统一使用 UTC，避免跨时区的小时窗口边界问题。
    return datetime.now(timezone.utc)


def _floor_to_hour(ts: datetime) -> datetime:
    # 向下取整到整点，用于小时窗口边界计算。
    return ts.replace(minute=0, second=0, microsecond=0)


def _ceil_to_next_hour(ts: datetime) -> datetime:
    # 向上取整到下一个整点，用于调度“每小时”任务。
    floored = _floor_to_hour(ts)
    if ts == floored:
        return ts
    return floored + timedelta(hours=1)


def _loads_json(env_value: str) -> Any:
    # 环境变量里读取 JSON 配置（例如 POMS_SOURCES_JSON）。
    return json.loads(env_value)


def _split_csv(value: str) -> list[str]:
    # INI 中常用逗号分隔写法：a,b,c
    parts = [p.strip() for p in (value or "").split(",")]
    return [p for p in parts if p]


def _parse_bool(value: str, default: bool) -> bool:
    v = (value or "").strip().lower()
    if not v:
        return default
    if v in {"1", "true", "yes", "y", "on"}:
        return True
    if v in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _get_ini(cfg: configparser.ConfigParser, section: str, option: str) -> Optional[str]:
    if not cfg.has_section(section):
        return None
    if not cfg.has_option(section, option):
        return None
    value = cfg.get(section, option)
    value = value.strip()
    return value if value else None


def _load_ini_config(path: str) -> dict[str, Any]:
    # 读取 INI 配置文件（允许 # / ; 注释）。
    cfg = configparser.ConfigParser(interpolation=None)
    with open(path, "r", encoding="utf-8") as f:
        cfg.read_file(f)

    enabled_raw = _get_ini(cfg, "sources", "enabled")
    enabled = _split_csv(enabled_raw) if enabled_raw else []

    sources: list[SourceConfig] = []
    for section in cfg.sections():
        if not section.startswith("source."):
            continue
        name = section.removeprefix("source.").strip()
        if not name:
            continue
        if enabled and name not in enabled:
            continue
        table = _get_ini(cfg, section, "table")
        if not table:
            continue
        id_column = _get_ini(cfg, section, "id_column") or "id"
        text_column = _get_ini(cfg, section, "text_column") or "content"
        created_at_column = _get_ini(cfg, section, "created_at_column")
        sources.append(
            SourceConfig(
                name=name,
                table=table,
                id_column=id_column,
                text_column=text_column,
                created_at_column=created_at_column,
            )
        )

    hourly_enabled = _parse_bool(_get_ini(cfg, "worker", "hourly_enabled") or "", default=True)
    worker_conf: dict[str, Any] = {
        "batch_size": int(_get_ini(cfg, "worker", "batch_size") or "50"),
        "poll_interval_s": float(_get_ini(cfg, "worker", "poll_interval_s") or "5"),
        "fetch_limit": int(_get_ini(cfg, "worker", "fetch_limit") or "200"),
        "hourly_enabled": hourly_enabled,
    }

    result: dict[str, Any] = {
        "pg_dsn": _get_ini(cfg, "postgres", "dsn"),
        "ollama_base_url": _get_ini(cfg, "ollama", "base_url"),
        "ollama_model": _get_ini(cfg, "ollama", "model"),
        "ollama_timeout_s": float(_get_ini(cfg, "ollama", "timeout_s") or "120"),
        "worker": worker_conf,
        "sources": sources,
    }
    return result


def _http_post_json(url: str, payload: dict[str, Any], timeout_s: float) -> dict[str, Any]:
    # 轻量 HTTP 客户端：只用标准库调用 Ollama 的 JSON API（不引入第三方依赖）。
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        raise RuntimeError(f"HTTP {exc.code} from {url}: {body}") from exc


@dataclass(frozen=True)
class SourceConfig:
    # 单个“源表”的结构描述：用来做增量拉取和拼接提示词。
    name: str
    table: str
    id_column: str
    text_column: str
    created_at_column: Optional[str] = None

    def validate(self) -> "SourceConfig":
        # 在运行前校验表名/列名是安全标识符，避免 SQL 注入。
        _quote_ident(self.name.replace("-", "_"))
        _quote_ident(self.table)
        _quote_ident(self.id_column)
        _quote_ident(self.text_column)
        if self.created_at_column is not None:
            _quote_ident(self.created_at_column)
        return self


class Db:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._conn = None
        self._driver = None

    def connect(self) -> None:
        # 兼容 psycopg(v3) 与 psycopg2：优先 v3，缺失则回退到 v2。
        if self._conn is not None:
            return
        try:
            import psycopg

            self._driver = "psycopg"
            self._conn = psycopg.connect(self._dsn)
            self._conn.autocommit = False
            return
        except Exception:
            pass

        try:
            import psycopg2

            self._driver = "psycopg2"
            self._conn = psycopg2.connect(self._dsn)
            self._conn.autocommit = False
            return
        except Exception as exc:
            raise RuntimeError(
                "缺少 PostgreSQL 驱动：请安装 psycopg(v3) 或 psycopg2。\n"
                "常用方式：\n"
                "- pip install 'psycopg[binary]'\n"
                "- 或 pip install psycopg2-binary\n"
                "如果你在 Debian/Ubuntu：\n"
                "- sudo apt-get install python3-psycopg2\n"
            ) from exc

    def close(self) -> None:
        # 正常退出时关闭连接。
        if self._conn is None:
            return
        try:
            self._conn.close()
        finally:
            self._conn = None

    def cursor(self):
        # 简单封装 cursor，避免在业务逻辑里到处判断连接是否建立。
        if self._conn is None:
            raise RuntimeError("DB is not connected")
        return self._conn.cursor()

    def commit(self) -> None:
        # 每轮循环成功后提交；失败则回滚。
        if self._conn is None:
            raise RuntimeError("DB is not connected")
        self._conn.commit()

    def rollback(self) -> None:
        if self._conn is None:
            raise RuntimeError("DB is not connected")
        self._conn.rollback()


class OllamaClient:
    def __init__(self, base_url: str, model: str, timeout_s: float) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout_s = timeout_s

    def chat(self, system_prompt: str, user_prompt: str) -> str:
        # 优先调用 /api/chat；失败或返回结构不符合预期时，回退到 /api/generate。
        chat_url = f"{self._base_url}/api/chat"
        payload = {
            "model": self._model,
            "stream": False,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        try:
            response = _http_post_json(chat_url, payload, timeout_s=self._timeout_s)
            message = response.get("message") or {}
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                return content.strip()
        except Exception:
            pass

        generate_url = f"{self._base_url}/api/generate"
        payload = {
            "model": self._model,
            "stream": False,
            "prompt": f"{system_prompt}\n\n{user_prompt}".strip(),
        }
        response = _http_post_json(generate_url, payload, timeout_s=self._timeout_s)
        content = response.get("response")
        if isinstance(content, str) and content.strip():
            return content.strip()
        raise RuntimeError("Ollama returned empty response")


def _ensure_tables(db: Db) -> None:
    # 在目标数据库里创建 worker 需要的内部表（状态表、归纳表、小时归纳表）。
    db.connect()
    cur = db.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS poms_state (
            key TEXT PRIMARY KEY,
            value JSONB NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS poms_summaries (
            id BIGSERIAL PRIMARY KEY,
            source TEXT NOT NULL,
            item_count INT NOT NULL,
            start_id BIGINT,
            end_id BIGINT,
            raw_ids JSONB NOT NULL,
            summary_text TEXT NOT NULL,
            model TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS poms_hourly_summaries (
            id BIGSERIAL PRIMARY KEY,
            window_start TIMESTAMPTZ NOT NULL,
            window_end TIMESTAMPTZ NOT NULL,
            included_summary_ids JSONB NOT NULL,
            summary_text TEXT NOT NULL,
            model TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    db.commit()


def _state_get(db: Db, key: str) -> Optional[dict[str, Any]]:
    # 读取断点状态（JSONB）。
    cur = db.cursor()
    cur.execute("SELECT value FROM poms_state WHERE key = %s", (key,))
    row = cur.fetchone()
    if not row:
        return None
    value = row[0]
    if isinstance(value, str):
        return json.loads(value)
    return value


def _state_set(db: Db, key: str, value: dict[str, Any]) -> None:
    # 写入断点状态（UPSERT）。
    cur = db.cursor()
    cur.execute(
        """
        INSERT INTO poms_state(key, value, updated_at)
        VALUES (%s, %s::jsonb, NOW())
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
        """,
        (key, json.dumps(value, ensure_ascii=False)),
    )


def _fetch_new_rows(
    db: Db,
    source: SourceConfig,
    last_id: int,
    limit: int,
) -> list[dict[str, Any]]:
    # 按 id 递增增量拉取，避免重复处理。
    table = _quote_ident(source.table)
    id_col = _quote_ident(source.id_column)
    text_col = _quote_ident(source.text_column)

    if source.created_at_column:
        created_col = _quote_ident(source.created_at_column)
        sql = (
            f"SELECT {id_col}, {created_col}, {text_col} "
            f"FROM {table} WHERE {id_col} > %s ORDER BY {id_col} ASC LIMIT %s"
        )
        cur = db.cursor()
        cur.execute(sql, (last_id, limit))
        rows = cur.fetchall() or []
        items: list[dict[str, Any]] = []
        for row in rows:
            items.append({"id": int(row[0]), "created_at": row[1], "text": row[2]})
        return items

    sql = (
        f"SELECT {id_col}, {text_col} "
        f"FROM {table} WHERE {id_col} > %s ORDER BY {id_col} ASC LIMIT %s"
    )
    cur = db.cursor()
    cur.execute(sql, (last_id, limit))
    rows = cur.fetchall() or []
    items = []
    for row in rows:
        items.append({"id": int(row[0]), "created_at": None, "text": row[1]})
    return items


def _build_batch_prompt(source_name: str, items: list[dict[str, Any]]) -> str:
    # 把 50 条（或 batch_size 条）原始数据拼成单次大模型输入。
    lines: list[str] = []
    lines.append(f"数据源：{source_name}")
    lines.append(f"条数：{len(items)}")
    lines.append("")
    for idx, item in enumerate(items, start=1):
        created_at = item.get("created_at")
        created_text = ""
        if created_at is not None:
            created_text = f"时间：{created_at}；"
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        lines.append(f"{idx}. {created_text}{text}")
    lines.append("")
    lines.append("请完成：")
    lines.append("1) 用要点形式总结这批信息，去重、合并同义表达。")
    lines.append("2) 提炼：市场情绪/潜在催化剂/风险点/可执行观察点（如果有）。")
    lines.append("3) 输出中文，尽量简洁，最多15条要点。")
    return "\n".join(lines).strip()


def _build_hourly_prompt(window_start: datetime, window_end: datetime, summaries: list[dict[str, Any]]) -> str:
    # 把一个小时内的归纳结果再拼成二次提炼输入。
    lines: list[str] = []
    lines.append(f"时间窗口：{window_start.isoformat()} ~ {window_end.isoformat()}")
    lines.append(f"归纳条目数：{len(summaries)}")
    lines.append("")
    for idx, s in enumerate(summaries, start=1):
        created_at = s.get("created_at")
        created_text = f"（{created_at}）" if created_at else ""
        text = str(s.get("summary_text") or "").strip()
        if not text:
            continue
        lines.append(f"{idx}. {created_text}{text}")
    lines.append("")
    lines.append("请完成：")
    lines.append("1) 在不丢关键信息的前提下进一步去重、压缩、合并。")
    lines.append("2) 按“主线观点/潜在机会/主要风险/需要继续跟踪”四类组织输出。")
    lines.append("3) 输出中文，尽量精简。")
    return "\n".join(lines).strip()


def _insert_summary(
    db: Db,
    source: SourceConfig,
    items: list[dict[str, Any]],
    summary_text: str,
    model: str,
) -> int:
    # 写入“批次归纳”表：包含原始 id 范围、原始 id 列表与模型输出。
    raw_ids = [int(item["id"]) for item in items if "id" in item]
    start_id = min(raw_ids) if raw_ids else None
    end_id = max(raw_ids) if raw_ids else None

    cur = db.cursor()
    cur.execute(
        """
        INSERT INTO poms_summaries(source, item_count, start_id, end_id, raw_ids, summary_text, model)
        VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s)
        RETURNING id
        """,
        (
            source.name,
            len(raw_ids),
            start_id,
            end_id,
            json.dumps(raw_ids),
            summary_text,
            model,
        ),
    )
    row = cur.fetchone()
    return int(row[0]) if row else 0


def _select_summaries_for_window(db: Db, window_start: datetime, window_end: datetime) -> list[dict[str, Any]]:
    # 获取一个小时窗口内写入的归纳条目，用于二次提炼。
    cur = db.cursor()
    cur.execute(
        """
        SELECT id, summary_text, created_at, source
        FROM poms_summaries
        WHERE created_at >= %s AND created_at < %s
        ORDER BY created_at ASC, id ASC
        """,
        (window_start, window_end),
    )
    rows = cur.fetchall() or []
    results: list[dict[str, Any]] = []
    for row in rows:
        results.append(
            {"id": int(row[0]), "summary_text": row[1], "created_at": row[2], "source": row[3]}
        )
    return results


def _insert_hourly_summary(
    db: Db,
    window_start: datetime,
    window_end: datetime,
    included_summary_ids: list[int],
    summary_text: str,
    model: str,
) -> int:
    # 写入“小时归纳”表：记录窗口与纳入的 summary id，便于追溯。
    cur = db.cursor()
    cur.execute(
        """
        INSERT INTO poms_hourly_summaries(window_start, window_end, included_summary_ids, summary_text, model)
        VALUES (%s, %s, %s::jsonb, %s, %s)
        RETURNING id
        """,
        (
            window_start,
            window_end,
            json.dumps(included_summary_ids),
            summary_text,
            model,
        ),
    )
    row = cur.fetchone()
    return int(row[0]) if row else 0


class Worker:
    def __init__(
        self,
        db: Db,
        ollama: OllamaClient,
        sources: list[SourceConfig],
        batch_size: int,
        poll_interval_s: float,
        fetch_limit: int,
        hourly_enabled: bool,
    ) -> None:
        # 每个源表维护一个缓冲区：先累积，再按 batch_size 切片处理。
        self._db = db
        self._ollama = ollama
        self._sources = [s.validate() for s in sources]
        self._batch_size = batch_size
        self._poll_interval_s = poll_interval_s
        self._fetch_limit = max(fetch_limit, batch_size)
        self._hourly_enabled = hourly_enabled
        self._stop = False
        self._buffers: dict[str, list[dict[str, Any]]] = {s.name: [] for s in self._sources}

    def request_stop(self) -> None:
        # 用信号触发优雅退出：本轮循环结束后停止。
        self._stop = True

    def run_forever(self) -> None:
        # 主循环：拉取增量、批量归纳、按小时二次提炼；循环内提交，异常则回滚。
        self._db.connect()
        _ensure_tables(self._db)
        self._db.connect()

        next_hourly_at = _ceil_to_next_hour(_utc_now())
        log = logging.getLogger("poms.worker")

        while not self._stop:
            loop_started_at = time.time()
            try:
                self._run_sources_once()
                if self._hourly_enabled:
                    now = _utc_now()
                    if now >= next_hourly_at:
                        self._run_hourly_until(_floor_to_hour(now))
                        next_hourly_at = _ceil_to_next_hour(_utc_now())
                self._db.commit()
            except Exception:
                self._db.rollback()
                log.exception("Loop error; rolled back")

            elapsed = time.time() - loop_started_at
            sleep_s = max(0.0, self._poll_interval_s - elapsed)
            if sleep_s > 0:
                time.sleep(sleep_s)

        try:
            self._db.commit()
        except Exception:
            self._db.rollback()
        self._db.close()

    def _run_sources_once(self) -> None:
        # 扫描每个源表：拉取新行 -> 放入缓冲 -> 满 batch 即归纳入库。
        log = logging.getLogger("poms.worker")
        for source in self._sources:
            state_key = f"source:{source.name}"
            state = _state_get(self._db, state_key) or {}
            last_id = int(state.get("last_id") or 0)

            new_items = _fetch_new_rows(self._db, source, last_id=last_id, limit=self._fetch_limit)
            if new_items:
                self._buffers[source.name].extend(new_items)
                state["last_id"] = max(last_id, max(int(i["id"]) for i in new_items))
                _state_set(self._db, state_key, state)
                log.info(
                    "Fetched %s new rows from %s (last_id=%s)",
                    len(new_items),
                    source.name,
                    state["last_id"],
                )

            buffer_items = self._buffers[source.name]
            while len(buffer_items) >= self._batch_size:
                batch = buffer_items[: self._batch_size]
                del buffer_items[: self._batch_size]
                self._summarize_and_store(source, batch)

    def _summarize_and_store(self, source: SourceConfig, items: list[dict[str, Any]]) -> None:
        # 对单个 batch 调用大模型，写入归纳表。
        log = logging.getLogger("poms.worker")
        system_prompt = "你是加密市场信息分析助手。你的目标是从噪声中提炼高信号，避免幻想，不确定要标注。"
        user_prompt = _build_batch_prompt(source.name, items)
        summary = self._ollama.chat(system_prompt=system_prompt, user_prompt=user_prompt)
        summary_id = _insert_summary(self._db, source, items, summary_text=summary, model=self._ollama._model)
        log.info("Inserted summary id=%s source=%s items=%s", summary_id, source.name, len(items))

    def _run_hourly_until(self, window_end: datetime) -> None:
        # 处理到指定整点：读取上次窗口结束时间，生成 [window_start, window_end) 的二次提炼结果并保存进度。
        log = logging.getLogger("poms.worker")
        state_key = "hourly:last_window_end"
        state = _state_get(self._db, state_key) or {}
        last_end_raw = state.get("window_end")
        if isinstance(last_end_raw, str):
            window_start = datetime.fromisoformat(last_end_raw)
            if window_start.tzinfo is None:
                window_start = window_start.replace(tzinfo=timezone.utc)
        else:
            window_start = window_end - timedelta(hours=1)

        window_start = _floor_to_hour(window_start.astimezone(timezone.utc))
        window_end = _floor_to_hour(window_end.astimezone(timezone.utc))

        if window_end <= window_start:
            return

        summaries = _select_summaries_for_window(self._db, window_start, window_end)
        included_ids = [int(s["id"]) for s in summaries]

        system_prompt = "你是加密市场信息分析助手。你要把已有归纳进一步压缩成结构化要点，去重合并。"
        user_prompt = _build_hourly_prompt(window_start, window_end, summaries)
        hourly_summary_text = self._ollama.chat(system_prompt=system_prompt, user_prompt=user_prompt)
        hourly_id = _insert_hourly_summary(
            self._db,
            window_start=window_start,
            window_end=window_end,
            included_summary_ids=included_ids,
            summary_text=hourly_summary_text,
            model=self._ollama._model,
        )

        state["window_end"] = window_end.isoformat()
        _state_set(self._db, state_key, state)
        log.info(
            "Inserted hourly summary id=%s window=%s~%s included=%s",
            hourly_id,
            window_start.isoformat(),
            window_end.isoformat(),
            len(included_ids),
        )


def _default_sources() -> list[SourceConfig]:
    # 默认两张源表（GMGN 推特、币安广场新闻）；列名/表名可用环境变量覆盖。
    return [
        SourceConfig(
            name="gmgn_twitter",
            table=os.getenv("POMS_GMGN_TABLE", "gmgn_tweets"),
            id_column=os.getenv("POMS_GMGN_ID_COL", "id"),
            text_column=os.getenv("POMS_GMGN_TEXT_COL", "content"),
            created_at_column=os.getenv("POMS_GMGN_CREATED_AT_COL", "created_at"),
        ),
        SourceConfig(
            name="binance_square_news",
            table=os.getenv("POMS_BN_TABLE", "binance_square_news"),
            id_column=os.getenv("POMS_BN_ID_COL", "id"),
            text_column=os.getenv("POMS_BN_TEXT_COL", "content"),
            created_at_column=os.getenv("POMS_BN_CREATED_AT_COL", "created_at"),
        ),
    ]


def _load_sources_from_env() -> list[SourceConfig]:
    # 通过 POMS_SOURCES_JSON 配置多个源表：
    # [
    #   {"name":"x","table":"t","id_column":"id","text_column":"content","created_at_column":"created_at"},
    #   ...
    # ]
    raw = os.getenv("POMS_SOURCES_JSON")
    if not raw:
        return _default_sources()
    parsed = _loads_json(raw)
    if not isinstance(parsed, list):
        raise ValueError("POMS_SOURCES_JSON must be a JSON list")
    sources: list[SourceConfig] = []
    for item in parsed:
        if not isinstance(item, dict):
            raise ValueError("Each source config must be an object")
        sources.append(
            SourceConfig(
                name=str(item["name"]),
                table=str(item["table"]),
                id_column=str(item.get("id_column", "id")),
                text_column=str(item.get("text_column", "content")),
                created_at_column=item.get("created_at_column"),
            )
        )
    return sources


def _resolve_sources(ini_sources: list[SourceConfig]) -> list[SourceConfig]:
    # 解析优先级：POMS_SOURCES_JSON（环境变量） > INI 文件 > 默认两张表（可由 POMS_GMGN_* / POMS_BN_* 覆盖）
    raw = os.getenv("POMS_SOURCES_JSON")
    if raw:
        return _load_sources_from_env()
    if ini_sources:
        return ini_sources
    return _default_sources()


def _configure_logging() -> None:
    # 统一日志格式，便于以 systemd/docker 等方式运行时收集。
    level_name = os.getenv("POMS_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )


def main(argv: Optional[list[str]] = None) -> int:
    # CLI：init-db 只建表；run 进入 worker 主循环。
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config", type=str, default=os.getenv("POMS_CONFIG", ""))
    pre_args, _ = pre.parse_known_args(argv)
    config_path = (pre_args.config or "").strip() or None

    ini_conf: dict[str, Any] = {}
    if config_path:
        ini_conf = _load_ini_config(config_path)

    worker_defaults = (ini_conf.get("worker") or {}) if isinstance(ini_conf.get("worker"), dict) else {}

    parser = argparse.ArgumentParser(prog="poms-worker")
    parser.add_argument("--config", type=str, default=config_path or "")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run")
    run.add_argument(
        "--batch-size",
        type=int,
        default=int(os.getenv("POMS_BATCH_SIZE", str(worker_defaults.get("batch_size", 50)))),
    )
    run.add_argument(
        "--poll-interval",
        type=float,
        default=float(os.getenv("POMS_POLL_INTERVAL_S", str(worker_defaults.get("poll_interval_s", 5)))),
    )
    run.add_argument(
        "--fetch-limit",
        type=int,
        default=int(os.getenv("POMS_FETCH_LIMIT", str(worker_defaults.get("fetch_limit", 200)))),
    )
    default_hourly = bool(worker_defaults.get("hourly_enabled", True))
    run.add_argument("--no-hourly", action="store_true", default=not default_hourly)

    init_db = sub.add_parser("init-db")
    check_db = sub.add_parser("check-db")

    args = parser.parse_args(argv)
    _configure_logging()

    dsn = os.getenv("POMS_PG_DSN") or os.getenv("DATABASE_URL") or (ini_conf.get("pg_dsn") if ini_conf else None)
    if not dsn:
        raise SystemExit("Missing POMS_PG_DSN (or DATABASE_URL), e.g. postgresql://user:pass@host:5432/db")

    db = Db(dsn=dsn)

    if args.cmd == "init-db":
        # 建表前先确保能连上数据库。
        try:
            db.connect()
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from exc
        _ensure_tables(db)
        db.close()
        return 0

    if args.cmd == "check-db":
        # 数据库连通性测试：只执行一个最小查询，成功即返回 0。
        try:
            db.connect()
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from exc
        cur = db.cursor()
        cur.execute("SELECT 1")
        _ = cur.fetchone()
        db.close()
        print("DB OK")
        return 0

    db.connect()

    base_url = os.getenv("POMS_OLLAMA_BASE_URL") or (ini_conf.get("ollama_base_url") if ini_conf else None) or "http://localhost:11434"
    model = os.getenv("POMS_OLLAMA_MODEL") or (ini_conf.get("ollama_model") if ini_conf else None) or "qwen2.5"
    timeout_s = float(os.getenv("POMS_OLLAMA_TIMEOUT_S") or (ini_conf.get("ollama_timeout_s") if ini_conf else 120) or 120)
    ini_sources = ini_conf.get("sources") if ini_conf else None
    sources = _resolve_sources(ini_sources if isinstance(ini_sources, list) else [])

    worker = Worker(
        db=db,
        ollama=OllamaClient(base_url=base_url, model=model, timeout_s=timeout_s),
        sources=sources,
        batch_size=args.batch_size,
        poll_interval_s=args.poll_interval,
        fetch_limit=args.fetch_limit,
        hourly_enabled=not args.no_hourly,
    )

    def _handle_signal(_sig: int, _frame: Any) -> None:
        logging.getLogger("poms.worker").info("Received stop signal")
        worker.request_stop()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    worker.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
