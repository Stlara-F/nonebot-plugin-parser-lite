from __future__ import annotations

import asyncio
from pathlib import Path

from anyio import Path as AnyPath
import pytest
import render_test_support as support
from render_test_support import ResolvedPathTask

from nonebot_plugin_parser_lite.data import AudioContent, MediaEditPlan, VideoContent
from nonebot_plugin_parser_lite.exception import SizeLimitException
from nonebot_plugin_parser_lite.utils import ffmpeg as ffmpeg_module
from nonebot_plugin_parser_lite.utils.cache import CacheManager
from nonebot_plugin_parser_lite.utils.ffmpeg import FFmpeg
from nonebot_plugin_parser_lite.utils.media_edit import edit_digests


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

    monkeypatch.setattr(
        FFmpeg, "_probe_media", classmethod(lambda cls, p: fake_probe(p))
    )
    monkeypatch.setattr(
        FFmpeg,
        "exec_ffmpeg_monitored",
        staticmethod(
            lambda cmd, output_path, max_size_mb, on_progress=None: fake_monitored(
                cmd, output_path, max_size_mb
            )
        ),
    )
    for stale in media_dir.glob("*"):
        stale.unlink()
    return {"calls": calls, "state": state, "media_dir": media_dir}


def filter_of(cmd: list[str]) -> str:
    return cmd[cmd.index("-filter_complex") + 1] if "-filter_complex" in cmd else ""


def audio_filter_of(cmd: list[str]) -> str:
    return cmd[cmd.index("-af") + 1] if "-af" in cmd else ""


def test_speed_only_command_reencodes_and_ends_with_output_path(recorder):
    asyncio.run(FFmpeg.edit_video(Path("src.mp4"), "u_speed", speed_ratio=1.25))
    cmd = recorder["calls"][-1]
    assert filter_of(cmd) == "[0:v]setpts=PTS/1.25[vf];[0:a]atempo=1.25[af]"
    assert cmd[cmd.index("-i") + 1] == "src.mp4"
    # 滤镜必须在输入之后，否则 ffmpeg 把它当成输入选项
    assert cmd.index("-filter_complex") > cmd.index("-i")
    assert cmd[cmd.index("-map") + 1] == "[vf]"
    assert cmd[cmd.index("-map", cmd.index("-map") + 1) + 1] == "[af]"
    assert cmd[cmd.index("-c:v") + 1] == "libx264"
    # 缺输出路径时 ffmpeg 只报「unconnected output」，这里直接钉住末位参数
    assert cmd[-1].endswith(".tmp.mp4")
    assert (recorder["media_dir"] / "u_speed.mp4").is_file()


def test_mute_only_keeps_video_stream_copy(recorder):
    asyncio.run(FFmpeg.edit_video(Path("src.mp4"), "u_mute", mute_ranges=((5.0, 10.0),)))
    cmd = recorder["calls"][-1]
    assert filter_of(cmd) == "[0:a]volume=enable='between(t,5,10)':volume=0[at]"
    assert cmd[cmd.index("-c:v") + 1] == "copy"
    assert "-pix_fmt" not in cmd
    assert cmd[cmd.index("-map") + 1] == "0:v"


def test_cut_builds_trim_concat_and_scales_afterwards(recorder):
    asyncio.run(
        FFmpeg.edit_video(
            Path("src.mp4"), "u_cut", speed_ratio=2.0, cut_ranges=((5.0, 10.0),)
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
        )
    )
    graph = filter_of(recorder["calls"][-1])
    assert "[0:a]volume=enable='between(t,10,12)':volume=0[am]" in graph
    assert "[am]asplit=2[ac0][ac1]" in graph
    assert graph.index("volume=enable") < graph.index("asplit")


@pytest.mark.parametrize(
    ("cut_ranges", "expected_trim"),
    [
        # 裁掉开头只留一个片段时仍然必须裁切，不能因为没得拼接就跳过
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
        )
    )
    graph = filter_of(recorder["calls"][-1])
    assert expected_trim in graph
    assert "concat=n=1:" in graph or "concat=n=2:" in graph
    assert "setpts=PTS/1.25" in graph


def test_silent_source_produces_no_audio_map(recorder):
    recorder["state"]["streams"] = ["video"]
    asyncio.run(FFmpeg.edit_video(Path("src.mp4"), "u_silent", speed_ratio=2.0))
    cmd = recorder["calls"][-1]
    assert "-an" in cmd
    assert "[0:a]" not in filter_of(cmd)


