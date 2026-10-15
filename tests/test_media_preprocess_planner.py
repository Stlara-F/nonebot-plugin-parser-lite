from __future__ import annotations

import json

import pytest
import render_test_support as support
from render_test_support import ResolvedPathTask

from nonebot_plugin_parser_lite.config import (
    SPONSORBLOCK_CATEGORIES,
    Config,
    parse_media_speedup_rules,
)
from nonebot_plugin_parser_lite.constants import PlatformEnum
from nonebot_plugin_parser_lite.data import (
    AudioContent,
    Author,
    ParseResult,
    Platform,
    VideoContent,
)
from nonebot_plugin_parser_lite.utils import media_edit, sponsorblock
from nonebot_plugin_parser_lite.utils.sponsorblock import SponsorSegment


def make_video(duration: float = 600.0, origin_ids: dict | None = None):
    cont = VideoContent(
        path_task=ResolvedPathTask(support.SyncPath("video.mp4")),  # type: ignore[arg-type]
        duration=duration,
    )
    cont.origin_ids = (
        {"bvid": "BV1", "cid": "9"} if origin_ids is None else origin_ids
    )
    return cont


def make_result(author_id="123456", contents=None, platform=PlatformEnum.BILIBILI):
    return ParseResult(
        platform=Platform(platform, platform.value),
        author=Author("tester", id=author_id),
        url="https://example.com/video",
        content=list(contents or [make_video()]),  # type: ignore[arg-type]
    )


def segment(start, end, category="sponsor", action="skip", recorded=600.0):
    return SponsorSegment(start, end, category, action, recorded)


def enable_sponsorblock(monkeypatch, categories=("sponsor",)):
    monkeypatch.setattr(
        support.pconfig, "plite_sponsorblock_api_base", "https://sb.example.top"
    )
    monkeypatch.setattr(support.pconfig, "plite_sponsorblock_categories", list(categories))


def feed(monkeypatch, rows):
    async def fake_lookup(*args, **kwargs):
        return rows

    monkeypatch.setattr(media_edit, "lookup_segments", fake_lookup)


# --------------------------------------------------------------------------
# 配置


def test_rule_grammar_is_platform_colon_id_equals_ratio():
    rules = parse_media_speedup_rules(
        ["bilibili:123456=1.25", " douyin:abc = 2 ", "bilibili:777=0.5"]
    )
    assert rules == {
        (PlatformEnum.BILIBILI, "123456"): 1.25,
        (PlatformEnum.DOUYIN, "abc"): 2.0,
        (PlatformEnum.BILIBILI, "777"): 0.5,
    }


@pytest.mark.parametrize(
    "raw",
    [
        "bilibili",
        "bilibili=1.25",
        "bilibili:123456",
        "bilibili:=1.25",
        "nosuchplatform:1=1.25",
        "bilibili:1=x",
    ],
)
def test_malformed_rule_is_rejected(raw):
    with pytest.raises(ValueError, match="变速规则"):
        parse_media_speedup_rules([raw])


def test_blank_entries_are_ignored():
    assert parse_media_speedup_rules(["", "  "]) == {}


def test_later_rule_wins_without_complaining():
    rules = parse_media_speedup_rules(["bilibili:1=1.2", "bilibili:1=1.5"])
    assert rules[(PlatformEnum.BILIBILI, "1")] == 1.5


def test_ratio_range_is_not_validated():
    assert parse_media_speedup_rules(["bilibili:1=100"]) == {(PlatformEnum.BILIBILI, "1"): 100.0}


def test_categories_whitelist_equals_the_service_ids():
    """白名单必须等于服务端接受的分类

    传未知分类时服务用 400 拒绝整个请求（实测返回 ``["Unknown category provided."]``），
    留一个服务端不认的写法就等于让整条裁切功能静默失效。
    """
    assert SPONSORBLOCK_CATEGORIES == {
        "sponsor",
        "selfpromo",
        "exclusive_access",
        "interaction",
        "poi_highlight",
        "intro",
        "outro",
        "preview",
        "padding",
        "filler",
        "music_offtopic",
    }


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"plite_sponsorblock_api_base": "sb.example.top"}, "以 http 开头"),
        ({"plite_sponsorblock_api_base": "https://x.top", "plite_sponsorblock_categories": ["bogus"]}, "未知的片段分类"),
        ({"plite_sponsorblock_categories": ["bogus"]}, "未知的片段分类"),
        ({"plite_media_speedup_rules": ["bilibili=1.2"]}, "平台:作者id=倍速"),
    ],
)
def test_config_rejects_bad_values(kwargs, reason):
    with pytest.raises(ValueError, match=reason):
        Config(**kwargs)


