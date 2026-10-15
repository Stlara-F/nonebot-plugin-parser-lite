"""上传前倍速：把配置里的「平台+作者」倍速写到即将发送的媒体上。

渲染层和 ``MediaContent.get_path`` 读同一个 ``speedup``，卡片显示的时长就是真正
发出去的文件时长。按平台标注裁切片段是平台特有的能力，写在对应解析器里。
"""

from itertools import chain

from ..config import pconfig
from ..data import MediaContent, ParseResult


def apply_media_speedup(result: ParseResult) -> None:
    """按平台与作者命中倍速规则；结果对象跨消息缓存，没命中时要显式清回 1.0"""
    rules = pconfig.media_speedup_rules
    ratio = rules.get((result.platform.name, result.author.id)) or 1.0
    repost_medias = result.repost.content if result.repost else []
    for cont in chain(result.content, repost_medias):
        if isinstance(cont, MediaContent) and cont.need_send:
            cont.speedup = ratio


def speedup_digest(result: ParseResult) -> str:
    """卡片缓存键里的倍速标记，把「同一个 URL 但倍速不同」的卡片分开缓存"""
    repost_medias = result.repost.content if result.repost else []
    return ",".join(
        f"{cont.speedup:.2f}x"
        for cont in chain(result.content, repost_medias)
        if isinstance(cont, MediaContent) and cont.need_send
    )
