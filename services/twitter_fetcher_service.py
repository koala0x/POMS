from __future__ import annotations

"""
Twitter List 抓取服务。

run_once() 流程:
1. 查 DB 拿到最近一批已入库的 tweet_id(早停判断,避免烧 API)
2. 调 twitterapi.io 的 list_timeline 接口,按 cursor 翻页
3. 命中已知 tweet_id 或翻完 max_pages / has_next_page=false 时停止
4. 调 IngestService.ingest("twitter", items) 直接入库,
   IngestService 内部走 ON CONFLICT (tweet_id) DO NOTHING,重复条目自动跳过

由 scheduler/twitter_fetcher_loop.py 在后台线程里定时调用,
不再走 HTTP /ingest,所以也不需要 api_main.py 的 Flask server 在监听。
"""

from datetime import datetime
from typing import Iterator

import requests
from loguru import logger
from sqlalchemy import select

from db.connection import Database
from db.models import TwitterPost
from services.ingest_service import IngestService


TWITTERAPI_IO_ENDPOINT = "https://api.twitterapi.io/twitter/list/tweets_timeline"

# 早停集合的窗口大小:max_pages*20 是单次最多新条目数,500 给 N 倍冗余足够。
_KNOWN_IDS_LOOKBACK = 500

# Twitter 的 createdAt 格式:"Tue Dec 10 07:00:30 +0000 2024"
_TWITTER_DATE_FMT = "%a %b %d %H:%M:%S %z %Y"


def _parse_twitter_created_at(raw: str | None) -> str | None:
    """把 Twitter 的英文日期串转成 ISO 8601;解析失败返回 None,业务侧用 created_at 兜底。"""
    if not raw:
        return None
    try:
        return datetime.strptime(raw, _TWITTER_DATE_FMT).isoformat()
    except ValueError:
        logger.warning("无法解析 Twitter 时间格式: {!r},入库时置为 None", raw)
        return None


def _tweet_to_item(tweet: dict) -> dict | None:
    """
    twitterapi.io 的 tweet 对象 → IngestService 需要的 twitter item 格式。

    必备字段:tweet_id + content。任一缺失返回 None,跳过该条。
    """
    tid = tweet.get("id")
    text = tweet.get("text")
    if not tid or not isinstance(text, str) or not text.strip():
        return None
    author_obj = tweet.get("author") or {}
    author = author_obj.get("userName") if isinstance(author_obj, dict) else None
    return {
        "tweet_id": str(tid),
        "content": text,
        "author": author,
        "posted_at": _parse_twitter_created_at(tweet.get("createdAt")),
    }


class TwitterListFetcherService:
    """
    定时拉某个 Twitter List 的推文,经 IngestService 入库。

    与 main.py 里的 Level1/2 Service 一样,run_once() 是同步阻塞的"一轮",
    由 scheduler 控制触发节奏。run_once() 内部失败时只抛异常,不写库。
    """

    def __init__(
        self,
        db: Database,
        ingest_service: IngestService,
        api_key: str,
        list_id: str,
        max_pages: int,
    ) -> None:
        self._db = db
        self._ingest = ingest_service
        self._api_key = api_key
        self._list_id = list_id
        self._max_pages = max_pages

    def is_configured(self) -> bool:
        """key / list_id 都填了才算配置完成,占位值视为未配置。"""
        return bool(self._api_key) and self._api_key != "YOUR_API_KEY" and bool(self._list_id)

    def run_once(self) -> int:
        """
        跑一轮抓取。返回实际新增到 DB 的行数(经过 ON CONFLICT 去重)。

        异常会向上抛(比如 401 / 网络错),由 loop 层捕获记录,本方法不吞错。
        """
        if not self.is_configured():
            logger.warning(
                "Twitter 抓取未配置(api_key 是占位值或 list_id 为空),跳过本轮"
            )
            return 0

        known_ids = self._load_known_tweet_ids()
        logger.info(
            "Twitter 抓取开始: list_id={} max_pages={} 已知 tweet_id={}",
            self._list_id,
            self._max_pages,
            len(known_ids),
        )

        new_items: list[dict] = []
        hit_known = False

        for tweets in self._fetch_pages():
            if not tweets:
                continue
            for tw in tweets:
                tid = str(tw.get("id") or "")
                if tid and tid in known_ids:
                    # 撞到已入库的最旧一批,后续都是更早的历史推文,没必要再翻
                    hit_known = True
                    logger.info("命中已知 tweet_id={},早停翻页", tid)
                    break
                item = _tweet_to_item(tw)
                if item is not None:
                    new_items.append(item)
            if hit_known:
                break

        if not new_items:
            logger.info("本轮没有新推文,直接返回")
            return 0

        inserted = self._ingest.ingest("twitter", new_items)
        logger.info(
            "Twitter 抓取完成: 收到 {} 条新推文,实际入库 {} 条",
            len(new_items),
            inserted,
        )
        return inserted

    def _load_known_tweet_ids(self) -> set[str]:
        """取最近 _KNOWN_IDS_LOOKBACK 条带 tweet_id 的记录,组装成 set 用于早停命中。"""
        with self._db.get_session() as session:
            stmt = (
                select(TwitterPost.tweet_id)
                .where(TwitterPost.tweet_id.is_not(None))
                .order_by(TwitterPost.id.desc())
                .limit(_KNOWN_IDS_LOOKBACK)
            )
            return {tid for tid in session.scalars(stmt).all() if tid}

    def _fetch_pages(self) -> Iterator[list[dict]]:
        """
        生成器:按 cursor 翻页 yield 每一页的 tweets 数组。

        停止条件:
        - 翻到 max_pages
        - 上游返回 has_next_page=false 或 next_cursor 为空
        - HTTP 非 200(打日志,直接抛出,由 loop 兜底捕获)
        """
        cursor = ""
        headers = {"x-api-key": self._api_key}
        for page_idx in range(1, self._max_pages + 1):
            params = {"listId": self._list_id, "cursor": cursor}
            logger.info("拉取第 {} 页, cursor={!r}", page_idx, cursor)
            resp = requests.get(
                TWITTERAPI_IO_ENDPOINT,
                headers=headers,
                params=params,
                timeout=30,
            )
            if resp.status_code != 200:
                # 把响应体一起打出来,排查 401/403/429 都靠这一行
                logger.error(
                    "twitterapi.io 调用失败: status={} body={!r}",
                    resp.status_code,
                    resp.text[:500],
                )
                resp.raise_for_status()
            body = resp.json()
            yield body.get("tweets") or []

            if not body.get("has_next_page"):
                logger.info("第 {} 页 has_next_page=false,停止翻页", page_idx)
                return
            cursor = body.get("next_cursor") or ""
            if not cursor:
                logger.info("第 {} 页 next_cursor 为空,停止翻页", page_idx)
                return
        logger.info("达到 max_pages={},停止翻页", self._max_pages)
