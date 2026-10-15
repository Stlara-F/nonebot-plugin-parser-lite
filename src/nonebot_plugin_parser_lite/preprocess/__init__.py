"""上传前媒体预处理：按配置对即将发送的视频做倍速 / 片段裁切。

分两个阶段，中间只传一个 ``VideoEditPlan``：

- ``plan_media_edits`` 在渲染卡片之前定稿编辑计划，卡片的时长与体积因此与
  最终发送的文件同源，重复解析同一条缓存结果也不会把倍速叠加两次；
- ``apply_edit_plan`` 在构造消息段时生成产物，命中缓存直接复用，任何失败
  都退回原始文件，不会因此拒发消息。

启用前提与代价：

- 产物是有损重编码，离开插件后无法还原为原作品；播放端若本身支持倍速，
  服务端重编码的收益很薄。
- 依赖 ffmpeg，且是 CPU/IO 密集操作，并发由
  ``plite_media_transcode_concurrency`` 单独控制（与卡片渲染的并发闸互不相干）。
- 裁切依赖外部标注，``plite_sponsorblock_api_base`` 默认留空，即不启用也不外发
  任何请求；标注若与当前视频时长对不上会被丢弃，避免把正片当广告裁掉。
- 匹配依赖解析结果里的作者 id，B 站番剧、微博文章等不携带作者 id 的解析
  不会命中规则。

独立版（standalone 分支）不含发送链路，需要宿主自行调用：先
``await plan_media_edits(result)``，再在取媒体路径时
``path = await apply_edit_plan(cont, await cont.get_path())``。
"""

from .apply import apply_edit_plan
from .planner import plan_media_edits

__all__ = ["apply_edit_plan", "plan_media_edits"]
