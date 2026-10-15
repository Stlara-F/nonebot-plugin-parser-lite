"""用真实 ffmpeg/ffprobe 验证产物时长与消音区间。

上面的命令拼装测试只看字符串，无法证明滤镜图能被 ffmpeg 接受、也无法证明
时长换算真的落到文件上。这里在具备可执行文件时跑一次真实转码；CI 若无
ffmpeg 会整模块跳过，此时以命令拼装测试为准。
"""

from __future__ import annotations

from pathlib import Path
import re
import shutil
import subprocess

import pytest
import render_test_support as support
from render_test_support import ResolvedPathTask

from nonebot_plugin_parser_lite.data import VideoContent, VideoEditPlan
from nonebot_plugin_parser_lite.preprocess.apply import apply_edit_plan

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


@pytest.fixture
def source_video(tmp_path: Path) -> Path:
    path = tmp_path / "source.mp4"
    subprocess.run(
        [
            FFMPEG,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
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
            str(path),
        ],
        stdin=subprocess.DEVNULL,
        check=True,
    )
    return path


@pytest.mark.asyncio
async def test_real_transcode_matches_planned_duration(source_video: Path, monkeypatch):
    monkeypatch.setattr(support.pconfig, "plite_media_transcode_concurrency", 1)

    cont = VideoContent(
        path_task=ResolvedPathTask(source_video),  # type: ignore[arg-type]
        duration=SOURCE_SECONDS,
    )
    # 裁掉开头 5 秒只留一个片段，同时 1.25 倍速
    plan = VideoEditPlan(
        speed_ratio=1.25,
        cut_ranges=((0.0, 5.0),),
        mute_ranges=((8.0, 10.0),),
    )
    cont.edit_plan = plan

    product = await apply_edit_plan(cont, source_video)
    assert Path(product) != source_video, "处理失败会退回原片，这里不该发生"

    actual = probe_duration(Path(product))
    assert actual == pytest.approx(plan.output_duration(SOURCE_SECONDS), abs=0.4)

    # 原片 8-10 秒被消音，映射到产物时间轴是 (8-5)/1.25 = 2.4 秒起
    assert mean_volume(Path(product), 2.6) < -60.0
    assert mean_volume(Path(product), 5.0) > -50.0


class _StubSponsorBlock:
    """本地 stub，用来验证身份透传与查询真的发生"""

    def __init__(self, payload: list[dict]):
        self.payload = payload
        self.requests: list[str] = []
        self._server = None
        self._thread = None

    def __enter__(self):
        from http.server import BaseHTTPRequestHandler, HTTPServer
        import json
        import threading

        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.requests.append(self.path)
                body = json.dumps(outer.payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.port = self._server.server_address[1]
        return self

    def __exit__(self, *exc_info):
        self._server.shutdown()
        self._server.server_close()
        return False


@pytest.mark.asyncio
async def test_plan_then_apply_end_to_end_with_real_ffmpeg(
    source_video: Path, monkeypatch
):
    from nonebot_plugin_parser_lite.constants import PlatformEnum
    from nonebot_plugin_parser_lite.data import Author, ParseResult, Platform
    from nonebot_plugin_parser_lite.preprocess import plan_media_edits

    monkeypatch.setattr(support.pconfig, "plite_media_speedup_rules", [
        "bilibili:mid:123456=1.25"
    ])
    monkeypatch.setattr(support.pconfig, "plite_sponsorblock_categories", [
        "sponsor",
        "intro",
    ])
    monkeypatch.setattr(support.pconfig, "plite_media_transcode_concurrency", 1)

    segments = [
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

    cont = VideoContent(
        path_task=ResolvedPathTask(source_video),  # type: ignore[arg-type]
        duration=SOURCE_SECONDS,
    )
    cont.origin_ids = {"bvid": "BV1xx411c7mD", "cid": "987654"}
    result = ParseResult(
        platform=Platform(PlatformEnum.BILIBILI, "哔哩哔哩"),
        author=Author("someone", id="123456"),
        url="https://www.bilibili.com/video/BV1xx411c7mD",
        content=[cont],
    )

    with _StubSponsorBlock(segments) as stub:
        monkeypatch.setattr(
            support.pconfig,
            "plite_sponsorblock_api_base",
            f"http://127.0.0.1:{stub.port}",
        )
        await plan_media_edits(result)

        assert "videoID=BV1xx411c7mD" in stub.requests[0]
        assert "cid=987654" in stub.requests[0]

    plan = cont.edit_plan
    assert plan is not None
    assert plan.cut_ranges == ((0.0, 5.0),)
    assert plan.mute_ranges == ((8.0, 10.0),)
    assert cont.display_duration == "0:12"

    product = await apply_edit_plan(cont, await cont.get_path())
    assert Path(product) != source_video
    assert probe_duration(Path(product)) == pytest.approx(
        plan.output_duration(SOURCE_SECONDS), abs=0.4
    )
    # 原片 8-10 秒消音，映射到产物时间轴是 (8-5)/1.25 = 2.4 秒起
    assert mean_volume(Path(product), 2.6) < -60.0
    assert mean_volume(Path(product), 5.0) > -50.0

    again = await apply_edit_plan(cont, await cont.get_path())
    assert str(again) == str(product), "相同计划应复用产物"


@pytest.mark.asyncio
async def test_real_send_path_uses_product_and_falls_back_when_disabled(
    source_video: Path, monkeypatch
):
    from nonebot_plugin_parser_lite.render import Renderer

    monkeypatch.setattr(support.pconfig, "plite_need_upload_video", False)
    monkeypatch.setattr(support.pconfig, "plite_need_upload", False)
    monkeypatch.setattr(support.pconfig, "plite_video_in_forward", False)
    monkeypatch.setattr(support.pconfig, "plite_need_forward_contents", False)
    monkeypatch.setattr(support.pconfig, "plite_media_speedup_rules", [
        "bilibili:mid:123456=1.25"
    ])

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
    cont.edit_plan = VideoEditPlan(speed_ratio=2.0)
    result = support.make_result()
    result.content = [cont]

    async for _ in Renderer().send_content(result):
        pass
    assert len(used) == 1
    assert used[0] != source_video, "发送链路应使用处理后的产物"
    assert probe_duration(used[0]) == pytest.approx(10.0, abs=0.4)

    cont.edit_plan = None
    used.clear()
    async for _ in Renderer().send_content(result):
        pass
    assert used == [source_video], "没有计划时应直接发原片"
