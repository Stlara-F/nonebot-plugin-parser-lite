from __future__ import annotations

import asyncio
from pathlib import Path

from anyio import Path as AnyPath
import pytest
import render_test_support as support
from render_test_support import ResolvedPathTask

from nonebot_plugin_parser_lite.data import VideoContent, VideoEditPlan
from nonebot_plugin_parser_lite.exception import SizeLimitException
from nonebot_plugin_parser_lite.preprocess import apply as apply_module
from nonebot_plugin_parser_lite.preprocess.apply import (
    apply_edit_plan,
    build_output_name,
)
from nonebot_plugin_parser_lite.utils.cache import CacheManager
from nonebot_plugin_parser_lite.utils.ffmpeg import FFmpeg


@pytest.fixture
def media_dir() -> Path:
    return Path(CacheManager.cache_dir(CacheManager.MEDIA))


@pytest.fixture
def recorder(monkeypatch, media_dir):
    """捕获 ffmpeg 调用并真实落一个占位产物，替代 _probe_media 与执行"""
    calls: list[list[str]] = []
    state = {"duration": 20.0, "streams": ["video", "audio"], "fail": None}

    async def fake_probe(path: Path):
        return {
            "format": {"duration": str(state["duration"])},
            "streams": [{"codec_type": name} for name in state["streams"]],
        }

    async def fake_monitored(cmd, output_path, max_size_mb, on_progress=None):
        calls.append(list(cmd))
        if state["fail"] is not None:
            raise state["fail"]
        await AnyPath(str(output_path)).write_bytes(b"edited")
        return b""

    monkeypatch.setattr(FFmpeg, "_probe_media", classmethod(lambda cls, p: fake_probe(p)))
    monkeypatch.setattr(
        FFmpeg,
        "exec_ffmpeg_monitored",
        staticmethod(lambda cmd, output_path, max_size_mb, on_progress=None: fake_monitored(cmd, output_path, max_size_mb)),
    )
    for stale in media_dir.glob("*.mp4"):
        stale.unlink()
    return {"calls": calls, "state": state, "media_dir": media_dir}


def filter_of(cmd: list[str]) -> str:
    return cmd[cmd.index("-filter_complex") + 1] if "-filter_complex" in cmd else ""


def test_speed_only_command_reencodes_and_ends_with_output_path(recorder):
    asyncio.run(
        FFmpeg.edit_video(
            Path("src.mp4"), "u_speed", speed_ratio=1.25, max_size_mb=90
        )
    )
    cmd = recorder["calls"][-1]
    assert filter_of(cmd) == "[0:v]setpts=PTS/1.25[vf];[0:a]atempo=1.25[af]"
    assert cmd[cmd.index("-i") + 1] == "src.mp4"
    # 滤镜必须在输入之后，否则 ffmpeg 会把它当成输入选项
    assert cmd.index("-filter_complex") > cmd.index("-i")
    assert cmd[cmd.index("-map") + 1] == "[vf]"
    assert cmd[cmd.index("-map", cmd.index("-map") + 1) + 1] == "[af]"
    assert cmd[cmd.index("-c:v") + 1] == "libx264"
    # 缺输出路径时 ffmpeg 只报「unconnected output」，这里直接钉住末位参数
    assert Path(cmd[-1]).name == ".u_speed.*.tmp.mp4" or cmd[-1].endswith(".tmp.mp4")
    assert (recorder["media_dir"] / "u_speed.mp4").is_file()


def test_mute_only_keeps_video_stream_copy(recorder):
    asyncio.run(
        FFmpeg.edit_video(
            Path("src.mp4"), "u_mute", mute_ranges=((5.0, 10.0),), max_size_mb=90
        )
    )
    cmd = recorder["calls"][-1]
    assert filter_of(cmd) == "[0:a]volume=enable='between(t,5,10)':volume=0[at]"
    assert cmd[cmd.index("-c:v") + 1] == "copy"
    assert "-pix_fmt" not in cmd
    assert cmd[cmd.index("-map") + 1] == "0:v"


