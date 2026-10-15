"""SponsorBlock 兼容服务的只读片段查询，失败一律按「没有片段」返回。

``GET /api/skipSegments?videoID&cid&categories`` 返回裸片段数组，
``GET /api/skipSegments/:hashPrefix`` 返回按视频分组的数组，两种都要能吃下。
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx
from nonebot import logger

REQUEST_TIMEOUT = 5.0
"""查询超时，免得第三方实例拖慢整条解析链路"""

CACHE_TTL_SECONDS = 300.0
"""同一条视频的查询结果保留多久"""

_cache: dict[tuple, tuple[float, list]] = {}


class SponsorSegment:
    """一条归一化后的片段"""

    __slots__ = ("action_type", "category", "end", "recorded_duration", "start")

    def __init__(
        self,
        start: float,
        end: float,
        category: str,
        action_type: str,
        recorded_duration: float | None,
    ):
        self.start = start
        self.end = end
        self.category = category
        self.action_type = action_type
        # 提交时记录的视频时长，服务端约定未知时为 0，这里统一记成 None
        self.recorded_duration = recorded_duration


def clear_cache() -> None:
    """丢弃片段缓存"""
    _cache.clear()


def _iter_segment_dicts(payload: Any) -> list[dict[str, Any]]:
    """摊平两种返回形态"""
    if not isinstance(payload, list):
        return []
    entries: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        segments = item.get("segments")
        if isinstance(segments, list):
            entries.extend(seg for seg in segments if isinstance(seg, dict))
        elif isinstance(item.get("segment"), list):
            entries.append(item)
    return entries


def _parse_segment(entry: dict[str, Any]) -> SponsorSegment | None:
    pair = entry.get("segment")
    if not isinstance(pair, (list, tuple)) or len(pair) != 2:
        return None
    try:
        start, end = float(pair[0]), float(pair[1])
    except (TypeError, ValueError):
        return None
    if start < 0 or end <= start:
        return None
    category, action_type = entry.get("category"), entry.get("actionType")
    if not isinstance(category, str) or not isinstance(action_type, str):
        return None
    try:
        recorded = float(entry.get("videoDuration"))
    except (TypeError, ValueError):
        recorded = 0.0
    return SponsorSegment(
        start=start,
        end=end,
        category=category.casefold(),
        action_type=action_type.casefold(),
        recorded_duration=recorded if recorded > 0 else None,
    )


async def fetch_segments(
    base_url: str,
    video_id: str,
    cid: str,
    categories: list[str],
) -> list[SponsorSegment]:
    """查询这条视频标记的片段"""
    if not categories:
        return []
    params = {"videoID": video_id, "cid": cid, "categories": json.dumps(categories)}
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            response = await client.get(
                f"{base_url.rstrip('/')}/api/skipSegments", params=params
            )
    except Exception as e:
        logger.warning(f"SponsorBlock 查询失败 {video_id}: {type(e).__name__}: {e!r}")
        return []

    if response.status_code != 200:
        logger.warning(f"SponsorBlock 返回 HTTP {response.status_code}，忽略片段")
        return []
    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError):
        logger.warning(f"SponsorBlock 返回了非 JSON 内容 for {video_id}，忽略片段")
        return []

    segments: list[SponsorSegment] = []
    for entry in _iter_segment_dicts(payload):
        parsed = _parse_segment(entry)
        if parsed is not None:
            segments.append(parsed)
    return segments


async def lookup_segments(
    base_url: str,
    video_id: str,
    cid: str,
    categories: list[str],
) -> list[SponsorSegment]:
    """带缓存的查询：同一条视频在 TTL 内只打一次服务

    顺带让并发计划拿到同一份片段，避免两个处理任务各算各的。
    """
    key = (base_url, video_id, cid, tuple(categories))
    cached = _cache.get(key)
    if cached is not None and cached[0] > time.monotonic():
        return cached[1]
    segments = await fetch_segments(base_url, video_id, cid, categories)
    if len(_cache) > 256:
        _cache.clear()
    _cache[key] = (time.monotonic() + CACHE_TTL_SECONDS, segments)
    return segments
