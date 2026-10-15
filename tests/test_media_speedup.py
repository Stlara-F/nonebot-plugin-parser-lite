"""上传前倍速：规则配置、写在内容上、渲染与发送读同一个值。

倍速是平台无关的通用能力，命令拼装与真实转码分开验证；按标注裁切是 B 站特有
的，用例在 test_bilibili_sponsorblock.py。
"""

import asyncio
from pathlib import Path

from anyio import Path as AnyPath
import pytest
import render_test_support as support
from render_test_support import ResolvedPathTask

from nonebot_plugin_parser_lite.config import Config, parse_media_speedup_rules
from nonebot_plugin_parser_lite.constants import PlatformEnum
from nonebot_plugin_parser_lite.data import (
    AudioContent,
    Author,
    ParseResult,
    Platform,
    VideoContent,
    _speedup_name,
)
from nonebot_plugin_parser_lite.exception import SizeLimitException
from nonebot_plugin_parser_lite.utils import ffmpeg as ffmpeg_module
from nonebot_plugin_parser_lite.utils.cache import CacheManager
from nonebot_plugin_parser_lite.utils.ffmpeg import FFmpeg
from nonebot_plugin_parser_lite.utils.media_edit import (
    apply_media_speedup,
    speedup_digest,
)


def make_result(
    author_id: str = "123456",
    contents: list | None = None,
    platform: PlatformEnum = PlatformEnum.BILIBILI,
    repost: ParseResult | None = None,
) -> ParseResult:
    result = ParseResult(
        platform=Platform(platform, platform.value),
        author=Author("tester", id=author_id),
        url="https://example.com/video",
        content=list(contents or []),
    )
    result.repost = repost
    return result


def make_media(cls, tmp_path: Path, name: str, duration: float = 60.0):
    source = tmp_path / name
    source.write_bytes(b"source")
    cont = cls(path_task=ResolvedPathTask(source), duration=duration)
    return cont


# --------------------------------------------------------------------------
# 规则配置


def test_rule_grammar_is_platform_colon_id_colon_ratio():
    assert parse_media_speedup_rules(
        ["bilibili:123456:1.25", " douyin : abc : 2 ", "bilibili:777:0.5"]
    ) == {
        (PlatformEnum.BILIBILI, "123456"): 1.25,
        (PlatformEnum.DOUYIN, "abc"): 2.0,
        (PlatformEnum.BILIBILI, "777"): 0.5,
    }


@pytest.mark.parametrize(
    "raw",
    [
        "bilibili",
        "bilibili:123456",
        "bilibili::1.25",
        "bilibili:123456:",
        "bilibili:123456:x",
        "bilibili:123456:1.25:9",
        "nosuchplatform:1:1.25",
    ],
)
def test_malformed_rule_is_rejected(raw):
    with pytest.raises(ValueError, match="变速规则"):
        parse_media_speedup_rules([raw])


def test_later_rule_wins_and_ratio_is_not_capped():
    rules = parse_media_speedup_rules(["bilibili:1:1.2", "bilibili:1:1.5", "bilibili:1:100"])
    assert rules[(PlatformEnum.BILIBILI, "1")] == 100.0


def test_config_rejects_bad_values():
    with pytest.raises(ValueError, match="平台:作者id:倍速"):
        Config(plite_media_speedup_rules=["bilibili=1.2"])
    with pytest.raises(ValueError, match="以 http 开头"):
        Config(plite_sponsorblock_api_base="sb.example.top")


# --------------------------------------------------------------------------
# 写到内容上


@pytest.mark.asyncio
async def test_rule_matches_platform_and_author(tmp_path, monkeypatch):
    monkeypatch.setattr(
        support.pconfig, "plite_media_speedup_rules", ["bilibili:123456:1.5"]
    )
    video = make_media(VideoContent, tmp_path, "v.mp4")
    audio = make_media(AudioContent, tmp_path, "a.mp3")
    apply_media_speedup(make_result(contents=[video, audio]))
    assert (video.speedup, audio.speedup) == (1.5, 1.5)


@pytest.mark.asyncio
async def test_unmatched_author_clears_a_stale_speedup(tmp_path, monkeypatch):
    monkeypatch.setattr(
        support.pconfig, "plite_media_speedup_rules", ["bilibili:999:2.0"]
    )
    video = make_media(VideoContent, tmp_path, "v.mp4")
    video.speedup = 1.5
    result = make_result(author_id="123456", contents=[video])
    apply_media_speedup(result)
    # 结果对象跨消息缓存，重新解析没命中时必须清回 1.0
    assert video.speedup == 1.0