def test_config_normalizes_and_exposes_rules():
    config = Config(
        plite_sponsorblock_api_base="https://sb.example.top",
        plite_sponsorblock_categories=["Sponsor", " selfpromo "],
        plite_media_speedup_rules=["bilibili:123456=1.25"],
    )
    assert config.sponsorblock_api_base == "https://sb.example.top"
    assert config.sponsorblock_categories == ["sponsor", "selfpromo"]
    assert config.media_speedup_rules == {(PlatformEnum.BILIBILI, "123456"): 1.25}


# --------------------------------------------------------------------------
# 计划


def test_normalize_ranges_clamps_sorts_and_merges():
    merged = media_edit._normalize_ranges(
        [(500.0, 520.0), (100.0, 160.0), (150.0, 180.0), (-10.0, 20.0), (590.0, 900.0)],
        600.0,
    )
    assert merged == ((0.0, 20.0), (100.0, 180.0), (500.0, 520.0), (590.0, 600.0))


@pytest.mark.parametrize(
    ("cuts", "expected"),
    [
        ([(10.0, 20.0), (20.02, 30.0)], ((10.0, 30.0),)),
        ([(0.03, 30.0)], ((0.0, 30.0),)),
        ([(0.06, 30.0)], ((0.06, 30.0),)),
    ],
)
def test_tiny_retained_pieces_are_absorbed(cuts, expected):
    """转码层会丢掉太短的碎片，计划里就得先并掉，时长才对得上"""
    from nonebot_plugin_parser_lite.constants import MIN_KEEP_PIECE_SECONDS
    from nonebot_plugin_parser_lite.utils.ffmpeg import FFmpeg

    merged = media_edit._normalize_ranges(cuts, 600.0, MIN_KEEP_PIECE_SECONDS)
    assert merged == expected
    # 碎片被转码层丢掉、计划里却还留着的话，卡片时长会比真正发出的文件长
    kept = FFmpeg._build_keep_pieces(merged, 600.0)
    assert sum(end - start for start, end in kept) == 600.0 - sum(
        end - start for start, end in merged
    )


@pytest.mark.asyncio
async def test_planner_absorbs_tiny_pieces_from_the_service(monkeypatch):
    """计划必须由 _video_ranges 带上碎片阈值，而不是只在纯函数里正确"""
    monkeypatch.setattr(support.pconfig, "plite_media_speedup_rules", [])
    enable_sponsorblock(monkeypatch)
    feed(
        monkeypatch,
        [segment(10.0, 20.0, recorded=None), segment(20.02, 30.0, recorded=None)],
    )
    result = make_result()
    await media_edit.plan_media_edits(result)
    assert result.content[0].edit_plan.cut_ranges == ((10.0, 30.0),)


@pytest.mark.asyncio
async def test_planner_does_nothing_when_unconfigured(monkeypatch):
    monkeypatch.setattr(support.pconfig, "plite_media_speedup_rules", [])
    monkeypatch.setattr(support.pconfig, "plite_sponsorblock_api_base", None)

    async def boom(*args, **kwargs):
        raise AssertionError("不该发起片段查询")

    monkeypatch.setattr(media_edit, "lookup_segments", boom)
    result = make_result()
    await media_edit.plan_media_edits(result)
    assert result.content[0].edit_plan is None


@pytest.mark.asyncio
async def test_replanning_clears_a_stale_plan(monkeypatch):
    monkeypatch.setattr(support.pconfig, "plite_media_speedup_rules", ["bilibili:123456=1.25"])
    result = make_result()
    await media_edit.plan_media_edits(result)
    assert result.content[0].edit_plan is not None

    monkeypatch.setattr(support.pconfig, "plite_media_speedup_rules", [])
    await media_edit.plan_media_edits(result)
    assert result.content[0].edit_plan is None


@pytest.mark.asyncio
async def test_ratio_matches_platform_and_author(monkeypatch):
    monkeypatch.setattr(support.pconfig, "plite_media_speedup_rules", ["bilibili:999=1.25"])
    result = make_result()
    await media_edit.plan_media_edits(result)
    assert result.content[0].edit_plan is None

    monkeypatch.setattr(support.pconfig, "plite_media_speedup_rules", ["douyin:123456=1.25"])
    await media_edit.plan_media_edits(result)
    assert result.content[0].edit_plan is None

    monkeypatch.setattr(support.pconfig, "plite_media_speedup_rules", ["bilibili:123456=1.25"])
    await media_edit.plan_media_edits(result)
    assert result.content[0].edit_plan.speed_ratio == 1.25


