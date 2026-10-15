"""上传前预处理的决策阶段。

只负责算出「这条视频要怎么改」，不碰文件也不碰 ffmpeg。计划挂在
``VideoContent.edit_plan`` 上，渲染层与上传层读同一个来源，因此卡片展示的
时长与实际发送的文件始终一致；重复解析同一条缓存结果也不会把倍速叠加两次。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable

from nonebot import logger

from ..config import MediaSpeedupRule, pconfig
from ..constants import PlatformEnum
from ..data import ParseResult, VideoContent, VideoEditPlan
from .sponsorblock import fetch_segments

STALENESS_TOLERANCE = 0.1
"""片段提交时记录的时长与当前源时长允许偏离的比例"""
STALENESS_FLOOR_SECONDS = 5.0
"""短视频的绝对容差下限"""


def _lookup_ratio(
    rules: tuple[MediaSpeedupRule, ...],
    platform: PlatformEnum,
    author_id: str | None,
) -> float:
    if author_id is None:
        return 1.0
    for rule in rules:
        if rule.platform is platform and rule.author_id == author_id:
            return rule.ratio
    return 1.0


def _normalize_ranges(
    ranges: Iterable[tuple[float, float]],
    duration: float,
) -> tuple[tuple[float, float], ...]:
    """裁剪到源时长内、排序并合并重叠区间"""
    clamped: list[tuple[float, float]] = []
    for start, end in ranges:
        left = max(0.0, start)
        right = min(end, duration)
        if right > left:
            clamped.append((left, right))
    clamped.sort()

    merged: list[list[float]] = []
    for start, end in clamped:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return tuple((start, end) for start, end in merged)


def _is_stale(recorded: float | None, source_duration: float) -> bool:
    """判断标注是否已对不上当前视频版本；服务没给时长时不作否证"""
    if recorded is None:
        return False
    tolerance = max(STALENESS_FLOOR_SECONDS, source_duration * STALENESS_TOLERANCE)
    return abs(recorded - source_duration) > tolerance


async def _fetch_edit_ranges(
    result: ParseResult,
    cont: VideoContent,
) -> tuple[tuple[tuple[float, float], ...], tuple[tuple[float, float], ...]]:
    """按分类白名单与 actionType 取回需要裁掉 / 消音的区间"""
    api_base = pconfig.sponsorblock_api_base
    categories = pconfig.sponsorblock_categories
    if api_base is None or not categories:
        return (), ()
    if result.platform.name is not PlatformEnum.BILIBILI:
        return (), ()
    if cont.duration <= 0:
        logger.debug("源时长未知，跳过片段查询")
        return (), ()

    bvid = cont.origin_ids.get("bvid")
    cid = cont.origin_ids.get("cid")
    if not bvid or not cid:
        return (), ()

    segments, recorded = await fetch_segments(api_base, bvid, cid, categories)
    if not segments:
        return (), ()

    allowed = set(categories)
    cuts: list[tuple[float, float]] = []
    mutes: list[tuple[float, float]] = []
    for segment in segments:
        if segment.category not in allowed:
            continue
        if _is_stale(recorded.get(segment.category), cont.duration):
            logger.debug(f"片段分类 {segment.category} 的标注时长与当前视频不符，忽略")
            continue
        if segment.action_type == "skip":
            cuts.append((segment.start, segment.end))
        elif segment.action_type == "mute":
            mutes.append((segment.start, segment.end))

    normalized_cuts = _normalize_ranges(cuts, cont.duration)
    if sum(end - start for start, end in normalized_cuts) >= cont.duration:
        logger.debug("片段几乎覆盖整条视频，放弃裁切")
        normalized_cuts = ()
    return normalized_cuts, _normalize_ranges(mutes, cont.duration)


async def _plan_one(source: ParseResult, cont: VideoContent) -> None:
    ratio = _lookup_ratio(
        pconfig.media_speedup_rules,
        source.platform.name,
        source.author.id,
    )
    cuts, mutes = await _fetch_edit_ranges(source, cont)
    plan = VideoEditPlan(speed_ratio=ratio, cut_ranges=cuts, mute_ranges=mutes)
    cont.edit_plan = None if plan.is_noop else plan


async def plan_media_edits(result: ParseResult) -> None:
    """为解析结果中即将发送的视频计算上传前编辑计划

    转发内容按转发来源的作者匹配。结果对象会被跨消息缓存，所以未配置时必须
    显式清掉旧计划，否则之前生效的倍速会继续留在卡片与产物上。
    """
    sources = [result]
    if result.repost is not None:
        sources.append(result.repost)
    pairs = [
        (source, cont)
        for source in sources
        for cont in source.content
        if isinstance(cont, VideoContent)
    ]

    if not pconfig.media_speedup_rules and pconfig.sponsorblock_api_base is None:
        for _, cont in pairs:
            cont.edit_plan = None
        return

    if pairs:
        await asyncio.gather(*(_plan_one(source, cont) for source, cont in pairs))