def test_cut_builds_trim_concat_and_scales_afterwards(recorder):
    asyncio.run(
        FFmpeg.edit_video(
            Path("src.mp4"),
            "u_cut",
            speed_ratio=2.0,
            cut_ranges=((5.0, 10.0),),
            max_size_mb=90,
        )
    )
    graph = filter_of(recorder["calls"][-1])
    assert "[0:v]split=2[vc0][vc1]" in graph
    assert "[vc1]trim=start=10:end=20,setpts=PTS-STARTPTS[v1]" in graph
    assert "[v0][a0][v1][a1]concat=n=2:v=1:a=1[vt][at]" in graph
    # 变速必须排在拼接之后，否则区间边界会落在错误的时间轴上
    assert graph.index("concat=") < graph.index("setpts=PTS/2")


def test_cut_applies_mute_on_the_source_timeline(recorder):
    asyncio.run(
        FFmpeg.edit_video(
            Path("src.mp4"),
            "u_cutmute",
            cut_ranges=((2.0, 4.0),),
            mute_ranges=((10.0, 12.0),),
            max_size_mb=90,
        )
    )
    graph = filter_of(recorder["calls"][-1])
    assert "[0:a]volume=enable='between(t,10,12)':volume=0[am]" in graph
    assert "[am]asplit=2[ac0][ac1]" in graph
    assert graph.index("volume=enable") < graph.index("asplit")


def test_silent_source_produces_no_audio_map(recorder):
    recorder["state"]["streams"] = ["video"]
    asyncio.run(
        FFmpeg.edit_video(Path("src.mp4"), "u_silent", speed_ratio=2.0, max_size_mb=90)
    )
    cmd = recorder["calls"][-1]
    assert "-an" in cmd
    assert "[0:a]" not in filter_of(cmd)


def test_existing_product_is_reused_without_transcoding(recorder):
    product = recorder["media_dir"] / "u_cached.mp4"
    product.write_bytes(b"already")
    asyncio.run(FFmpeg.edit_video(Path("src.mp4"), "u_cached", speed_ratio=2.0))
    assert recorder["calls"] == []
    assert product.read_bytes() == b"already"


def test_output_name_depends_on_ratio_and_ranges():
    source = AnyPath(support.SyncPath("abc123.mp4"))
    plan_a = VideoEditPlan(speed_ratio=1.25)
    plan_b = VideoEditPlan(speed_ratio=1.5)
    plan_c = VideoEditPlan(speed_ratio=1.25, cut_ranges=((1.0, 2.0),))
    assert build_output_name(source, plan_a) != build_output_name(source, plan_b)
    assert build_output_name(source, plan_a) != build_output_name(source, plan_c)


def make_video(plan: VideoEditPlan | None) -> VideoContent:
    cont = VideoContent(
        path_task=ResolvedPathTask(support.SyncPath("video.mp4")),  # type: ignore[arg-type]
        duration=60.0,
    )
    cont.edit_plan = plan
    return cont


@pytest.mark.asyncio
async def test_apply_returns_source_when_plan_is_absent_or_noop(monkeypatch):
    async def boom(*args, **kwargs):
        raise AssertionError("不该调用 ffmpeg")

    monkeypatch.setattr(FFmpeg, "edit_video", boom)
    source = AnyPath(support.SyncPath("video.mp4"))
    assert await apply_edit_plan(make_video(None), source) == source
    assert await apply_edit_plan(make_video(VideoEditPlan()), source) == source


