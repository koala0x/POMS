"""
拉取 Twitter List 推文 → POST /ingest 入库。

流程:
1. 查 DB 拿到最近一批已入库的 tweet_id(用于翻页早停,避免烧 API)
2. 调 twitterapi.io 的 list_timeline 接口,按 cursor 翻页
3. 每页过滤出"未入库"的推文,拼成 /ingest 需要的 items
4. 命中已知 tweet_id 或翻完 max_pages / has_next_page=false 时停止
5. 批量 POST 到 /ingest,由 IngestService 走 ON CONFLICT DO NOTHING 兜底去重

用法:
    ./.venv/bin/python scripts/fetch_twitter_list.py
    # 也可以临时覆盖 list_id / max_pages:
    ./.venv/bin/python scripts/fetch_twitter_list.py --list-id 1898760983553974442 --max-pages 5

部署:挂到 cron / systemd timer 定时跑(API 接入服务必须先起来)。
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterator

import requests
from loguru import logger
from sqlalchemy import select

# 让脚本可以直接 `python scripts/fetch_twitter_list.py` 跑,加 ROOT 到 sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config.settings import get_settings  # noqa: E402
from db.connection import Database  # noqa: E402
from db.models import TwitterPost  # noqa: E402


TWITTERAPI_IO_ENDPOINT = "https://api.twitterapi.io/twitter/list/tweets_timeline"

# 早停时,我们需要一个"最近已入库 tweet_id"的小集合做命中判断。
# 取 500 条足够覆盖 max_pages*20 的窗口,且查询代价很小。
_KNOWN_IDS_LOOKBACK = 500

# Twitter 的 createdAt 格式:"Tue Dec 10 07:00:30 +0000 2024"
_TWITTER_DATE_FMT = "%a %b %d %H:%M:%S %z %Y"


def _parse_twitter_created_at(raw: str | None) -> str | None:
    """
    把 Twitter 的英文日期串转成 ISO 8601,/ingest 那边能直接 fromisoformat。
    解析失败就返回 None,/ingest 侧会接受 None 并用 created_at 兜底。
    """
    if not raw:
        return None
    try:
        return datetime.strptime(raw, _TWITTER_DATE_FMT).isoformat()
    except ValueError:
        logger.warning("无法解析 Twitter 时间格式: {!r},入库时置为 None", raw)
        return None


def _load_known_tweet_ids(db: Database, limit: int = _KNOWN_IDS_LOOKBACK) -> set[str]:
    """
    取最近 limit 条已入库且 tweet_id 非空的记录,组装成 set 供命中判断用。

    用 id 倒序(自增主键 = 入库顺序的代理),拿"最近一批"已知 tweet_id。
    DB 全空时返回空 set,首跑就老老实实翻满 max_pages。
    """
    with db.get_session() as session:
        stmt = (
            select(TwitterPost.tweet_id)
            .where(TwitterPost.tweet_id.is_not(None))
            .order_by(TwitterPost.id.desc())
            .limit(limit)
        )
        return {tid for tid in session.scalars(stmt).all() if tid}


def _fetch_pages(
    api_key: str, list_id: str, max_pages: int
) -> Iterator[list[dict]]:
    """
    生成器:按 cursor 翻页 yield 每一页的 tweets 数组。

    停止条件:
    - 翻到 max_pages
    - 上游返回 has_next_page=false 或 next_cursor 为空
    - HTTP 非 200(打日志,直接抛出)
    """
    cursor = ""
    headers = {"x-api-key": api_key}
    for page_idx in range(1, max_pages + 1):
        params = {"listId": list_id, "cursor": cursor}
        logger.info("拉取第 {} 页,cursor={!r}", page_idx, cursor)
        resp = requests.get(
            TWITTERAPI_IO_ENDPOINT, headers=headers, params=params, timeout=30
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
        tweets = body.get("tweets") or []
        yield tweets

        if not body.get("has_next_page"):
            logger.info("第 {} 页 has_next_page=false,停止翻页", page_idx)
            return
        cursor = body.get("next_cursor") or ""
        if not cursor:
            logger.info("第 {} 页 next_cursor 为空,停止翻页", page_idx)
            return
    logger.info("达到 max_pages={},停止翻页", max_pages)


def _tweet_to_ingest_item(tweet: dict) -> dict | None:
    """
    把 twitterapi.io 的 tweet 对象转成 /ingest 的 twitter item 格式。

    必备字段:tweet_id(用于去重)+ content(必填)。任一缺失返回 None,跳过。
    author / posted_at 都是软字段,允许 None。
    """
    tid = tweet.get("id")
    text = tweet.get("text")
    if not tid or not isinstance(text, str) or not text.strip():
        # 极少见但确实出现过:Twitter 偶尔返回空 text 的占位行,直接丢弃
        return None
    author_obj = tweet.get("author") or {}
    author = author_obj.get("userName") if isinstance(author_obj, dict) else None
    return {
        "tweet_id": str(tid),
        "content": text,
        "author": author,
        "posted_at": _parse_twitter_created_at(tweet.get("createdAt")),
    }


def _post_to_ingest(ingest_url: str, items: list[dict]) -> int:
    """批量 POST 到 /ingest,返回服务端实际写入行数(经过 ON CONFLICT 去重)。"""
    if not items:
        return 0
    resp = requests.post(
        ingest_url,
        json={"source": "twitter", "items": items},
        timeout=60,
    )
    if resp.status_code != 200:
        logger.error(
            "/ingest 调用失败: status={} body={!r}", resp.status_code, resp.text[:500]
        )
        resp.raise_for_status()
    body = resp.json()
    inserted = int(body.get("inserted") or 0)
    logger.info("/ingest 返回 inserted={} (本批 items={})", inserted, len(items))
    return inserted


def run(list_id: str, api_key: str, max_pages: int, ingest_url: str) -> None:
    settings = get_settings()
    db = Database(settings)

    if api_key in ("", "YOUR_API_KEY"):
        logger.error("twitterapi_io_key 没填,先去 settings.py 改成真实 key 再跑")
        sys.exit(2)

    known_ids = _load_known_tweet_ids(db)
    logger.info("已加载 {} 条已知 tweet_id 用于早停", len(known_ids))

    new_items: list[dict] = []
    hit_known = False

    for tweets in _fetch_pages(api_key, list_id, max_pages):
        if not tweets:
            continue
        for tw in tweets:
            tid = str(tw.get("id") or "")
            if tid and tid in known_ids:
                # 撞到已经入库的最旧一批,后续都是更早的历史推文,没必要再翻
                hit_known = True
                logger.info("命中已知 tweet_id={},早停翻页", tid)
                break
            item = _tweet_to_ingest_item(tw)
            if item is not None:
                new_items.append(item)
        if hit_known:
            break

    if not new_items:
        logger.info("本次没有新推文,直接退出")
        return

    inserted = _post_to_ingest(ingest_url, new_items)
    logger.info("抓取完成: list_id={} 收到 {} 条新推文,实际入库 {} 条",
                list_id, len(new_items), inserted)


def _parse_args() -> argparse.Namespace:
    s = get_settings()
    p = argparse.ArgumentParser(description="拉取 Twitter List 推文并入库")
    p.add_argument("--list-id", default=s.twitter_list_id,
                   help=f"Twitter List ID,默认 {s.twitter_list_id}")
    p.add_argument("--max-pages", type=int, default=s.twitter_list_max_pages,
                   help=f"最多翻多少页,默认 {s.twitter_list_max_pages}")
    p.add_argument("--ingest-url", default=s.ingest_url,
                   help=f"/ingest 地址,默认 {s.ingest_url}")
    return p.parse_args()


def main() -> None:
    # 脚本独立跑,直接把日志打到 stdout 即可,不和服务共用 log 文件
    logger.remove()
    logger.add(sys.stdout, level="INFO", backtrace=False, diagnose=False)

    args = _parse_args()
    settings = get_settings()
    run(
        list_id=args.list_id,
        api_key=settings.twitterapi_io_key,
        max_pages=args.max_pages,
        ingest_url=args.ingest_url,
    )


if __name__ == "__main__":
    main()