def test_silent_source_with_cuts_and_mutes_does_not_reference_audio(recorder):
    """没有音轨时不能挂引用 [0:a] 的消音滤镜

    滤镜图一旦被拒，整次编辑（含裁切和变速）都会退化成原片。
    """
    recorder["state"]["streams"] = ["video"]
    asyncio.run(
        FFmpeg.edit_video(
            Path("src.mp4"),
            "u_silentcut",
            speed_ratio=1.25,
            cut_ranges=((2.0, 4.0),),
            mute_ranges=((10.0, 12.0),),
        )
    )
    cmd = recorder["calls"][-1]
    graph = filter_of(cmd)
    assert "volume=enable" not in graph
    assert "[0:a]" not in graph
    assert "-an" in cmd
    assert "trim=start=4:end=20" in graph
    assert "setpts=PTS/1.25" in graph


def test_existing_product_is_reused_without_transcoding(recorder):
    product = recorder["media_dir"] / "u_cached.mp4"
    product.write_bytes(b"already")
    asyncio.run(FFmpeg.edit_video(Path("src.mp4"), "u_cached", speed_ratio=2.0))
    assert recorder["calls"] == []
    assert product.read_bytes() == b"already"


def test_tempo_chain_covers_the_whole_configured_range():
    assert FFmpeg._build_tempo_chain(1.25) == "atempo=1.25"
    assert FFmpeg._build_tempo_chain(2.0) == "atempo=2"
    assert FFmpeg._build_tempo_chain(0.5) == "atempo=0.5"
    # 单级 atempo 只保证 0.5~2.0，超出必须串级，否则老版本 ffmpeg 直接拒
    assert FFmpeg._build_tempo_chain(4.0) == "atempo=2,atempo=2"
    assert FFmpeg._build_tempo_chain(3.0) == "atempo=2,atempo=1.5"
    assert FFmpeg._build_tempo_chain(0.4) == "atempo=0.5,atempo=0.8"


def test_extreme_ratio_is_split_into_tempo_stages(recorder):
    asyncio.run(FFmpeg.edit_video(Path("src.mp4"), "u_extreme", speed_ratio=4.0))
    graph = filter_of(recorder["calls"][-1])
    assert "setpts=PTS/4" in graph
    assert "atempo=2,atempo=2" in graph
    assert "atempo=4" not in graph


def test_edit_audio_reencodes_and_keeps_container(recorder):
    asyncio.run(FFmpeg.edit_audio(Path("src.mp3"), "a_speed", speed_ratio=1.5))
    cmd = recorder["calls"][-1]
    assert audio_filter_of(cmd) == "atempo=1.5"
    assert "-vn" in cmd
    assert cmd[cmd.index("-c:a") + 1] == "libmp3lame"
    assert cmd.index("-af") > cmd.index("-i")
    assert cmd[-1].endswith(".tmp.mp3")
    assert (recorder["media_dir"] / "a_speed.mp3").is_file()


@pytest.mark.parametrize(
    ("suffix", "codec"),
    [(".m4a", "aac"), (".mp4", "aac"), (".wav", "pcm_s16le"), (".flac", "flac")],
)
def test_edit_audio_picks_codec_by_container(recorder, suffix, codec):
    asyncio.run(FFmpeg.edit_audio(Path(f"src{suffix}"), f"a_{codec}", speed_ratio=1.25))
    cmd = recorder["calls"][-1]
    assert cmd[cmd.index("-c:a") + 1] == codec
    # 产物沿用源容器，避免收发端按扩展名认错格式
    assert Path(cmd[-1]).suffix == suffix


def test_edit_audio_chains_tempo_for_extreme_ratio(recorder):
    asyncio.run(FFmpeg.edit_audio(Path("src.mp3"), "a_extreme", speed_ratio=4.0))
    assert audio_filter_of(recorder["calls"][-1]) == "atempo=2,atempo=2"


def test_edit_audio_reuses_product(recorder):
    product = recorder["media_dir"] / "a_cached.mp3"
    product.write_bytes(b"already")
    asyncio.run(FFmpeg.edit_audio(Path("src.mp3"), "a_cached", speed_ratio=1.5))
    assert recorder["calls"] == []
    assert product.read_bytes() == b"already"


def make_video(tmp_path: Path, plan: MediaEditPlan | None) -> VideoContent:
    source = tmp_path / "video.mp4"
    source.write_bytes(b"source")
    cont = VideoContent(path_task=ResolvedPathTask(source), duration=60.0)
    cont.edit_plan = plan
    return cont


