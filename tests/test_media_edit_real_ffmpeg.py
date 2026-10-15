"""用真实 ffmpeg/ffprobe 验证产物时长与消音区间。

命令拼装用例只看字符串，无法证明滤镜图能被 ffmpeg 接受、也无法证明时长换算真的
落到文件上。这里在具备可执行文件时跑一次真实转码；CI 若无 ffmpeg 会整模块跳过，
此时以命令拼装用例为准。
"""

from pathlib import Path
import re
import shutil
import subprocess

from anyio import Path as AnyPath
import pytest
import render_test_support as support
from render_test_support import ResolvedPathTask

from nonebot_plugin_parser_lite.data import AudioContent, VideoContent
from nonebot_plugin_parser_lite.parsers.bilibili.sponsorblock import (
    skip_ranges,
    skip_video,
)

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")

pytestmark = pytest.mark.skipif(
    FFMPEG is None or FFPROBE is None,
    reason="需要 ffmpeg 与 ffprobe 可执行文件",
)

SOURCE_SECONDS = 20.0


def probe_duration(path: Path) -> float:
    result = subprocess.run(
        [
            FFPROBE,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        check=True,
    )
    return float(result.stdout.strip())


def mean_volume(path: Path, start: float) -> float:
    result = subprocess.run(
        [
            FFMPEG,
            "-hide_banner",
            "-nostats",
            "-ss",
            str(start),
            "-t",
            "1.2",
            "-i",
            str(path),
            "-af",
            "volumedetect",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    match = re.search(r"mean_volume: (-?[\d.]+) dB", result.stderr)
    assert match is not None, result.stderr[-300:]
    return float(match.group(1))


def probe_audio_streams(path: Path) -> int:
    result = subprocess.run(
        [
            FFPROBE,
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        check=True,
    )
    return len([line for line in result.stdout.splitlines() if line.strip() == "audio"])


def _encode(args: list[str], path: Path) -> Path:
    subprocess.run(
        [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", *args, str(path)],
        stdin=subprocess.DEVNULL,
        check=True,
    )
    return path


@pytest.fixture
def source_video(tmp_path: Path) -> Path:
    lavfi = [
        "-f",
        "lavfi",
        "-i",
        f"testsrc=duration={SOURCE_SECONDS}:size=160x120:rate=25",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=440:duration={SOURCE_SECONDS}",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-shortest",
    ]
    return _encode(lavfi, tmp_path / "source.mp4")


@pytest.fixture
def silent_video(tmp_path: Path) -> Path:
    """没有音轨的视频：用来证明滤镜图不会引用 [0:a]"""
    lavfi = [
        "-f",
        "lavfi",
        "-i",
        "testsrc=duration=10:size=160x120:rate=25",
        "-an",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
    ]
    return _encode(lavfi, tmp_path / "silent.mp4")


@pytest.fixture
def source_audio(tmp_path: Path) -> Path:
    lavfi = [
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=8",
        "-c:a",
        "libmp3lame",
        "-q:a",
        "2",
    ]
    return _encode(lavfi, tmp_path / "tone.mp3")


@pytest.fixture(autouse=True)
def single_transcode(monkeypatch):
    monkeypatch.setattr(support.pconfig, "plite_media_transcode_concurrency", 1)


@pytest.mark.asyncio
async def test_real_speedup_matches_planned_duration(source_video: Path):
    cont = VideoContent(
        path_task=ResolvedPathTask(source_video),  # type: ignore[arg-type]
        duration=SOURCE_SECONDS,
    )
    cont.speedup = 2.0

    product = Path(str(await cont.get_path()))
    assert product != source_video, "处理失败会退回原片，这里不该发生"
    assert probe_duration(product) == pytest.approx(SOURCE_SECONDS / 2.0, abs=0.4)
    assert cont.display_duration == "0:10"
    assert Path(str(await cont.get_path())) == product, "相同倍速应复用产物"


@pytest.mark.asyncio
async def test_real_speedup_is_a_quarter_as_long(source_audio: Path):
    """4 倍速的产物时长必须真的是 1/4（单级 atempo 在 ffmpeg 4.3+ 就允许到 100）"""
    cont = AudioContent(
        path_task=ResolvedPathTask(source_audio),  # type: ignore[arg-type]
        duration=8.0,
    )
    cont.speedup = 4.0
    product = Path(str(await cont.get_path()))
    assert product.suffix == ".mp3", "产物沿用源容器"
    assert probe_duration(product) == pytest.approx(2.0, abs=0.5)
    assert probe_audio_streams(product) == 1


@pytest.mark.asyncio
async def test_real_skip_cuts_and_mutes_the_annotated_ranges(source_video: Path):
    """裁掉开头 5 秒 + 8-10 秒消音，产物时长与静音位置都要真的对上"""
    product = Path(
        str(
            await skip_video(
                AnyPath(str(source_video)),
                ((0.0, 5.0),),
                ((8.0, 10.0),),
            )
        )
    )
    assert product != source_video
    assert product.suffix == ".mp4", "空降成片统一封装成 mp4"
    assert probe_duration(product) == pytest.approx(15.0, abs=0.4)
    # 原片 8-10 秒被消音，裁掉开头 5 秒后落在产物时间轴的 3 秒起
    assert mean_volume(product, 3.2) < -60.0
    assert mean_volume(product, 6.0) > -50.0


@pytest.mark.asyncio
async def test_real_skip_on_silent_video_still_edits(silent_video: Path):
    """静音源 + 同时有裁切和消音区间时，编辑必须真的发生

    早先的滤镜图会无条件挂上引用 [0:a] 的消音滤镜，ffmpeg 直接拒掉整张图，
    结果是裁切一起退化成原片——只有真实转码才能暴露这一点。
    """
    product = Path(
        str(
            await skip_video(
                AnyPath(str(silent_video)), ((2.0, 4.0),), ((6.0, 7.0),)
            )
        )
    )
    assert product != silent_video, "不该退化成原片"
    assert probe_duration(product) == pytest.approx(8.0, abs=0.4)
    assert probe_audio_streams(product) == 0


@pytest.mark.asyncio
async def test_real_skip_end_to_end_from_the_annotation_service(
    source_video: Path, monkeypatch
):
    """查询 → 换算区间 → 产出成片：整条链路按真实二进制跑一遍"""
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import json
    import threading

    rows = [
        {
            "segment": [0.0, 5.0],
            "category": "sponsor",
            "actionType": "skip",
            "videoDuration": SOURCE_SECONDS,
        },
        {
            "segment": [8.0, 10.0],
            "category": "intro",
            "actionType": "mute",
            "videoDuration": SOURCE_SECONDS,
        },
    ]
    requests: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            body = json.dumps(rows).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        cuts, mutes, removed = await skip_ranges(
            f"http://127.0.0.1:{server.server_address[1]}",
            ["sponsor", "intro"],
            "BV1xx411c7mD",
            "987654",
            SOURCE_SECONDS,
        )
    finally:
        server.shutdown()
        server.server_close()

    assert "videoID=BV1xx411c7mD" in requests[0]
    assert "cid=987654" in requests[0]
    assert cuts == ((0.0, 5.0),)
    assert mutes == ((8.0, 10.0),)
    assert removed == pytest.approx(5.0)

    product = Path(str(await skip_video(AnyPath(str(source_video)), cuts, mutes)))
    assert probe_duration(product) == pytest.approx(15.0, abs=0.4)
    assert mean_volume(product, 3.2) < -60.0

    # 换算后的时长直接进卡片
    cont = VideoContent(
        path_task=ResolvedPathTask(source_video),  # type: ignore[arg-type]
        duration=SOURCE_SECONDS - removed,
    )
    assert cont.display_duration == "0:15"


@pytest.mark.asyncio
async def test_send_path_uses_product_and_plain_source_when_unset(
    source_video: Path, monkeypatch
):
    from nonebot_plugin_parser_lite.render import Renderer

    monkeypatch.setattr(support.pconfig, "plite_need_upload_video", False)
    monkeypatch.setattr(support.pconfig, "plite_need_upload", False)
    monkeypatch.setattr(support.pconfig, "plite_video_in_forward", False)
    monkeypatch.setattr(support.pconfig, "plite_need_forward_contents", False)

    used: list = []

    async def video_seg(file, thumbnail=None):
        from nonebot_plugin_alconna.uniseg import Video

        used.append(Path(str(file)))
        return Video(raw=b"video")

    async def img_seg(file):
        from nonebot_plugin_alconna.uniseg import Image

        return Image(raw=b"cover")

    monkeypatch.setattr(support.UniHelper, "video_seg", video_seg)
    monkeypatch.setattr(support.UniHelper, "img_seg", img_seg)

    cont = VideoContent(
        path_task=ResolvedPathTask(source_video),  # type: ignore[arg-type]
        cover=ResolvedPathTask(source_video),  # type: ignore[arg-type]
        duration=SOURCE_SECONDS,
    )
    result = support.make_result()
    result.content = [cont]

    cont.speedup = 2.0
    async for _ in Renderer().send_content(result):
        pass
    assert len(used) == 1
    assert used[0] != source_video, "发送链路应使用处理后的产物"
    assert probe_duration(used[0]) == pytest.approx(10.0, abs=0.4)

    cont.speedup = 1.0
    used.clear()
    async for _ in Renderer().send_content(result):
        pass
    assert used == [source_video], "没有倍速时应直接发原片"