@pytest.mark.asyncio
async def test_missing_author_id_never_matches(monkeypatch):
    monkeypatch.setattr(support.pconfig, "plite_media_speedup_rules", ["bilibili:123456=1.25"])
    result = make_result(author_id=None)
    await media_edit.plan_media_edits(result)
    assert result.content[0].edit_plan is None


@pytest.mark.asyncio
async def test_repost_matches_the_repost_author(monkeypatch):
    monkeypatch.setattr(support.pconfig, "plite_media_speedup_rules", ["bilibili:777=1.5"])
    inner = make_video(origin_ids={"bvid": "BV2", "cid": "1"})
    repost = ParseResult(
        platform=Platform(PlatformEnum.BILIBILI, "bilibili"),
        author=Author("reposted", id="777"),
        url="https://example.com/inner",
        content=[inner],
    )
    result = make_result()
    result.repost = repost
    await media_edit.plan_media_edits(result)
    assert result.content[0].edit_plan is None
    assert repost.content[0].edit_plan.speed_ratio == 1.5


@pytest.mark.asyncio
async def test_ranges_are_split_by_action_type(monkeypatch):
    monkeypatch.setattr(support.pconfig, "plite_media_speedup_rules", [])
    enable_sponsorblock(monkeypatch, ["sponsor", "intro"])
    feed(
        monkeypatch,
        [
            segment(0.0, 30.0, "sponsor", "skip"),
            segment(60.0, 75.0, "intro", "mute"),
            segment(90.0, 100.0, "sponsor", "full"),
            segment(200.0, 260.0, "outro", "skip"),
        ],
    )
    result = make_result()
    await media_edit.plan_media_edits(result)
    plan = result.content[0].edit_plan
    assert plan is not None
    assert plan.cut_ranges == ((0.0, 30.0),)
    assert plan.mute_ranges == ((60.0, 75.0),)


@pytest.mark.asyncio
async def test_staleness_is_checked_per_segment(monkeypatch):
    """同一分类里混着不同版本的提交时，只能逐段判断"""
    monkeypatch.setattr(support.pconfig, "plite_media_speedup_rules", [])
    enable_sponsorblock(monkeypatch)
    feed(
        monkeypatch,
        [segment(10.0, 20.0, recorded=300.0), segment(30.0, 40.0, recorded=600.0)],
    )
    result = make_result()
    await media_edit.plan_media_edits(result)
    assert result.content[0].edit_plan.cut_ranges == ((30.0, 40.0),)


@pytest.mark.asyncio
async def test_unknown_recorded_duration_is_not_falsified(monkeypatch):
    monkeypatch.setattr(support.pconfig, "plite_media_speedup_rules", [])
    enable_sponsorblock(monkeypatch)
    feed(monkeypatch, [segment(10.0, 20.0, recorded=None)])
    result = make_result()
    await media_edit.plan_media_edits(result)
    assert result.content[0].edit_plan.cut_ranges == ((10.0, 20.0),)


@pytest.mark.asyncio
async def test_cuts_covering_the_whole_video_are_abandoned(monkeypatch):
    monkeypatch.setattr(support.pconfig, "plite_media_speedup_rules", [])
    enable_sponsorblock(monkeypatch)
    feed(monkeypatch, [segment(0.0, 600.0)])
    result = make_result()
    await media_edit.plan_media_edits(result)
    assert result.content[0].edit_plan is None


@pytest.mark.asyncio
async def test_speed_and_ranges_compose(monkeypatch):
    monkeypatch.setattr(support.pconfig, "plite_media_speedup_rules", ["bilibili:123456=2.0"])
    enable_sponsorblock(monkeypatch)
    feed(monkeypatch, [segment(0.0, 60.0)])
    result = make_result()
    await media_edit.plan_media_edits(result)
    assert result.content[0].display_duration == "4:30"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("platform", "origin_ids", "duration"),
    [
        (PlatformEnum.BILIBILI, {}, 600.0),
        (PlatformEnum.BILIBILI, {"bvid": "BV1", "cid": "2"}, 0.0),
        (PlatformEnum.DOUYIN, {"bvid": "BV1", "cid": "2"}, 600.0),
    ],
)
async def test_lookup_needs_identity_and_duration(monkeypatch, platform, origin_ids, duration):
    monkeypatch.setattr(support.pconfig, "plite_media_speedup_rules", [])
    enable_sponsorblock(monkeypatch)

    async def boom(*args, **kwargs):
        raise AssertionError("不该发起查询")

    monkeypatch.setattr(media_edit, "lookup_segments", boom)
    video = make_video(duration=duration, origin_ids=origin_ids)
    await media_edit.plan_media_edits(make_result(platform=platform, contents=[video]))
    assert video.edit_plan is None


