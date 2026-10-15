from __future__ import annotations

import pytest
import render_test_support as support
from render_test_support import ResolvedPathTask

from nonebot_plugin_parser_lite.config import (
    Config,
    parse_media_speedup_rule,
    parse_media_speedup_rules,
)
from nonebot_plugin_parser_lite.constants import PlatformEnum
from nonebot_plugin_parser_lite.data import (
    Author,
    ParseResult,
    Platform,
    VideoContent,
)
from nonebot_plugin_parser_lite.preprocess import planner
from nonebot_plugin_parser_lite.preprocess.sponsorblock import SponsorSegment


def make_video(
    duration: float = 600.0,
    origin_ids: dict[str, str] | None = None,
) -> VideoContent:
    cont = VideoContent(
        path_task=ResolvedPathTask(support.SyncPath("video.mp4")),  # type: ignore[arg-type]
        duration=duration,
    )
    if origin_ids:
        cont.origin_ids = dict(origin_ids)
    return cont


def make_result(
    platform: PlatformEnum = PlatformEnum.BILIBILI,
    author_id: str | None = "123456",
    contents: list[object] | None = None,
    repost: ParseResult | None = None,
    duration: float = 600.0,
) -> ParseResult:
    video = make_video(duration, {"bvid": "BV1xx411c7mD", "cid": "987654"})
    return ParseResult(
        platform=Platform(platform, platform.value),
        author=Author("tester", id=author_id),
        url="https://example.com/video",
        content=list(contents or [video]),  # type: ignore[arg-type]
        repost=repost,  # type: ignore[arg-type]
    )


def test_rule_grammar_accepts_documented_aliases():
    for raw in (
        "bilibili:author:123=1.25",
        "bilibili:mid:123=1.25",
        "bilibili:uid:123=1.25",
        "bilibili:id:123=1.25",
        " douyin:user_id:abc = 2 ",
    ):
        rule = parse_media_speedup_rule(raw)
        assert rule.author_id in {"123", "abc"}
        assert rule.ratio in {1.25, 2.0}


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("bilibili", "缺少 '='"),
        ("bilibili=1.25", "三段"),
        ("bilibili:author:123", "缺少 '='"),
        ("bilibili:author:extra:123=1.25", "三段"),
        ("bilibili:name:123=1.25", "未知的作者标识写法"),
        ("bilibili:author:=1.25", "作者 id 为空"),
        ("nosuchplatform:author:123=1.25", "平台不存在"),
        ("bilibili:author:123=x", "不是数字"),
        ("bilibili:author:123=0", "倍速需在"),
        ("bilibili:author:123=0.25", "倍速需在"),
        ("bilibili:author:123=100", "倍速需在"),
    ],
)
def test_rule_grammar_rejects_malformed(raw, reason):
    with pytest.raises(ValueError, match=reason):
        parse_media_speedup_rule(raw)


def test_conflicting_ratio_for_same_author_is_rejected():
    with pytest.raises(ValueError, match="互相矛盾"):
        parse_media_speedup_rules(["bilibili:mid:1=1.2", "bilibili:uid:1=1.5"])


def test_duplicate_identical_rule_is_collapsed():
    rules = parse_media_speedup_rules(["bilibili:mid:1=1.25", "bilibili:mid:1=1.25"])
    assert len(rules) == 1


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"plite_sponsorblock_api_base": "sb.example.top"}, "http:// 或 https://"),
        ({"plite_sponsorblock_api_base": ""}, "不能是空字符串"),
        ({"plite_sponsorblock_api_base": "https://sb.example.top"}, "至少一个片段分类"),
        (
            {
                "plite_sponsorblock_api_base": "https://sb.example.top",
                "plite_sponsorblock_categories": ["bogus"],
            },
            "未知的片段分类",
        ),
        ({"plite_sponsorblock_categories": ["sponsor"]}, "时必须同时配置 SponsorBlock 服务地址"),
        ({"plite_media_transcode_concurrency": 0}, "转码并发数至少为 1"),
    ],
)
def test_sponsorblock_config_is_validated(kwargs, reason):
    with pytest.raises(ValueError, match=reason):
        Config(**kwargs)


def test_sponsorblock_config_normalizes_base_and_categories():
    config = Config(
        plite_sponsorblock_api_base="https://sb.example.top/",
        plite_sponsorblock_categories=["Sponsor", " selfpromotion "],
    )
    assert config.sponsorblock_api_base == "https://sb.example.top"
    assert config.sponsorblock_categories == ["sponsor", "selfpromotion"]


