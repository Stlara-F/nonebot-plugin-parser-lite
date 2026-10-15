from __future__ import annotations

import pytest
import render_test_support as support
from render_test_support import ResolvedPathTask

from nonebot_plugin_parser_lite.data import VideoContent, VideoEditPlan


def make_video(duration: float = 600.0) -> VideoContent:
    return VideoContent(
        path_task=ResolvedPathTask(support.SyncPath("video.mp4")),  # type: ignore[arg-type]
        duration=duration,
    )


def test_cut_and_speed_compose_into_one_output_duration():
    plan = VideoEditPlan(speed_ratio=1.5, cut_ranges=((0.0, 60.0), (120.0, 180.0)))
    assert plan.removed_duration == pytest.approx(120.0)
    assert plan.output_duration(600.0) == pytest.approx(320.0)


def test_mute_does_not_shorten_the_timeline():
    plan = VideoEditPlan(mute_ranges=((10.0, 25.0),))
    assert plan.output_duration(600.0) == pytest.approx(600.0)
    assert not plan.is_noop


def test_default_plan_is_noop():
    assert VideoEditPlan().is_noop


def test_output_duration_never_goes_negative():
    plan = VideoEditPlan(cut_ranges=((0.0, 900.0),))
    assert plan.output_duration(600.0) == pytest.approx(0.0)


def test_display_duration_follows_the_plan_without_mutating_source():
    cont = make_video(600.0)
    assert cont.display_duration == "10:00"

    cont.edit_plan = VideoEditPlan(speed_ratio=1.25)
    assert cont.duration == 600.0
    assert cont.display_duration == "8:00"

    # 同一个缓存结果被反复渲染时，倍速不能被叠加两次
    assert cont.display_duration == "8:00"


@pytest.mark.asyncio
async def test_display_size_is_marked_pending_only_for_real_edits():
    cont = make_video()
    cont.edit_plan = VideoEditPlan(speed_ratio=1.5)
    assert await cont.get_display_size() == "处理后变化"

    cont.edit_plan = VideoEditPlan()
    assert await cont.get_display_size() != "处理后变化"


def test_signature_separates_ratio_and_every_range_set():
    base = VideoEditPlan(speed_ratio=1.25, cut_ranges=((5.0, 10.0),))
    assert base.signature() == VideoEditPlan(1.25, ((5.0, 10.0),)).signature()
    assert base.signature() != VideoEditPlan(1.5, ((5.0, 10.0),)).signature()
    assert base.signature() != VideoEditPlan(1.25, ((5.0, 12.0),)).signature()
    assert base.signature() != VideoEditPlan(
        1.25, ((5.0, 10.0),), ((1.0, 2.0),)
    ).signature()
    assert base.signature() != VideoEditPlan(1.25).signature()
