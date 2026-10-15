from dataclasses import dataclass
from typing import Literal

from anyio import Path
from nonebot import get_driver, get_plugin_config
from pydantic import BaseModel, field_validator, model_validator

from .constants import PlatformEnum
from .path import cache_dir as _cache_dir
from .path import config_dir as _config_dir
from .path import data_dir as _data_dir
from .utils.bilibili.video import BiliAudioQuality, BiliVideoCodecs, BiliVideoQuality


def parse_hm_to_minutes(value: str) -> int:
    """将 h:m 或 hh:mm 解析为从 0 点起的分钟数"""
    text = value.strip()
    parts = text.split(":")
    if len(parts) != 2:
        raise ValueError(f"时间格式错误，应为 h:m，收到: {value!r}")
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError as e:
        raise ValueError(f"时间格式错误，应为 h:m，收到: {value!r}") from e
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"时间超出有效范围 (0:00-23:59)，收到: {value!r}")
    return hour * 60 + minute


SPEEDUP_RATIO_MIN = 0.5
"""倍速下限，与 ffmpeg atempo 的最小可用值一致"""
SPEEDUP_RATIO_MAX = 4.0
"""倍速上限，避免一条笔误的规则把部署者拖进长时间重编码"""
_AUTHOR_ID_ALIASES = frozenset({"author", "mid", "uid", "id", "user_id", "userid"})
"""作者维度规则里表示「作者 id」的等价写法"""
SPONSORBLOCK_CATEGORIES = frozenset(
    {
        "sponsor",
        "intro",
        "outro",
        "interaction",
        "selfpromotion",
        "musiconly",
        "preview",
        "poihighlight",
        "filler",
        "exclusiveaccess",
        "chapter",
    }
)
"""SponsorBlock 已知的片段分类；是否真的裁掉还取决于服务返回的 actionType"""


@dataclass(frozen=True, slots=True)
class MediaSpeedupRule:
    """解析后的单条上传前变速规则"""

    platform: PlatformEnum
    """平台"""
    author_id: str
    """作者 id，匹配 ParseResult.author.id"""
    ratio: float
    """倍速"""

    @property
    def key(self) -> tuple[PlatformEnum, str]:
        return (self.platform, self.author_id)


def parse_media_speedup_rule(raw: str) -> MediaSpeedupRule:
    """把 `bilibili:mid:123456=1.25` 解析为规则对象

    中段是作者 id 的等价写法（`author` / `mid` / `uid` / `id` / `user_id` /
    `userid`），插件统一按 `ParseResult.author.id` 匹配，不区分平台的命名习惯。
    """
    text = raw.strip()
    if "=" not in text:
        raise ValueError(f"变速规则缺少 '='，应为 平台:作者标识:id=倍速，收到: {raw!r}")
    left, _, ratio_text = text.partition("=")
    parts = left.split(":")
    if len(parts) != 3:
        raise ValueError(
            f"变速规则应为 平台:作者标识:id=倍速 三段，收到: {raw!r}"
        )
    platform_name, id_kind, author_id = (part.strip() for part in parts)
    if id_kind not in _AUTHOR_ID_ALIASES:
        raise ValueError(
            f"未知的作者标识写法 {id_kind!r}，可用: "
            f"{', '.join(sorted(_AUTHOR_ID_ALIASES))}；收到: {raw!r}"
        )
    if not author_id:
        raise ValueError(f"变速规则的作者 id 为空，收到: {raw!r}")
    try:
        platform = PlatformEnum(platform_name)
    except ValueError:
        raise ValueError(f"变速规则里的平台不存在: {platform_name!r}") from None
    try:
        ratio = float(ratio_text.strip())
    except ValueError:
        raise ValueError(f"变速规则的倍速不是数字，收到: {raw!r}") from None
    if not (SPEEDUP_RATIO_MIN <= ratio <= SPEEDUP_RATIO_MAX):
        raise ValueError(
            f"倍速需在 {SPEEDUP_RATIO_MIN}~{SPEEDUP_RATIO_MAX} 之间，收到: {raw!r}"
        )
    return MediaSpeedupRule(platform=platform, author_id=author_id, ratio=ratio)


def parse_media_speedup_rules(
    value: list[str],
) -> tuple[MediaSpeedupRule, ...]:
    """解析并校验整张规则表，同一平台+作者只允许一条规则"""
    parsed: dict[tuple[PlatformEnum, str], MediaSpeedupRule] = {}
    for raw in value:
        rule = parse_media_speedup_rule(raw)
        previous = parsed.get(rule.key)
        if previous is not None and previous.ratio != rule.ratio:
            raise ValueError(
                f"同一作者配置了互相矛盾的倍速: "
                f"{rule.platform} / {rule.author_id} = "
                f"{previous.ratio} 与 {rule.ratio}"
            )
        parsed[rule.key] = rule
    return tuple(parsed.values())