@pytest.mark.asyncio
async def test_planner_does_nothing_when_unconfigured(monkeypatch):
    called = False

    async def fail_fetch(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("不应该发起片段查询")

    monkeypatch.setattr(planner, "fetch_segments", fail_fetch)
    monkeypatch.setattr(support.pconfig, "plite_media_speedup_rules", [])
    monkeypatch.setattr(support.pconfig, "plite_sponsorblock_api_base", None)

    result = make_result()
    await planner.plan_media_edits(result)
    assert called is False
    assert result.content[0].edit_plan is None  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_speedup_rule_matches_platform_and_author(monkeypatch):
    monkeypatch.setattr(
        support.pconfig, "plite_media_speedup_rules", ["bilibili:mid:123456=1.25"]
    )
    result = make_result()
    await planner.plan_media_edits(result)
    plan = result.content[0].edit_plan  # type: ignore[union-attr]
    assert plan is not None
    assert plan.speed_ratio == 1.25
    assert plan.cut_ranges == ()


@pytest.mark.asyncio
async def test_speedup_rule_ignores_other_author_and_platform(monkeypatch):
    monkeypatch.setattr(
        support.pconfig, "plite_media_speedup_rules", ["bilibili:mid:999=1.25"]
    )
    result = make_result()
    await planner.plan_media_edits(result)
    assert result.content[0].edit_plan is None  # type: ignore[union-attr]

    monkeypatch.setattr(
        support.pconfig, "plite_media_speedup_rules", ["douyin:mid:123456=1.25"]
    )
    await planner.plan_media_edits(result)
    assert result.content[0].edit_plan is None  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_missing_author_id_never_matches(monkeypatch):
    monkeypatch.setattr(
        support.pconfig, "plite_media_speedup_rules", ["bilibili:mid:123456=1.25"]
    )
    result = make_result(author_id=None)
    await planner.plan_media_edits(result)
    assert result.content[0].edit_plan is None  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_repost_video_matches_the_repost_author(monkeypatch):
    monkeypatch.setattr(
        support.pconfig, "plite_media_speedup_rules", ["bilibili:mid:777=1.5"]
    )
    inner_video = make_video(origin_ids={"bvid": "BV1yy", "cid": "1"})
    repost = ParseResult(
        platform=Platform(PlatformEnum.BILIBILI, "bilibili"),
        author=Author("reposted", id="777"),
        url="https://example.com/inner",
        content=[inner_video],
    )
    result = make_result(author_id="123456", repost=repost)
    await planner.plan_media_edits(result)

    assert result.content[0].edit_plan is None  # type: ignore[union-attr]
    plan = repost.content[0].edit_plan  # type: ignore[union-attr]
    assert plan is not None
    assert plan.speed_ratio == 1.5


@pytest.mark.asyncio
async def test_replanning_clears_a_stale_plan(monkeypatch):
    monkeypatch.setattr(
        support.pconfig, "plite_media_speedup_rules", ["bilibili:mid:123456=1.25"]
    )
    result = make_result()
    await planner.plan_media_edits(result)
    assert result.content[0].edit_plan is not None  # type: ignore[union-attr]

    monkeypatch.setattr(support.pconfig, "plite_media_speedup_rules", [])
    await planner.plan_media_edits(result)
    assert result.content[0].edit_plan is None  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_sponsorblock_ranges_are_split_by_action_type(monkeypatch):
    monkeypatch.setattr(support.pconfig, "plite_media_speedup_rules", [])
    monkeypatch.setattr(
        support.pconfig, "plite_sponsorblock_api_base", "https://sb.example.top"
    )
    monkeypatch.setattr(
        support.pconfig, "plite_sponsorblock_categories", ["sponsor", "intro"]
    )

    fetched: dict[str, object] = {}

    async def fake_fetch(base_url, video_id, cid, categories):
        fetched["base"] = base_url
        fetched["video_id"] = video_id
        fetched["cid"] = cid
        fetched["categories"] = categories
        return (
            [
                SponsorSegment(0.0, 30.0, "sponsor", "skip"),
                SponsorSegment(60.0, 75.0, "intro", "mute"),
                SponsorSegment(90.0, 100.0, "sponsor", "full"),
                SponsorSegment(200.0, 260.0, "outro", "skip"),
            ],
            {"sponsor": 600.0, "intro": 600.0, "outro": 600.0},
        )

    monkeypatch.setattr(planner, "fetch_segments", fake_fetch)

    result = make_result()
    await planner.plan_media_edits(result)
    plan = result.content[0].edit_plan  # type: ignore[union-attr]

    assert fetched == {
        "base": "https://sb.example.top",
        "video_id": "BV1xx411c7mD",
        "cid": "987654",
        "categories": ["sponsor", "intro"],
    }
    assert plan is not None
    # outro 不在白名单里；full 不是可裁切动作
    assert plan.cut_ranges == ((0.0, 30.0),)
    assert plan.mute_ranges == ((60.0, 75.0),)


@pytest.mark.asyncio
async def test_stale_segments_are_dropped(monkeypatch):
    monkeypatch.setattr(support.pconfig, "plite_media_speedup_rules", [])
    monkeypatch.setattr(
        support.pconfig, "plite_sponsorblock_api_base", "https://sb.example.top"
    )
    monkeypatch.setattr(
        support.pconfig, "plite_sponsorblock_categories", ["sponsor"]
    )

    async def stale_fetch(base_url, video_id, cid, categories):
        return (
            [SponsorSegment(0.0, 30.0, "sponsor", "skip")],
            {"sponsor": 300.0},
        )

    monkeypatch.setattr(planner, "fetch_segments", stale_fetch)
    result = make_result(duration=600.0)
    await planner.plan_media_edits(result)
    assert result.content[0].edit_plan is None  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_ranges_are_clamped_sorted_and_merged(monkeypatch):
    monkeypatch.setattr(support.pconfig, "plite_media_speedup_rules", [])
    monkeypatch.setattr(
        support.pconfig, "plite_sponsorblock_api_base", "https://sb.example.top"
    )
    monkeypatch.setattr(
        support.pconfig, "plite_sponsorblock_categories", ["sponsor"]
    )

    async def messy_fetch(base_url, video_id, cid, categories):
        return (
            [
                SponsorSegment(500.0, 520.0, "sponsor", "skip"),
                SponsorSegment(100.0, 160.0, "sponsor", "skip"),
                SponsorSegment(150.0, 180.0, "sponsor", "skip"),
                SponsorSegment(-10.0, 20.0, "sponsor", "skip"),
                SponsorSegment(590.0, 900.0, "sponsor", "skip"),
            ],
            {"sponsor": None},
        )

    monkeypatch.setattr(planner, "fetch_segments", messy_fetch)
    result = make_result(duration=600.0)
    await planner.plan_media_edits(result)
    plan = result.content[0].edit_plan  # type: ignore[union-attr]
    assert plan is not None
    assert plan.cut_ranges == ((0.0, 20.0), (100.0, 180.0), (500.0, 520.0), (590.0, 600.0))


@pytest.mark.asyncio
async def test_cut_covering_whole_video_is_abandoned(monkeypatch):
    monkeypatch.setattr(support.pconfig, "plite_media_speedup_rules", [])
    monkeypatch.setattr(
        support.pconfig, "plite_sponsorblock_api_base", "https://sb.example.top"
    )
    monkeypatch.setattr(
        support.pconfig, "plite_sponsorblock_categories", ["sponsor"]
    )

    async def greedy_fetch(base_url, video_id, cid, categories):
        return (
            [SponsorSegment(0.0, 600.0, "sponsor", "skip")],
            {"sponsor": 600.0},
        )

    monkeypatch.setattr(planner, "fetch_segments", greedy_fetch)
    result = make_result(duration=600.0)
    await planner.plan_media_edits(result)
    assert result.content[0].edit_plan is None  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_speedup_and_sponsorblock_compose(monkeypatch):
    monkeypatch.setattr(
        support.pconfig, "plite_media_speedup_rules", ["bilibili:mid:123456=2.0"]
    )
    monkeypatch.setattr(
        support.pconfig, "plite_sponsorblock_api_base", "https://sb.example.top"
    )
    monkeypatch.setattr(
        support.pconfig, "plite_sponsorblock_categories", ["sponsor"]
    )

    async def fake_fetch(base_url, video_id, cid, categories):
        return (
            [SponsorSegment(0.0, 60.0, "sponsor", "skip")],
            {"sponsor": 600.0},
        )

    monkeypatch.setattr(planner, "fetch_segments", fake_fetch)
    result = make_result(duration=600.0)
    await planner.plan_media_edits(result)
    plan = result.content[0].edit_plan  # type: ignore[union-attr]
    assert plan is not None
    assert plan.output_duration(600.0) == pytest.approx(270.0)
    assert result.content[0].display_duration == "4:30"  # type: ignore[union-attr]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("platform", "origin_ids", "duration"),
    [
        (PlatformEnum.BILIBILI, {}, 600.0),
        (PlatformEnum.BILIBILI, {"bvid": "BV1", "cid": "2"}, 0.0),
        (PlatformEnum.DOUYIN, {"bvid": "BV1", "cid": "2"}, 600.0),
    ],
)
async def test_sponsorblock_lookup_is_skipped_without_identity(
    monkeypatch, platform, origin_ids, duration
):
    monkeypatch.setattr(support.pconfig, "plite_media_speedup_rules", [])
    monkeypatch.setattr(
        support.pconfig, "plite_sponsorblock_api_base", "https://sb.example.top"
    )
    monkeypatch.setattr(
        support.pconfig, "plite_sponsorblock_categories", ["sponsor"]
    )

    async def spy_fetch(*args, **kwargs):
        raise AssertionError("不该发起查询")

    monkeypatch.setattr(planner, "fetch_segments", spy_fetch)

    video = make_video(duration=duration, origin_ids=origin_ids)
    result = make_result(platform=platform, contents=[video])
    await planner.plan_media_edits(result)
    assert video.edit_plan is None