@pytest.mark.asyncio
async def test_repost_matches_the_repost_author(tmp_path, monkeypatch):
    monkeypatch.setattr(
        support.pconfig, "plite_media_speedup_rules", ["bilibili:777:1.5"]
    )
    inner = make_media(VideoContent, tmp_path, "inner.mp4")
    outer = make_media(VideoContent, tmp_path, "outer.mp4")
    result = make_result(contents=[outer], repost=make_result("777", [inner]))
    apply_media_speedup(result)
    assert (outer.speedup, inner.speedup) == (1.0, 1.5)


# --------------------------------------------------------------------------
# 展示换算与产物名


@pytest.mark.parametrize("cls", [VideoContent, AudioContent])
def test_display_duration_divides_the_speedup(cls, tmp_path):
    cont = make_media(cls, tmp_path, "m.mp4", duration=120.0)
    assert cont.display_duration == "2:00"
    cont.speedup = 2.0
    assert cont.display_duration == "1:00"
    assert cont.duration == 120.0, "时长要由倍速换算，不能就地改写"


@pytest.mark.parametrize("cls", [VideoContent, AudioContent])
def test_display_size_is_marked_as_an_estimate(cls, tmp_path):
    cont = make_media(cls, tmp_path, "m.mp4", duration=120.0)
    cont._size_bytes = 2048
    assert asyncio.run(cont.get_display_size()) == "2.00KB"
    cont.speedup = 2.0
    assert asyncio.run(cont.get_display_size()) == "~1.00KB"


def test_speedup_name_is_readable_and_path_sensitive(tmp_path):
    name = _speedup_name(AnyPath(str(tmp_path / "source.mp4")), 1.25)
    assert name.startswith("source-1.25x-")
    assert ":" not in name, "Windows 的文件名不允许冒号"
    other = _speedup_name(AnyPath(str(Path("elsewhere") / "source.mp4")), 1.25)
    assert name != other, "主干相同、目录不同的源不能命中同一个产物"
    assert name != _speedup_name(AnyPath(str(tmp_path / "source.mp4")), 1.5)


# --------------------------------------------------------------------------
# ffmpeg 命令拼装


@pytest.fixture
def recorder(monkeypatch, media_dir):
    """捕获 ffmpeg 调用并真实落一个占位产物，替代 _probe_media 与执行"""
    calls: list[list[str]] = []
    state = {"streams": ["video", "audio"], "fail": None}

    async def fake_probe(path: Path):
        return {
            "format": {"duration": "20.0"},
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
        staticmethod(
            lambda cmd, output_path, max_size_mb, on_progress=None: fake_monitored(
                cmd, output_path, max_size_mb
            )
        ),
    )
    for stale in media_dir.glob("*"):
        stale.unlink()
    return {"calls": calls, "state": state, "media_dir": media_dir}


@pytest.fixture
def media_dir() -> Path:
    return Path(CacheManager.cache_dir(CacheManager.MEDIA))


def filter_of(cmd: list[str]) -> str:
    return cmd[cmd.index("-filter_complex") + 1] if "-filter_complex" in cmd else ""


def test_speedup_video_command_reencodes_and_ends_with_output_path(recorder):
    asyncio.run(FFmpeg.edit_video(Path("src.mp4"), "u_speed", speed_ratio=1.25))
    cmd = recorder["calls"][-1]
    assert filter_of(cmd) == "[0:v]setpts=PTS/1.25[vf];[0:a]atempo=1.25[af]"
    assert cmd[cmd.index("-i") + 1] == "src.mp4"
    # 滤镜必须在输入之后，否则 ffmpeg 把它当成输入选项
    assert cmd.index("-filter_complex") > cmd.index("-i")
    assert cmd[cmd.index("-map") + 1] == "[vf]"
    assert cmd[cmd.index("-c:v") + 1] == "libx264"
    # 缺输出路径时 ffmpeg 只报「unconnected output」，这里直接钉住末位参数
    assert cmd[-1].endswith(".tmp.mp4")
    assert (recorder["media_dir"] / "u_speed.mp4").is_file()


def test_ratio_is_passed_to_ffmpeg_as_is(recorder):
    """不拆级：默认用户 ffmpeg 大于 7，单级 atempo 允许到 100"""
    asyncio.run(FFmpeg.edit_video(Path("src.mp4"), "u_extreme", speed_ratio=4.0))
    assert "atempo=4" in filter_of(recorder["calls"][-1])


def test_silent_source_produces_no_audio_map(recorder):
    recorder["state"]["streams"] = ["video"]
    asyncio.run(FFmpeg.edit_video(Path("src.mp4"), "u_silent", speed_ratio=2.0))
    cmd = recorder["calls"][-1]
    assert "-an" in cmd
    assert "[0:a]" not in filter_of(cmd)