@pytest.mark.asyncio
async def test_apply_skips_when_ffmpeg_is_missing(monkeypatch):
    async def unavailable(cls):
        return False

    called = False

    async def spy(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError

    monkeypatch.setattr(FFmpeg, "is_available", classmethod(unavailable))
    monkeypatch.setattr(FFmpeg, "edit_video", spy)
    source = AnyPath(support.SyncPath("video.mp4"))
    assert await apply_edit_plan(make_video(VideoEditPlan(1.25)), source) == source
    assert called is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [RuntimeError("ffmpeg 执行失败"), SizeLimitException(120.0)],
)
async def test_apply_falls_back_to_original_on_failure(monkeypatch, error):
    async def unavailable(cls):
        return True

    async def failing(cls, *args, **kwargs):
        raise error

    monkeypatch.setattr(FFmpeg, "is_available", classmethod(unavailable))
    monkeypatch.setattr(FFmpeg, "edit_video", classmethod(failing))
    source = AnyPath(support.SyncPath("video.mp4"))
    assert await apply_edit_plan(make_video(VideoEditPlan(1.25)), source) == source


@pytest.mark.asyncio
async def test_apply_passes_configured_size_limit(monkeypatch):
    seen: dict[str, object] = {}

    async def unavailable(cls):
        return True

    async def spy(cls, input_path, file_name, **kwargs):
        seen.update(kwargs)
        seen["file_name"] = file_name
        return AnyPath(support.SyncPath("product.mp4"))

    monkeypatch.setattr(FFmpeg, "is_available", classmethod(unavailable))
    monkeypatch.setattr(FFmpeg, "edit_video", classmethod(spy))
    monkeypatch.setattr(support.pconfig, "plite_max_size", 42)

    plan = VideoEditPlan(speed_ratio=1.25)
    await apply_edit_plan(make_video(plan), AnyPath(support.SyncPath("v.mp4")))
    assert seen["max_size_mb"] == 42
    assert seen["speed_ratio"] == 1.25
    assert seen["file_name"] == build_output_name(
        AnyPath(support.SyncPath("v.mp4")), plan
    )


@pytest.mark.asyncio
async def test_transcode_gate_serialises_at_concurrency_one(monkeypatch, recorder):
    monkeypatch.setattr(support.pconfig, "plite_media_transcode_concurrency", 1)
    apply_module._GATE = apply_module._TranscodeGate()

    running = 0
    peak = 0

    async def unavailable(cls):
        return True

    async def slow(cls, input_path, file_name, **kwargs):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.02)
        running -= 1
        return AnyPath(support.SyncPath(f"{file_name}.mp4"))

    monkeypatch.setattr(FFmpeg, "is_available", classmethod(unavailable))
    monkeypatch.setattr(FFmpeg, "edit_video", classmethod(slow))

    plan = VideoEditPlan(speed_ratio=1.25)
    source = AnyPath(support.SyncPath("video.mp4"))
    await asyncio.gather(*(apply_edit_plan(make_video(plan), source) for _ in range(4)))
    assert peak == 1


@pytest.mark.asyncio
async def test_transcode_gate_allows_configured_parallelism(monkeypatch, recorder):
    monkeypatch.setattr(support.pconfig, "plite_media_transcode_concurrency", 2)
    apply_module._GATE = apply_module._TranscodeGate()

    running = 0
    peak = 0

    async def unavailable(cls):
        return True

    async def slow(cls, input_path, file_name, **kwargs):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.02)
        running -= 1
        return AnyPath(support.SyncPath(f"{file_name}.mp4"))

    monkeypatch.setattr(FFmpeg, "is_available", classmethod(unavailable))
    monkeypatch.setattr(FFmpeg, "edit_video", classmethod(slow))

    plan = VideoEditPlan(speed_ratio=1.25)
    source = AnyPath(support.SyncPath("video.mp4"))
    await asyncio.gather(*(apply_edit_plan(make_video(plan), source) for _ in range(4)))
    assert peak == 2