_SPEEDUP_RULES_MEMO: dict[tuple[str, ...], tuple[MediaSpeedupRule, ...]] = {}


def _speedup_rules_for(
    raw: tuple[str, ...],
) -> tuple[MediaSpeedupRule, ...]:
    """按原始配置串缓存解析结果；字段校验已保证这里不会抛错"""
    cached = _SPEEDUP_RULES_MEMO.get(raw)
    if cached is None:
        if len(_SPEEDUP_RULES_MEMO) > 32:
            _SPEEDUP_RULES_MEMO.clear()
        cached = parse_media_speedup_rules(list(raw))
        _SPEEDUP_RULES_MEMO[raw] = cached
    return cached


class Config(BaseModel):
    plite_zhihu_ck: str | None = None
    """知乎 cookies"""
    plite_linuxdo_ck: str | None = None
    """linuxdo cookies"""
    plite_x_ck: str | None = None
    """X cookies"""
    plite_need_upload: bool = False
    """是否需要上传音视频文件（兼容旧配置）"""
    plite_need_upload_audio: bool = False
    """是否需要上传音频文件"""
    plite_need_upload_video: bool = False
    """是否需要上传视频文件"""
    plite_file_upload_timeout: float | None = None
    """文件发送的超时秒数，由适配器支持；未设置时沿用默认等待时间"""
    plite_use_base64: bool = False
    """是否使用 base64 编码发送图片，音频，视频"""
    plite_max_size: int = 90
    """资源最大大小，默认 90 单位 MB"""
    plite_append_url: bool = False
    """是否在解析结果中添加原始URL"""
    plite_embed_url: bool = False
    """是否在解析结果中添加嵌入式播放链接"""
    plite_append_qrcode: bool = False
    """是否在解析结果中添加原始URL二维码"""
    plite_disabled_platforms: list[PlatformEnum] = []
    """禁用的解析器"""
    plite_blacklist_users: list[str] = []
    """黑名单用户列表，这些用户触发的解析将被忽略"""
    plite_bili_video_codes: list[BiliVideoCodecs] = [
        BiliVideoCodecs.AVC,
        BiliVideoCodecs.AV1,
        BiliVideoCodecs.HEV,
        BiliVideoCodecs.UNKNOWN,
    ]
    """B站视频编码"""
    plite_bili_video_quality: BiliVideoQuality = BiliVideoQuality._1080P
    """B站视频清晰度"""
    plite_bili_audio_quality: BiliAudioQuality = BiliAudioQuality._192K
    """B站音频流音质上限"""
    plite_need_forward_contents: bool = True
    """是否需要合并转发内容(大于四项时始终转发)"""
    plite_summary_in_forward: bool = False
    """是否将总结卡片放入内容合并转发"""
    plite_video_in_forward: bool = False
    """是否将视频放入内容合并转发"""
    plite_lazy_download: bool = False
    """是否开启懒下载模式，仅在用户请求时才下载视频"""
    plite_lazy_download_tip: bool = False
    """懒下载是否发送命令提示"""
    plite_lazy_download_timeout: int = 30
    """懒下载模式等待命令超时时间"""
    plite_download_command: list[str] = ["xz", "下载"]
    """在懒下载模式中用户请求下载视频时的命令列表"""
    plite_live_photo: bool = True
    """是否使用 ffmpeg 转码 Live Photo"""
    plite_max_comments: int = 5
    """最大评论数量"""
    plite_forward_text_threshold: int = 1000
    """纯文本文本长度阈值，超过此长度的文本将会强制转发(最大4500)"""
    plite_max_retries: int = 3
    """最大下载重试次数"""
    plite_day_range: list[str] = ["6:00", "19:00"]
    """白天时间范围 [开始, 结束]，格式 h:m；范围内为浅色主题，范围外为夜间模式"""
    plite_render_theme: str = "default"
    """渲染主题 ID，主题目录可放在插件数据目录的 themes 下"""
    plite_theme_dirs: list[str] = []
    """额外的主题目录，可填写主题目录或包含多个主题的目录"""
    plite_bili_cdn_region: str = "zh"
    """哔哩哔哩 CDN 地区；支持 zh、en、ja、proxy 线路"""
    plite_bili_cdn_domain: str | None = None
    """自定义哔哩哔哩 CDN 域名，优先于地区配置"""
    plite_render_format: Literal["jpeg", "webp"] = "webp"
    """渲染图片格式，jpeg会丢失透明度但可能兼容性更好"""
    plite_media_speedup_rules: list[str] = []
    """上传前按「平台+作者」对视频做倍速，格式 平台:作者标识:id=倍速（0.5~4.0），
    如 ["bilibili:mid:123456=1.25"]；作者标识 author/mid/uid/id/user_id 等价，
    统一匹配解析结果的作者 id。命中即重新编码，产物为有损，需 ffmpeg"""
    plite_media_transcode_concurrency: int = 1
    """上传前预处理允许的同时转码数，与卡片渲染的并发限制互相独立；修改后需重启生效"""
    plite_sponsorblock_api_base: str | None = None
    """SponsorBlock 兼容服务的基地址，留空表示不启用、不外发任何请求；
    插件不替你决定要信任哪个第三方实例，请填写自建或你认可的服务地址"""
    plite_sponsorblock_categories: list[str] = []
    """参与裁切的片段分类白名单，配置了服务地址时必填；
    可选 sponsor/intro/outro/interaction/selfpromotion/musiconly/preview/
    poihighlight/filler/exclusiveaccess/chapter，
    实际是否裁掉还取决于服务返回的 actionType（skip 删除区间，mute 只消音）"""

    @field_validator("plite_download_command")
    @classmethod
    def validate_download_commands(cls, value: list[str]) -> list[str]:
        commands = list(dict.fromkeys(command.strip() for command in value))
        if not all(commands):
            raise ValueError("懒下载命令不能为空字符串")
        return commands

    @field_validator("plite_day_range")
    @classmethod
    def validate_day_range(cls, value: list[str]) -> list[str]:
        if len(value) != 2:
            raise ValueError("白天时间范围必须包含开始和结束两个时间")
        for item in value:
            parse_hm_to_minutes(item)
        return value

    @field_validator("plite_media_speedup_rules")
    @classmethod
    def validate_media_speedup_rules(cls, value: list[str]) -> list[str]:
        rules = list(dict.fromkeys(item.strip() for item in value if item.strip()))
        parse_media_speedup_rules(rules)
        return rules

    @field_validator("plite_sponsorblock_categories")
    @classmethod
    def validate_sponsorblock_categories(cls, value: list[str]) -> list[str]:
        categories = list(dict.fromkeys(item.strip().casefold() for item in value))
        if not all(categories):
            raise ValueError("片段分类不能为空字符串")
        unknown = [item for item in categories if item not in SPONSORBLOCK_CATEGORIES]
        if unknown:
            raise ValueError(
                f"未知的片段分类 {unknown}，可用: "
                f"{', '.join(sorted(SPONSORBLOCK_CATEGORIES))}"
            )
        return categories

    @model_validator(mode="after")
    def validate_lazy_download(self) -> "Config":
        if self.plite_lazy_download and not self.plite_download_command:
            raise ValueError("开启懒下载时至少需要配置一条下载命令")
        return self

    @model_validator(mode="after")
    def validate_media_preprocess(self) -> "Config":
        if self.plite_media_transcode_concurrency < 1:
            raise ValueError("转码并发数至少为 1")
        if self.plite_sponsorblock_api_base is not None:
            base = self.plite_sponsorblock_api_base.strip()
            if not base:
                raise ValueError("SponsorBlock 服务地址不能是空字符串，留空表示不启用")
            if not base.startswith(("http://", "https://")):
                raise ValueError("SponsorBlock 服务地址必须以 http:// 或 https:// 开头")
            if not self.plite_sponsorblock_categories:
                raise ValueError(
                    "配置了 SponsorBlock 服务地址时必须同时指定至少一个片段分类"
                )
        elif self.plite_sponsorblock_categories:
            raise ValueError("指定片段分类时必须同时配置 SponsorBlock 服务地址")
        return self

    @property
    def nickname(self) -> str:
        """机器人昵称"""
        return _nickname

    @property
    def cache_dir(self) -> Path:
        """插件缓存目录"""
        return _cache_dir

    @property
    def config_dir(self) -> Path:
        """插件配置目录"""
        return _config_dir

    @property
    def data_dir(self) -> Path:
        """插件数据目录"""
        return _data_dir

    @property
    def max_size(self) -> int:
        """资源最大大小(mb)"""
        return self.plite_max_size

    @property
    def file_upload_timeout(self) -> float | None:
        return self.plite_file_upload_timeout

    @property
    def disabled_platforms(self) -> list[PlatformEnum]:
        """禁用的解析器"""
        return self.plite_disabled_platforms

    @property
    def bili_video_codes(self) -> list[BiliVideoCodecs]:
        """B站视频编码"""
        return self.plite_bili_video_codes

    @property
    def bili_video_quality(self) -> BiliVideoQuality:
        """B站视频清晰度"""
        return self.plite_bili_video_quality

    @property
    def bili_audio_quality(self) -> BiliAudioQuality:
        """B站音频流音质上限"""
        return self.plite_bili_audio_quality

    @property
    def zhihu_ck(self) -> str | None:
        """知乎 cookies"""
        return self.plite_zhihu_ck

    @property
    def linuxdo_ck(self) -> str | None:
        """linuxdo cookies"""
        return self.plite_linuxdo_ck

    @property
    def x_ck(self) -> str | None:
        """X cookies"""
        return self.plite_x_ck

    @property
    def need_upload_audio(self) -> bool:
        """是否需要上传音频文件"""
        return self.plite_need_upload_audio or self.plite_need_upload

    @property
    def need_upload_video(self) -> bool:
        """是否需要上传视频文件"""
        return self.plite_need_upload_video or self.plite_need_upload

    @property
    def use_base64(self) -> bool:
        """是否使用 base64 编码发送图片，音频，视频"""
        return self.plite_use_base64

    @property
    def append_url(self) -> bool:
        """是否在解析结果中添加原始URL"""
        return self.plite_append_url

    @property
    def embed_url(self) -> bool:
        """是否在解析结果中添加嵌入式播放链接"""
        return self.plite_embed_url

    @property
    def append_qrcode(self) -> bool:
        """是否在解析结果中添加原始URL二维码"""
        return self.plite_append_qrcode

    @property
    def need_forward_contents(self) -> bool:
        """是否需要合并转发"""
        return self.plite_need_forward_contents

    @property
    def summary_in_forward(self) -> bool:
        """是否将总结卡片放入内容合并转发"""
        return self.plite_summary_in_forward

    @property
    def video_in_forward(self) -> bool:
        """是否将视频放入内容合并转发"""
        return self.plite_video_in_forward

    @property
    def blacklist_users(self) -> list[str]:
        """黑名单用户列表"""
        return self.plite_blacklist_users

    @property
    def download_command(self) -> list[str]:
        """在懒下载模式中用户请求下载视频时的命令列表"""
        return self.plite_download_command

    @property
    def lazy_download(self) -> bool:
        """是否开启懒下载模式"""
        return self.plite_lazy_download

    @property
    def lazy_download_tip(self) -> bool:
        """懒下载是否发送命令提示"""
        return self.plite_lazy_download_tip

    @property
    def lazy_download_timeout(self) -> int:
        """懒下载模式等待命令超时时间"""
        return self.plite_lazy_download_timeout

    @property
    def live_photo(self) -> bool:
        """是否使用 iPhone Live Photo 功能"""
        return self.plite_live_photo

    @property
    def max_comments(self) -> int:
        """最大评论数量"""
        return self.plite_max_comments

    @property
    def forward_text_threshold(self) -> int:
        """纯文本文本长度阈值，超过此长度的文本将会强制转发"""
        return self.plite_forward_text_threshold

    @property
    def max_retries(self) -> int:
        """最大下载重试次数"""
        return self.plite_max_retries

    @property
    def day_range_minutes(self) -> tuple[int, int]:
        """白天时间范围，返回 (开始分钟, 结束分钟)"""
        return (
            parse_hm_to_minutes(self.plite_day_range[0]),
            parse_hm_to_minutes(self.plite_day_range[1]),
        )

    @property
    def render_theme(self) -> str:
        """渲染主题 ID"""
        return self.plite_render_theme

    @property
    def theme_dirs(self) -> list[str]:
        """额外的主题目录"""
        return self.plite_theme_dirs

    @property
    def bili_cdn_region(self) -> str:
        """哔哩哔哩 CDN 地区"""
        return self.plite_bili_cdn_region

    @property
    def bili_cdn_domain(self) -> str | None:
        """自定义哔哩哔哩 CDN 域名"""
        return self.plite_bili_cdn_domain

    @property
    def render_format(self) -> Literal["jpeg", "webp"]:
        """渲染图片格式"""
        return self.plite_render_format

    @property
    def media_speedup_rules(self) -> tuple[MediaSpeedupRule, ...]:
        """解析后的上传前倍速规则"""
        return _speedup_rules_for(tuple(self.plite_media_speedup_rules))

    @property
    def media_transcode_concurrency(self) -> int:
        """上传前预处理允许的并发数"""
        return self.plite_media_transcode_concurrency

    @property
    def sponsorblock_api_base(self) -> str | None:
        """SponsorBlock 兼容服务基地址，未配置时返回 None"""
        base = self.plite_sponsorblock_api_base
        if base is None:
            return None
        return base.strip().rstrip("/")

    @property
    def sponsorblock_categories(self) -> list[str]:
        """参与裁切的片段分类"""
        return self.plite_sponsorblock_categories


# 初始化配置实例
_driver = get_driver()
pconfig: Config = get_plugin_config(Config)
"""插件配置"""
gconfig = _driver.config
"""全局配置"""
_nickname: str = next(iter(gconfig.nickname), "nonebot-plugin-parser")
"""机器人昵称"""