@pytest.mark.asyncio
async def test_audio_gets_speed_only(monkeypatch):
    monkeypatch.setattr(support.pconfig, "plite_media_speedup_rules", ["bilibili:123456=1.5"])
    enable_sponsorblock(monkeypatch)
    feed(monkeypatch, [segment(0.0, 30.0)])

    audio = AudioContent(
        path_task=ResolvedPathTask(support.SyncPath("audio.mp3")),  # type: ignore[arg-type]
        duration=600.0,
    )
    await media_edit.plan_media_edits(make_result(contents=[audio]))
    plan = audio.edit_plan
    assert plan is not None
    assert (plan.speed_ratio, plan.cut_ranges, plan.mute_ranges) == (1.5, (), ())


# --------------------------------------------------------------------------
# 片段查询


def test_parser_accepts_both_documented_shapes():
    flat = [{"segment": [1.0, 5.0], "category": "Sponsor", "actionType": "Skip"}]
    grouped = [{"videoID": "BV1", "segments": [{"segment": [2, 9], "category": "intro", "actionType": "mute"}]}]
    assert len(sponsorblock._iter_segment_dicts(flat)) == 1
    parsed = sponsorblock._parse_segment(sponsorblock._iter_segment_dicts(grouped)[0])
    assert parsed is not None
    assert (parsed.start, parsed.end, parsed.action_type) == (2.0, 9.0, "mute")


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
def test_malformed_segment_is_rejected(entry):
    assert sponsorblock._parse_segment(entry) is None


def test_zero_recorded_duration_means_unknown():
    """上游约定 videoDuration 为 0 表示提交时不知道时长，不能当成 0 秒"""
    parsed = sponsorblock._parse_segment(
        {"segment": [1.0, 5.0], "category": "sponsor", "actionType": "skip", "videoDuration": 0}
    )
    assert parsed is not None
    assert parsed.recorded_duration is None


class _Response:
    def __init__(self, status_code, payload=None, raw="not json"):
        self.status_code = status_code
        self._payload = payload
        self._raw = raw

    def json(self):
        if self._payload is not None:
            return self._payload
        raise json.JSONDecodeError("boom", self._raw, 0)


class _Client:
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
async def test_fetch_builds_the_query_and_returns_rows(monkeypatch):
    client = _Client(
        _Response(200, [{"segment": [1.0, 4.0], "category": "sponsor", "actionType": "skip", "videoDuration": 600}])
    )
    monkeypatch.setattr(sponsorblock.httpx, "AsyncClient", client)
    rows = await sponsorblock.fetch_segments("https://sb.example.top/", "BV1xx", "987", ["sponsor"])
    url, params = client.requests[0]
    assert url == "https://sb.example.top/api/skipSegments"
    assert params == {"videoID": "BV1xx", "cid": "987", "categories": '["sponsor"]'}
    assert [(r.start, r.end, r.recorded_duration) for r in rows] == [(1.0, 4.0, 600.0)]


@pytest.mark.asyncio
@pytest.mark.parametrize("client", [_Client(_Response(404)), _Client(_Response(400)), _Client(_Response(200)), _Client(error=RuntimeError("down"))])
async def test_fetch_degrades_to_empty(monkeypatch, client):
    monkeypatch.setattr(sponsorblock.httpx, "AsyncClient", lambda **kwargs: client)
    assert await sponsorblock.fetch_segments("https://x.top", "BV1xx", "9", ["sponsor"]) == []


@pytest.mark.asyncio
async def test_fetch_without_categories_makes_no_request(monkeypatch):
    def forbidden(**kwargs):
        raise AssertionError("不该建立连接")

    monkeypatch.setattr(sponsorblock.httpx, "AsyncClient", forbidden)
    assert await sponsorblock.fetch_segments("https://x.top", "BV1", "1", []) == []


@pytest.mark.asyncio
async def test_lookup_hits_the_service_only_once(monkeypatch):
    sponsorblock.clear_cache()
    calls: list[int] = []

    class Counting(_Client):
        async def get(self, url, params=None):
            calls.append(1)
            return await super().get(url, params)

    client = Counting(
        _Response(200, [{"segment": [1.0, 4.0], "category": "sponsor", "actionType": "skip"}])
    )
    monkeypatch.setattr(sponsorblock.httpx, "AsyncClient", lambda **kwargs: client)

    first = await sponsorblock.lookup_segments("https://x.top", "BV1", "9", ["sponsor"])
    second = await sponsorblock.lookup_segments("https://x.top", "BV1", "9", ["sponsor"])
    assert len(calls) == 1
    assert first is second

    sponsorblock.clear_cache()
    await sponsorblock.lookup_segments("https://x.top", "BV1", "9", ["sponsor"])
    assert len(calls) == 2