def capture_segments(monkeypatch) -> list:
    """记录构造消息段时真正使用的文件"""
    seen: list = []

    async def img_seg(file):
        from nonebot_plugin_alconna.uniseg import Image

        return Image(raw=b"cover")

    async def video_seg(file, thumbnail=None):
        from nonebot_plugin_alconna.uniseg import Video

        seen.append(file)
        return Video(raw=b"video")

    async def file_seg(file, display_name=None):
        from nonebot_plugin_alconna.uniseg import File

        seen.append(file)
        return File(raw=b"video", name=display_name or "video.mp4")

    def construct(segments, user_id=None):
        from nonebot_plugin_alconna.uniseg import Reference

        return Reference(nodes=[])

    monkeypatch.setattr(support.UniHelper, "construct_forward_message", construct)
    monkeypatch.setattr(support.UniHelper, "img_seg", img_seg)
    monkeypatch.setattr(support.UniHelper, "video_seg", video_seg)
    monkeypatch.setattr(support.UniHelper, "file_seg", file_seg)
    return seen


@pytest.mark.asyncio
@pytest.mark.parametrize("need_upload", [False, True])
@pytest.mark.parametrize("in_forward", [False, True])
async def test_send_content_uses_the_edited_product(
    monkeypatch, tmp_path, need_upload, in_forward
):
    monkeypatch.setattr(support.pconfig, "plite_need_upload_video", need_upload)
    monkeypatch.setattr(support.pconfig, "plite_need_upload", False)
    monkeypatch.setattr(support.pconfig, "plite_video_in_forward", in_forward)
    monkeypatch.setattr(support.pconfig, "plite_need_forward_contents", False)

    seen = capture_segments(monkeypatch)

    original = tmp_path / "original.mp4"
    original.write_bytes(b"source")
    product = tmp_path / "product.mp4"
    product.write_bytes(b"edited")

    async def fake_apply(cont, source_path):
        return AnyPath(product)

    monkeypatch.setattr(
        "nonebot_plugin_parser_lite.render.apply_edit_plan", fake_apply
    )

    cont = make_video(VideoEditPlan(speed_ratio=1.25))
    cont.path_task = ResolvedPathTask(original)
    result = support.make_result()
    result.content = [cont]

    messages = [message async for message in support.Renderer().send_content(result)]
    assert messages, "视频消息应当被产出"
    assert [str(path) for path in seen] == [str(product)], (
        "消息段必须使用处理后的产物，而不是原片"
    )


@pytest.mark.asyncio
async def test_send_content_falls_back_to_source_when_unedited(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(support.pconfig, "plite_need_upload_video", False)
    monkeypatch.setattr(support.pconfig, "plite_need_upload", False)
    monkeypatch.setattr(support.pconfig, "plite_video_in_forward", False)
    monkeypatch.setattr(support.pconfig, "plite_need_forward_contents", False)

    seen = capture_segments(monkeypatch)

    async def never_edit(cont, source_path):
        return source_path

    monkeypatch.setattr(
        "nonebot_plugin_parser_lite.render.apply_edit_plan", never_edit
    )

    original = tmp_path / "original.mp4"
    original.write_bytes(b"source")
    cont = make_video(None)
    cont.path_task = ResolvedPathTask(original)
    result = support.make_result()
    result.content = [cont]

    async for _ in support.Renderer().send_content(result):
        pass
    assert [str(path) for path in seen] == [str(original)]


@pytest.mark.parametrize(
    ("cut_ranges", "expected_trim"),
    [
        # 裁掉开头只留一个片段时仍然必须裁切，不能因为没有拼接就跳过
        (((0.0, 5.0),), "trim=start=5:end=20"),
        (((15.0, 20.0),), "trim=start=0:end=15"),
        (((5.0, 10.0),), "trim=start=10:end=20"),
    ],
)
def test_single_piece_cut_still_trims(recorder, cut_ranges, expected_trim):
    asyncio.run(
        FFmpeg.edit_video(
            Path("src.mp4"),
            f"u_{int(cut_ranges[0][0])}",
            speed_ratio=1.25,
            cut_ranges=cut_ranges,
            max_size_mb=90,
        )
    )
    graph = filter_of(recorder["calls"][-1])
    assert expected_trim in graph
    assert "concat=n=1:" in graph or "concat=n=2:" in graph
    assert "setpts=PTS/1.25" in graph