def test_silent_source_with_cuts_and_mutes_does_not_reference_audio(recorder):
    """没有音轨时不能挂引用 [0:a] 的消音滤镜

    滤镜图一旦被拒，整次编辑（含裁切）都会退化成原片。CI 没有 ffmpeg，这条字符串
    断言就是它唯一的防线；真实转码用例在 test_media_edit_real_ffmpeg.py。
    """
    recorder["state"]["streams"] = ["video"]
    asyncio.run(
        FFmpeg.edit_video(
            Path("src.mp4"),
            "u_silentcut",
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


def test_existing_product_is_reused_without_transcoding(recorder):
    product = recorder["media_dir"] / "u_cached.mp4"
    product.write_bytes(b"already")
    asyncio.run(FFmpeg.edit_video(Path("src.mp4"), "u_cached", speed_ratio=2.0))
    assert recorder["calls"] == []
    assert product.read_bytes() == b"already"


def test_edit_audio_reencodes_and_keeps_container(recorder):
    asyncio.run(FFmpeg.edit_audio(Path("src.mp3"), "a_speed", speed_ratio=1.5))
    cmd = recorder["calls"][-1]
    assert cmd[cmd.index("-af") + 1] == "atempo=1.5"
    assert "-vn" in cmd
    assert cmd.index("-af") > cmd.index("-i")
    # 编码器交给 ffmpeg 按输出后缀决定
    assert "-c:a" not in cmd
    assert cmd[-1].endswith(".tmp.mp3")
    assert (recorder["media_dir"] / "a_speed.mp3").is_file()


# --------------------------------------------------------------------------
# get_path 是唯一的执行点


@pytest.mark.asyncio
async def test_get_path_returns_source_when_no_speedup(tmp_path, monkeypatch):
    async def always(cls):
        return True

    called: list[int] = []

    async def spy(cls, *args, **kwargs):
        called.append(1)
        return AnyPath(tmp_path / "product.mp4")

    # 把 ffmpeg 说成可用，才能证明「不变速就直接发原片」是倍速判断挡住的
    monkeypatch.setattr(FFmpeg, "is_available", classmethod(always))
    monkeypatch.setattr(FFmpeg, "edit_video", classmethod(spy))

    cont = make_media(VideoContent, tmp_path, "video.mp4")
    assert await cont.get_path() == await cont.path_task
    assert called == []


@pytest.mark.asyncio
async def test_get_path_returns_the_sped_up_product(tmp_path, monkeypatch):
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

    cont = make_media(VideoContent, tmp_path, "video.mp4")
    cont.speedup = 1.25
    source = await cont.path_task
    assert str(await cont.get_path()) == str(tmp_path / "product.mp4")
    assert seen["input_path"] == source
    assert seen["speed_ratio"] == 1.25
    assert seen["max_size_mb"] == 42
    assert seen["file_name"] == _speedup_name(source, 1.25)


@pytest.mark.asyncio
async def test_audio_speedup_routes_to_the_audio_editor(tmp_path, monkeypatch):
    async def always(cls):
        return True

    async def video_editor(cls, *args, **kwargs):
        raise AssertionError("音频不该走视频编辑器")

    async def audio_editor(cls, input_path, file_name, **kwargs):
        return AnyPath(tmp_path / "product.mp3")

    monkeypatch.setattr(FFmpeg, "is_available", classmethod(always))
    monkeypatch.setattr(FFmpeg, "edit_video", classmethod(video_editor))
    monkeypatch.setattr(FFmpeg, "edit_audio", classmethod(audio_editor))

    cont = make_media(AudioContent, tmp_path, "audio.mp3")
    cont.speedup = 1.5
    assert str(await cont.get_path()) == str(tmp_path / "product.mp3")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [RuntimeError("ffmpeg 执行失败"), SizeLimitException(120.0)]
)
async def test_failure_falls_back_to_source_and_drops_speedup(
    tmp_path, monkeypatch, error
):
    async def always(cls):
        return True

    async def failing(cls, *args, **kwargs):
        raise error

    monkeypatch.setattr(FFmpeg, "is_available", classmethod(always))
    monkeypatch.setattr(FFmpeg, "edit_video", classmethod(failing))

    cont = make_media(VideoContent, tmp_path, "video.mp4")
    cont.speedup = 1.25
    source = await cont.path_task
    assert await cont.get_path() == source
    # 退回原片后必须丢掉倍速，否则卡片继续按「已处理」显示时长与体积
    assert cont.speedup == 1.0
    assert cont.display_duration == "1:00"


@pytest.mark.asyncio
async def test_missing_ffmpeg_skips_speedup_and_drops_it(tmp_path, monkeypatch):
    async def never(cls):
        return False

    called: list[int] = []

    async def spy(cls, *args, **kwargs):
        called.append(1)
        return AnyPath(tmp_path / "product.mp4")

    monkeypatch.setattr(FFmpeg, "is_available", classmethod(never))
    monkeypatch.setattr(FFmpeg, "edit_video", classmethod(spy))

    cont = make_media(VideoContent, tmp_path, "video.mp4")
    cont.speedup = 1.25
    assert await cont.get_path() == await cont.path_task
    assert called == [], "缺 ffmpeg 时不该调用转码"
    assert cont.speedup == 1.0


async def _peak_concurrency(tmp_path, monkeypatch, limit: int) -> int:
    monkeypatch.setattr(support.pconfig, "plite_media_transcode_concurrency", limit)
    monkeypatch.setattr(ffmpeg_module, "_gate", None)

    running = 0
    peak = 0

    async def always(cls):
        return True

    async def slow(cls, input_path, file_name, **kwargs):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.02)
        running -= 1
        return AnyPath(tmp_path / "product.mp4")

    monkeypatch.setattr(FFmpeg, "is_available", classmethod(always))
    monkeypatch.setattr(FFmpeg, "edit_video", classmethod(slow))

    conts = [make_media(VideoContent, tmp_path, "video.mp4") for _ in range(4)]
    for cont in conts:
        cont.speedup = 1.25
    await asyncio.gather(*(cont.get_path() for cont in conts))
    return peak


