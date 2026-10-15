"""上传前预处理的计划阶段：算出每条音视频要怎么改，记在内容的 ``edit_plan`` 上。

渲染层和 ``get_path`` 读同一个计划，卡片显示的时长就是真正发出去的文件的时长。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable

from nonebot import logger

from ..config import pconfig
from ..constants import MIN_KEEP_PIECE_SECONDS, PlatformEnum
from ..data import (
    AudioContent,
    MediaContent,
    MediaEditPlan,
    ParseResult,
    VideoContent,
)
from .sponsorblock import lookup_segments

STALENESS_TOLERANCE = 2.0
"""标注记录的时长与当前时长相差多少秒以内还算对得上（上游约定 ±2 秒）"""


def _normalize_ranges(
    ranges: Iterable[tuple[float, float]],
    duration: float,
    min_gap: float = 0.0,
) -> tuple[tuple[float, float], ...]:
    """裁到源时长内、排序、合并；间隔不超过 min_gap 的碎片也并掉"""
    clamped = sorted(
        (max(0.0, start), min(end, duration))
        for start, end in ranges
        if min(end, duration) > max(0.0, start)
    )

    merged: list[list[float]] = []
    for start, end in clamped:
        if merged and start - merged[-1][1] <= min_gap:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    if merged:
        if merged[0][0] <= min_gap:
            merged[0][0] = 0.0
        if merged[-1][1] >= duration - min_gap:
            merged[-1][1] = duration
    return tuple((start, end) for start, end in merged)


async def _video_ranges(
    result: ParseResult, cont: VideoContent
) -> tuple[tuple[tuple[float, float], ...], tuple[tuple[float, float], ...]]:
    """按分类白名单和 actionType 取回要裁掉 / 要消音的区间"""
    api_base = pconfig.sponsorblock_api_base
    categories = pconfig.sponsorblock_categories
    bvid = cont.origin_ids.get("bvid")
    cid = cont.origin_ids.get("cid")
    if (
        api_base is None
        or not categories
        or result.platform.name is not PlatformEnum.BILIBILI
        or not (bvid and cid)
        or cont.duration <= 0
    ):
        return (), ()

    cuts: list[tuple[float, float]] = []
    mutes: list[tuple[float, float]] = []
    for segment in await lookup_segments(api_base, bvid, cid, categories):
        if segment.category not in categories:
            continue
        recorded = segment.recorded_duration
        if recorded is not None and abs(recorded - cont.duration) > STALENESS_TOLERANCE:
            logger.debug(f"片段分类 {segment.category} 的标注已对不上当前视频，忽略")
            continue
        if segment.action_type == "skip":
            cuts.append((segment.start, segment.end))
        elif segment.action_type == "mute":
            mutes.append((segment.start, segment.end))

    # 并掉太短的保留片段，让计划算出的时长就是真正会留下来的时长
    normalized_cuts = _normalize_ranges(cuts, cont.duration, MIN_KEEP_PIECE_SECONDS)
    if sum(end - start for start, end in normalized_cuts) >= cont.duration:
        logger.debug("片段几乎覆盖整条视频，放弃裁切")
        normalized_cuts = ()
    return normalized_cuts, _normalize_ranges(mutes, cont.duration)


async def _plan_one(result: ParseResult, cont: MediaContent) -> None:
    ratio = pconfig.media_speedup_rules.get((result.platform.name, result.author.id))
    cuts: tuple[tuple[float, float], ...] = ()
    mutes: tuple[tuple[float, float], ...] = ()
    if isinstance(cont, VideoContent):
        cuts, mutes = await _video_ranges(result, cont)
    plan = MediaEditPlan(speed_ratio=ratio or 1.0, cut_ranges=cuts, mute_ranges=mutes)
    cont.edit_plan = None if plan.is_noop else plan


def _editable(result: ParseResult) -> list[tuple[ParseResult, MediaContent]]:
    """结果里所有可能被编辑的音视频，转发内容算进来"""
    results = [result]
    if result.repost is not None:
        results.append(result.repost)
    return [
        (item, cont)
        for item in results
        for cont in item.content
        if isinstance(cont, VideoContent | AudioContent)
    ]


async def plan_media_edits(result: ParseResult) -> None:
    """为结果里即将发送的音视频计算编辑计划，音频只按倍速处理

    必须在渲染之前调用；结果对象跨消息缓存，没配置时要显式清掉旧计划。
    """
    pairs = _editable(result)
    if not pconfig.media_speedup_rules and pconfig.sponsorblock_api_base is None:
        for _, cont in pairs:
            cont.edit_plan = None
        return
    await asyncio.gather(*(_plan_one(item, cont) for item, cont in pairs))


def edit_digests(result: ParseResult) -> str:
    """所有编辑计划的指纹，把「同一个 URL 但处理方式不同」的卡片分开缓存"""
    return ",".join(
        cont.edit_plan.digest()
        for _, cont in _editable(result)
        if cont.edit_plan is not None and not cont.edit_plan.is_noop
    )