def test_response_parser_accepts_both_documented_shapes():
    from nonebot_plugin_parser_lite.preprocess.sponsorblock import (
        _iter_segment_dicts,
        _parse_segment,
    )

    flat = [{"segment": [1.0, 5.0], "category": "Sponsor", "actionType": "Skip"}]
    grouped = [{"videoID": "BV1", "segments": [{"segment": [2, 9], "category": "intro", "actionType": "mute"}]}]

    flat_entries = _iter_segment_dicts(flat)
    grouped_entries = _iter_segment_dicts(grouped)
    assert len(flat_entries) == 1
    assert len(grouped_entries) == 1

    segment, recorded = _parse_segment(flat_entries[0])
    assert (segment.start, segment.end) == (1.0, 5.0)
    assert segment.category == "sponsor"
    assert segment.action_type == "skip"
    assert recorded is None

    segment, _ = _parse_segment(grouped_entries[0])
    assert (segment.start, segment.end) == (2.0, 9.0)
    assert segment.action_type == "mute"


@pytest.mark.parametrize(
    "entry",
    [
        {},
        {"segment": [1.0], "category": "sponsor", "actionType": "skip"},
        {"segment": ["a", "b"], "category": "sponsor", "actionType": "skip"},
        {"segment": [5.0, 1.0], "category": "sponsor", "actionType": "skip"},
        {"segment": [-5.0, 1.0], "category": "sponsor", "actionType": "skip"},
        {"segment": [1.0, 2.0], "actionType": "skip"},
        {"segment": [1.0, 2.0], "category": "sponsor"},
    ],
)
def test_malformed_segments_are_rejected(entry):
    from nonebot_plugin_parser_lite.preprocess.sponsorblock import _parse_segment

    assert _parse_segment(entry) is None