@pytest.mark.asyncio
async def test_transcode_gate_serialises_at_one(tmp_path, monkeypatch):
    assert await _peak_concurrency(tmp_path, monkeypatch, 1) == 1


@pytest.mark.asyncio
async def test_transcode_gate_allows_configured_parallelism(tmp_path, monkeypatch):
    assert await _peak_concurrency(tmp_path, monkeypatch, 2) == 2


# --------------------------------------------------------------------------
# 发送与卡片


def capture_segments(monkeypatch) -> list:
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
async def test_send_content_uses_the_sped_up_product(
    monkeypatch, tmp_path, need_upload, in_forward
):
    monkeypatch.setattr(support.pconfig, "plite_need_upload_video", need_upload)
    monkeypatch.setattr(support.pconfig, "plite_need_upload", False)
    monkeypatch.setattr(support.pconfig, "plite_video_in_forward", in_forward)
    monkeypatch.setattr(support.pconfig, "plite_need_forward_contents", False)

    async def always(cls):
        return True

    async def fake_retime(cls, input_path, file_name, **kwargs):
        product = tmp_path / "product.mp4"
        product.write_bytes(b"edited")
        return AnyPath(product)

    monkeypatch.setattr(FFmpeg, "is_available", classmethod(always))
    monkeypatch.setattr(FFmpeg, "edit_video", classmethod(fake_retime))

    seen = capture_segments(monkeypatch)
    cont = make_media(VideoContent, tmp_path, "video.mp4")
    cont.speedup = 1.25
    result = make_result(contents=[cont])

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
    cont = make_media(VideoContent, tmp_path, "video.mp4")
    cont.speedup = 1.25
    result = make_result(contents=[cont])

    async for _ in support.Renderer().send_content(result):
        pass
    assert [str(path) for path in seen] == [str(await cont.path_task)]


@pytest.mark.asyncio
async def test_card_cache_key_changes_with_the_speedup(monkeypatch, tmp_path):
    """卡片是按倍速换算出来的时长渲染的，倍速变了就必须换一张卡

    持久化的卡片缓存跨重启复用，键里若没有倍速标记，改了配置后老卡片会一直显示
    旧时长。
    """
    renders: list = []

    async def fake_render_image(self, result, **kwargs):
        renders.append(speedup_digest(result))
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

    cont = make_media(VideoContent, tmp_path, "video.mp4")
    cont.speedup = 1.25
    result = make_result(contents=[cont])

    await support.Renderer().cache_or_render_image(result)
    await support.Renderer().cache_or_render_image(result)
    assert len(renders) == 1, "同一倍速应命中卡片缓存"

    cont.speedup = 1.5
    await support.Renderer().cache_or_render_image(result)
    assert len(renders) == 2, "倍速变化后必须重渲染卡片"

    cont.speedup = 1.0
    await support.Renderer().cache_or_render_image(result)
    assert len(renders) == 3