def make_audio(tmp_path: Path, plan: MediaEditPlan | None) -> AudioContent:
    source = tmp_path / "audio.mp3"
    source.write_bytes(b"source")
    cont = AudioContent(path_task=ResolvedPathTask(source), duration=60.0)
    cont.edit_plan = plan
    return cont


@pytest.mark.asyncio
async def test_get_path_returns_source_without_a_plan(tmp_path):
    cont = make_video(tmp_path, None)
    source = await cont.path_task
    assert await cont.get_path() == source
    cont.edit_plan = MediaEditPlan()
    assert await cont.get_path() == source


@pytest.mark.asyncio
async def test_get_path_transcodes_and_returns_product(tmp_path, monkeypatch):
    async def always(cls):
        return True

    seen: dict[str, object] = {}

    async def spy(cls, input_path, file_name, **kwargs):
        seen.update(kwargs, file_name=file_name, input_path=input_path)
        product = tmp_path / "product.mp4"
        product.write_bytes(b"edited")
        return AnyPath(product)

    monkeypatch.setattr(FFmpeg, "is_available", classmethod(always))
    monkeypatch.setattr(FFmpeg, "edit_video", classmethod(spy))
    monkeypatch.setattr(support.pconfig, "plite_max_size", 42)

    plan = MediaEditPlan(speed_ratio=1.25, cut_ranges=((5.0, 10.0),))
    cont = make_video(tmp_path, plan)
    source = await cont.path_task
    assert str(await cont.get_path()) == str(tmp_path / "product.mp4")
    assert seen["input_path"] == source
    assert seen["max_size_mb"] == 42
    assert seen["speed_ratio"] == 1.25
    assert seen["file_name"] == plan.cache_name(source)


@pytest.mark.asyncio
async def test_audio_plan_routes_to_the_audio_editor(tmp_path, monkeypatch):
    async def always(cls):
        return True

    async def video_editor(cls, *args, **kwargs):
        raise AssertionError("音频不该走视频编辑器")

    captured: dict[str, object] = {}

    async def audio_editor(cls, input_path, file_name, **kwargs):
        captured.update(file_name=file_name, **kwargs)
        return AnyPath(tmp_path / "product.mp3")

    monkeypatch.setattr(FFmpeg, "is_available", classmethod(always))
    monkeypatch.setattr(FFmpeg, "edit_video", classmethod(video_editor))
    monkeypatch.setattr(FFmpeg, "edit_audio", classmethod(audio_editor))

    plan = MediaEditPlan(speed_ratio=1.5)
    cont = make_audio(tmp_path, plan)
    assert str(await cont.get_path()) == str(tmp_path / "product.mp3")
    assert captured["speed_ratio"] == 1.5
    assert captured["file_name"] == plan.cache_name(await cont.path_task)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [RuntimeError("ffmpeg 执行失败"), SizeLimitException(120.0)]
)
async def test_failure_falls_back_to_source_and_clears_plan(
    tmp_path, monkeypatch, error
):
    async def always(cls):
        return True

    async def failing(cls, *args, **kwargs):
        raise error

    monkeypatch.setattr(FFmpeg, "is_available", classmethod(always))
    monkeypatch.setattr(FFmpeg, "edit_video", classmethod(failing))

    cont = make_video(tmp_path, MediaEditPlan(1.25))
    source = await cont.path_task
    assert await cont.get_path() == source
    # 回退到原片后必须清掉计划，否则卡片继续按「已处理」显示时长与体积
    assert cont.edit_plan is None
    assert cont.display_duration == "1:00"


@pytest.mark.asyncio
async def test_missing_ffmpeg_skips_editing_and_clears_plan(tmp_path, monkeypatch):
    async def never(cls):
        return False

    async def spy(cls, *args, **kwargs):
        raise AssertionError("缺 ffmpeg 时不该调用转码")

    monkeypatch.setattr(FFmpeg, "is_available", classmethod(never))
    monkeypatch.setattr(FFmpeg, "edit_video", classmethod(spy))

    cont = make_video(tmp_path, MediaEditPlan(1.25))
    assert await cont.get_path() == await cont.path_task
    assert cont.edit_plan is None