class _FakeResponse:
    def __init__(self, status_code, payload=None, raw=None):
        self.status_code = status_code
        self._payload = payload
        self._raw = raw

    def json(self):
        import json

        if self._payload is not None:
            return self._payload
        raise json.JSONDecodeError("boom", self._raw or "", 0)


class _FakeClient:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.requests = []

    def __call__(self, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def get(self, url, params=None):
        self.requests.append((url, params))
        if self.error is not None:
            raise self.error
        return self.response


@pytest.mark.asyncio
async def test_fetch_segments_builds_query_and_returns_parsed_rows(monkeypatch):
    from nonebot_plugin_parser_lite.preprocess import sponsorblock

    client = _FakeClient(
        _FakeResponse(
            200,
            [
                {
                    "segment": [1.0, 4.0],
                    "category": "sponsor",
                    "actionType": "skip",
                    "videoDuration": 600,
                }
            ],
        )
    )
    monkeypatch.setattr(sponsorblock.httpx, "AsyncClient", client)

    segments, recorded = await sponsorblock.fetch_segments(
        "https://sb.example.top", "BV1xx", "987", ["sponsor"]
    )
    url, params = client.requests[0]
    assert url == "https://sb.example.top/api/skipSegments"
    assert params["videoID"] == "BV1xx"
    assert params["cid"] == "987"
    assert params["categories"] == '["sponsor"]'
    assert [(s.start, s.end, s.action_type) for s in segments] == [(1.0, 4.0, "skip")]
    assert recorded == {"sponsor": 600.0}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "client",
    [
        _FakeClient(_FakeResponse(404)),
        _FakeClient(_FakeResponse(500)),
        _FakeClient(_FakeResponse(200, raw="not json")),
        _FakeClient(error=RuntimeError("network down")),
    ],
)
async def test_fetch_segments_degrades_to_empty(monkeypatch, client):
    from nonebot_plugin_parser_lite.preprocess import sponsorblock

    monkeypatch.setattr(sponsorblock.httpx, "AsyncClient", lambda **kwargs: client)
    assert await sponsorblock.fetch_segments(
        "https://sb.example.top", "BV1xx", "987", ["sponsor"]
    ) == ([], {})


@pytest.mark.asyncio
async def test_fetch_segments_without_categories_is_a_noop(monkeypatch):
    from nonebot_plugin_parser_lite.preprocess import sponsorblock

    def forbidden(**kwargs):
        raise AssertionError("未配置分类时不应建立连接")

    monkeypatch.setattr(sponsorblock.httpx, "AsyncClient", forbidden)
    assert await sponsorblock.fetch_segments("https://x", "BV1", "1", []) == ([], {})
