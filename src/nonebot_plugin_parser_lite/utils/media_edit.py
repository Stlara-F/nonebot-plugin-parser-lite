"""上传前倍速：把配置里的「平台+作者」倍速写到即将发送的音视频上。

渲染层和 ``MediaContent.get_path`` 读同一个 ``speedup``，卡片显示的时长就是真正
发出去的文件时长。按平台标注裁切片段是平台特有的能力，写在对应解析器里。
"""

from ..config import pconfig
from ..data import AudioContent, MediaContent, ParseResult, VideoContent


def _editable(result: ParseResult) -> list[tuple[ParseResult, MediaContent]]:
    """结果里所有可能被倍速的音视频，转发内容算进来"""
    results = [result]
    if result.repost is not None:
        results.append(result.repost)
    return [
        (item, cont)
        for item in results
        for cont in item.content
        if isinstance(cont, VideoContent | AudioContent)
    ]


def apply_media_speedup(result: ParseResult) -> None:
    """按平台与作者命中倍速规则；结果对象跨消息缓存，没命中时要显式清回 1.0"""
    rules = pconfig.media_speedup_rules
    for item, cont in _editable(result):
        cont.speedup = rules.get((item.platform.name, item.author.id)) or 1.0


def speedup_digest(result: ParseResult) -> str:
    """卡片缓存键里的倍速标记，把「同一个 URL 但倍速不同」的卡片分开缓存"""
    return ",".join(
        f"{cont.speedup:.2f}x" for _, cont in _editable(result) if cont.speedup != 1.0
    )