async def _measure_peak(tmp_path, monkeypatch, concurrency: int) -> int:
    monkeypatch.setattr(support.pconfig, "plite_media_transcode_concurrency", concurrency)
    monkeypatch.setattr(ffmpeg_module, "_gate", None)

    async def always(cls):
        return True

    running = 0
    peak = 0

    async def slow(cls, input_path, file_name, **kwargs):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.02)
        running -= 1
        return AnyPath(tmp_path / "product.mp4")

    monkeypatch.setattr(FFmpeg, "is_available", classmethod(always))
    monkeypatch.setattr(FFmpeg, "edit_video", classmethod(slow))

    plan = MediaEditPlan(speed_ratio=1.25)
    await asyncio.gather(
        *(make_video(tmp_path, plan).get_path() for _ in range(4))
    )
    return peak


@pytest.mark.asyncio
async def test_transcode_gate_serialises_at_concurrency_one(tmp_path, monkeypatch):
    assert await _measure_peak(tmp_path, monkeypatch, 1) == 1


@pytest.mark.asyncio
async def test_transcode_gate_allows_configured_parallelism(tmp_path, monkeypatch):
    assert await _measure_peak(tmp_path, monkeypatch, 2) == 2


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

    async def always(cls):
        return True

    async def fake_transcode(cls, input_path, file_name, **kwargs):
        product = tmp_path / "product.mp4"
        product.write_bytes(b"edited")
        return AnyPath(product)

    monkeypatch.setattr(FFmpeg, "is_available", classmethod(always))
    monkeypatch.setattr(FFmpeg, "edit_video", classmethod(fake_transcode))

    seen = capture_segments(monkeypatch)
    cont = make_video(tmp_path, MediaEditPlan(speed_ratio=1.25))
    result = support.make_result()
    result.content = [cont]

    messages = [message async for message in support.Renderer().send_content(result)]
    assert messages, "视频消息应当被产出"
    assert [str(path) for path in seen] == [str(tmp_path / "product.mp4")], (
        "消息段必须使用处理后的产物，而不是原片"
    )


@pytest.mark.asyncio
async def test_send_content_falls_back_to_source_when_ffmpeg_is_missing(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(support.pconfig, "plite_need_upload_video", False)
    monkeypatch.setattr(support.pconfig, "plite_need_upload", False)
    monkeypatch.setattr(support.pconfig, "plite_video_in_forward", False)
    monkeypatch.setattr(support.pconfig, "plite_need_forward_contents", False)

    async def never(cls):
        return False

    monkeypatch.setattr(FFmpeg, "is_available", classmethod(never))

    seen = capture_segments(monkeypatch)
    cont = make_video(tmp_path, MediaEditPlan(speed_ratio=1.25))
    result = support.make_result()
    result.content = [cont]

    async for _ in support.Renderer().send_content(result):
        pass
    assert [str(path) for path in seen] == [str(await cont.path_task)]


@pytest.mark.asyncio
async def test_card_cache_key_changes_with_the_edit_plan(monkeypatch, tmp_path):
    """卡片是按计划换算出来的时长渲染的，计划变了就必须换一张卡

    持久化的卡片缓存跨重启复用，键里若没有计划指纹，改了倍速配置后老卡片会
    一直显示旧时长。
    """

    renders: list = []

    async def fake_render_image(self, result, **kwargs):
        renders.append(edit_digests(result))
        return tmp_path / "raw.png"

    async def fake_compress(path):
        return b"IMAGE"

    render_dir = tmp_path / "render"

    async def fake_ensure_dir(cache_type=CacheManager.MEDIA):
        render_dir.mkdir(parents=True, exist_ok=True)
        return AnyPath(render_dir)

    monkeypatch.setattr(
        type(support.Renderer()), "render_image", fake_render_image, raising=True
    )
    monkeypatch.setattr(FFmpeg, "png_to_webp", staticmethod(fake_compress))
    monkeypatch.setattr(
        CacheManager,
        "ensure_dir",
        classmethod(
            lambda cls, cache_type=CacheManager.MEDIA: fake_ensure_dir(cache_type)
        ),
    )

    video = make_video(tmp_path, MediaEditPlan(speed_ratio=1.25))
    result = support.make_result()
    result.content = [video]

    await support.Renderer().cache_or_render_image(result)
    await support.Renderer().cache_or_render_image(result)
    assert len(renders) == 1, "同一计划应命中卡片缓存"

    video.edit_plan = MediaEditPlan(speed_ratio=1.5)
    await support.Renderer().cache_or_render_image(result)
    assert len(renders) == 2, "计划变化后必须重渲染卡片"

    video.edit_plan = None
    await support.Renderer().cache_or_render_image(result)
    assert len(renders) == 3
