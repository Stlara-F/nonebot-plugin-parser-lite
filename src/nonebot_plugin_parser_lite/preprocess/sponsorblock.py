"""SponsorBlock 兼容服务的只读片段查询。

只使用公开的 skipSegments 接口，不提交、不投票；任何异常都按「没有片段」
处理，让上传链路退回原始文件。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from nonebot import logger

REQUEST_TIMEOUT = 5.0
"""片段查询的硬超时，避免第三方实例拖慢整条解析链路"""


class SponsorSegment:
    """一条已归一化的片段"""

    __slots__ = ("action_type", "category", "end", "start")

    def __init__(self, start: float, end: float, category: str, action_type: str):
        self.start = start
        self.end = end
        self.category = category
        self.action_type = action_type


def _iter_segment_dicts(payload: Any) -> list[dict[str, Any]]:
    """兼容两种返回形态：裸片段数组，以及按视频分组的数组"""
    if not isinstance(payload, list):
        return []
    raw: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        segments = item.get("segments")
        if isinstance(segments, list):
            raw.extend(seg for seg in segments if isinstance(seg, dict))
        elif isinstance(item.get("segment"), list):
            raw.append(item)
    return raw


def _parse_segment(entry: dict[str, Any]) -> tuple[SponsorSegment, float | None] | None:
    pair = entry.get("segment")
    if not isinstance(pair, (list, tuple)) or len(pair) != 2:
        return None
    try:
        start, end = float(pair[0]), float(pair[1])
    except (TypeError, ValueError):
        return None
    if start < 0 or end <= start:
        return None
    category = entry.get("category")
    action_type = entry.get("actionType")
    if not isinstance(category, str) or not isinstance(action_type, str):
        return None
    recorded_raw = entry.get("videoDuration")
    try:
        recorded: float | None = float(recorded_raw)
    except (TypeError, ValueError):
        recorded = None
    return (
        SponsorSegment(
            start=start,
            end=end,
            category=category.casefold(),
            action_type=action_type.casefold(),
        ),
        recorded,
    )


async def fetch_segments(
    base_url: str,
    video_id: str,
    cid: str,
    categories: list[str],
) -> tuple[list[SponsorSegment], dict[str, float | None]]:
    """查询片段的跳过/消音标记

    :return: 片段列表，以及 ``分类 -> 提交时记录的时长``（秒，缺失时为 None）
    """
    if not categories:
        return [], {}
    params = {
        "videoID": video_id,
        "cid": cid,
        "categories": json.dumps(categories),
    }
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            response = await client.get(f"{base_url}/api/skipSegments", params=params)
    except Exception as e:
        logger.debug(f"SponsorBlock 查询失败 {video_id}: {type(e).__name__}: {e!r}")
        return [], {}

    if response.status_code == 404:
        return [], {}
    if response.status_code >= 400:
        logger.debug(
            f"SponsorBlock 返回 {response.status_code} for {video_id}，忽略片段"
        )
        return [], {}

    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError):
        logger.debug(f"SponsorBlock 返回了非 JSON 内容 for {video_id}，忽略片段")
        return [], {}

    segments: list[SponsorSegment] = []
    recorded_durations: dict[str, float | None] = {}
    for entry in _iter_segment_dicts(payload):
        parsed = _parse_segment(entry)
        if parsed is None:
            continue
        segment, recorded = parsed
        segments.append(segment)
        recorded_durations.setdefault(segment.category, recorded)
    return segments, recorded_durations
