"""上传前预处理的执行阶段。

只负责「按计划把文件做出来」：并发闸、产物命名、失败回退。滤镜命令在
``FFmpeg.edit_video`` 里，计划怎么算在 ``planner`` 里，三层各自可测。
"""

from __future__ import annotations

import asyncio
import hashlib

from anyio import Path
from nonebot import logger

from ..config import pconfig
from ..data import VideoContent, VideoEditPlan
from ..exception import SizeLimitException
from ..utils.ffmpeg import FFmpeg


class _TranscodeGate:
    """按事件循环持有的转码并发闸

    转码是分钟级任务，不能去挤占卡片渲染的并发预算（render/context.py 那把闸
    只覆盖叶子图片资源），所以这里单独计量，默认 1。
    """

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._semaphore: asyncio.Semaphore | None = None

    def acquire(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        if self._loop is not loop or self._semaphore is None:
            self._loop = loop
            self._semaphore = asyncio.Semaphore(pconfig.media_transcode_concurrency)
        return self._semaphore


_GATE = _TranscodeGate()


def build_output_name(source_path: Path, plan: VideoEditPlan) -> str:
    """产物文件名要编入计划指纹，否则改倍速或换一批片段都会命中旧产物"""
    raw = f"{source_path.stem}|{plan.signature()}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


async def apply_edit_plan(cont: VideoContent, source_path: Path) -> Path:
    """按计划生成上传用的文件，失败时退回原始文件

    转码产物的体积同样受 ``plite_max_size`` 约束；这里超限只说明「加工后的
    版本太大」，不该冒泡成「媒体太大」而拒发原片。
    """
    plan = cont.edit_plan
    if plan is None or plan.is_noop:
        return source_path

    if not await FFmpeg.is_available():
        logger.warning("ffmpeg 不可用，跳过上传前预处理")
        return source_path

    try:
        async with _GATE.acquire():
            return await FFmpeg.edit_video(
                source_path,
                build_output_name(source_path, plan),
                speed_ratio=plan.speed_ratio,
                cut_ranges=plan.cut_ranges,
                mute_ranges=plan.mute_ranges,
                max_size_mb=pconfig.max_size,
            )
    except SizeLimitException as e:
        logger.warning(f"预处理后体积超限（{e.size}MB），改用原始文件上传")
        return source_path
    except Exception as e:
        logger.warning(f"上传前预处理失败，改用原始文件: {type(e).__name__}: {e!r}")
        return source_path
