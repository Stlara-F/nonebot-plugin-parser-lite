from __future__ import annotations

import asyncio
from pathlib import Path

from anyio import Path as AnyPath
import pytest
from render_test_support import ResolvedPathTask

from nonebot_plugin_parser_lite.data import AudioContent, MediaEditPlan, VideoContent


def make_video(tmp_path: Path, plan: MediaEditPlan | None = None) -> VideoContent:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"x" * 2048)
    cont = VideoContent(path_task=ResolvedPathTask(source), duration=120.0)
    cont.edit_plan = plan
    return cont


def test_output_duration_composes_cut_and_speed():
    plan = MediaEditPlan(speed_ratio=1.5, cut_ranges=((0.0, 60.0), (120.0, 180.0)))
    assert plan.removed_duration == pytest.approx(120.0)
    assert plan.output_duration(600.0) == pytest.approx(320.0)
    assert plan.output_duration(60.0) == 0.0


def test_mute_keeps_the_timeline_but_is_not_a_noop():
    plan = MediaEditPlan(mute_ranges=((10.0, 25.0),))
    assert plan.output_duration(600.0) == pytest.approx(600.0)
    assert not plan.is_noop
    assert MediaEditPlan().is_noop


@pytest.mark.parametrize("cls", [VideoContent, AudioContent])
def test_display_duration_follows_the_plan_without_mutating_source(
    cls, tmp_path: Path
):
    source = tmp_path / "media.mp4"
    source.write_bytes(b"x")
    cont = cls(path_task=ResolvedPathTask(source), duration=120.0)
    assert cont.display_duration == "2:00"

    cont.edit_plan = MediaEditPlan(speed_ratio=2.0)
    assert cont.display_duration == "1:00"
    assert cont.duration == 120.0, "时长要由计划换算，不能就地改写"
    # 同一个缓存结果反复渲染时，倍速不能被叠加两次
    assert cont.display_duration == "1:00"


@pytest.mark.parametrize("cls", [VideoContent, AudioContent])
def test_display_size_is_an_estimate_when_planned(cls, tmp_path: Path):
    source = tmp_path / "media.mp4"
    source.write_bytes(b"x" * 2048)
    cont = cls(path_task=ResolvedPathTask(source), duration=120.0)
    cont._size_bytes = 2048

    assert asyncio.run(cont.get_display_size()) == "2.00KB"
    cont.edit_plan = MediaEditPlan(speed_ratio=2.0)
    assert asyncio.run(cont.get_display_size()) == "~1.00KB"


def test_digest_separates_every_plan_input():
    plan = MediaEditPlan(1.25, ((5.0, 10.0),))
    assert plan.digest() == MediaEditPlan(1.25, ((5.0, 10.0),)).digest()
    assert plan.digest() != MediaEditPlan(1.5, ((5.0, 10.0),)).digest()
    assert plan.digest() != MediaEditPlan(1.25, ((5.0, 10.0000001),)).digest()
    assert plan.digest() != MediaEditPlan(1.25, ((5.0, 10.0),), ((1.0, 2.0),)).digest()


def test_cache_name_is_readable_and_path_sensitive(tmp_path: Path):
    plan = MediaEditPlan(1.25, ((5.0, 10.0),))
    name = plan.cache_name(AnyPath(str(tmp_path / "source.mp4")))
    assert name.startswith("source-1.25x-cliped-")
    assert ":" not in name, "Windows 的文件名不允许冒号"

    muted = MediaEditPlan(1.0, (), ((5.0, 10.0),))
    assert "-1.00x-no_clip_mute-" in muted.cache_name(AnyPath(str(tmp_path / "s.mp3")))

    elsewhere = plan.cache_name(AnyPath(str(Path("other") / "source.mp4")))
    assert name != elsewhere, "主干相同、目录不同的源不能命中同一个产物"
