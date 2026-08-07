"""
群成员入群/退群通知插件
- 入群时发送自定义欢迎语，退群时发送自定义退群语
- 支持黑白名单（黑名单优先；白名单留空则全部生效）
- 即使其他欢迎插件已发送过消息，本插件也会照常发送（主动发送 + 高优先级）
"""

import re
import time

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.event.filter import EventMessageType, event_message_type
from astrbot.api.message_components import At, Plain
from astrbot.api.star import Context, Star

# 用于防止短时间内重复处理同一事件（例如多端重复上报）
_last_event_ts: dict[tuple, float] = {}


def _fmt(template: str, **kwargs) -> str:
    """替换 {xxx} 占位符，未提供的字段原样保留"""

    def repl(m):
        key = m.group(1)
        v = kwargs.get(key)
        return str(v) if v is not None else m.group(0)

    return re.sub(r"\{(\w+)\}", repl, template)


class MemberNoticePlugin(Star):
    '''
    本插件：群成员入群/退群时发送欢迎/退群消息。
    支持自定义文案、黑白名单、@新成员，且不会被其他欢迎插件屏蔽。
    '''

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

    # ------------------------------------------------------------------
    # 事件入口：OneBot V11 通知事件会被 aiocqhttp 适配器转换为
    # GROUP_MESSAGE 类型的消息事件，raw_message 保留原始事件对象。
    # priority=999 保证最先执行，避免被其他插件的 stop_event 屏蔽。
    # ------------------------------------------------------------------
    @event_message_type(EventMessageType.GROUP_MESSAGE, priority=999)
    async def on_group_notice(self, event: AstrMessageEvent):
        if not self.config.get("enable", True):
            return

        raw = getattr(event.message_obj, "raw_message", None)
        if raw is None:
            return
        # OneBot V11 通知事件标记
        post_type = raw.get("post_type") if isinstance(raw, dict) else getattr(raw, "post_type", None)
        if post_type != "notice":
            return
        notice_type = raw.get("notice_type") if isinstance(raw, dict) else getattr(raw, "notice_type", None)
        if notice_type not in ("group_increase", "group_decrease"):
            return

        group_id = str(raw.get("group_id") if isinstance(raw, dict) else getattr(raw, "group_id", ""))
        user_id = str(raw.get("user_id") if isinstance(raw, dict) else getattr(raw, "user_id", ""))
        if not group_id or not user_id:
            return

        # 黑白名单判断：黑名单优先，其次白名单
        if not self._is_group_allowed(group_id):
            logger.debug(f"[MemberNotice] 群 {group_id} 不在生效范围，跳过")
            return

        # 去重：同一群同一用户同一事件类型，10 秒内只处理一次
        dedup_key = (group_id, user_id, notice_type)
        now = time.time()
        if _last_event_ts.get(dedup_key, 0) > now - 10:
            return
        _last_event_ts[dedup_key] = now

        try:
            if notice_type == "group_increase":
                await self._handle_increase(event, group_id, user_id)
            else:
                await self._handle_decrease(event, group_id, user_id)
        except Exception as e:
            logger.error(f"[MemberNotice] 处理事件异常: {e}")

    # ------------------------------------------------------------------
    # 入群欢迎
    # ------------------------------------------------------------------
    async def _handle_increase(self, event: AstrMessageEvent, group_id: str, user_id: str):
        if not self.config.get("welcome_enable", True):
            return
        nickname = await self._fetch_nickname(event, group_id, user_id) or user_id
        text = _fmt(
            str(self.config.get("welcome_msg", "欢迎 {nickname} 加入本群！")),
            nickname=nickname, user_id=user_id, group_id=group_id,
        )
        chain = []
        if self.config.get("at_new_member", True):
            chain.append(At(qq=user_id))
        chain.append(Plain(text=text))
        await self._send(event, group_id, chain)
        logger.info(f"[MemberNotice] 入群欢迎 -> 群 {group_id} / 成员 {user_id} ({nickname})")

    # ------------------------------------------------------------------
    # 退群提示
    # ------------------------------------------------------------------
    async def _handle_decrease(self, event: AstrMessageEvent, group_id: str, user_id: str):
        if not self.config.get("leave_enable", True):
            return
        # 成员已退群，通常无法拿到群名片，用陌生人接口尽力获取，失败则显示 QQ 号
        nickname = await self._fetch_stranger_name(event, user_id) or user_id
        text = _fmt(
            str(self.config.get("leave_msg", "{nickname} 离开了本群。")),
            nickname=nickname, user_id=user_id, group_id=group_id,
        )
        await self._send(event, group_id, [Plain(text=text)])
        logger.info(f"[MemberNotice] 退群提示 -> 群 {group_id} / 成员 {user_id}")

    # ------------------------------------------------------------------
    # 主动发送消息（不依赖事件结果链路，因此不会被其他插件屏蔽）
    # ------------------------------------------------------------------
    async def _send(self, event: AstrMessageEvent, group_id: str, chain: list):
        try:
            await self.context.send_message(
                event.unified_msg_origin,
                MessageChain(chain=chain),
            )
        except Exception as e:
            logger.error(f"[MemberNotice] 发送失败(群 {group_id}): {e}")

    # ------------------------------------------------------------------
    # 获取群成员昵称（入群时）
    # ------------------------------------------------------------------
    async def _fetch_nickname(self, event: AstrMessageEvent, group_id: str, user_id: str) -> str | None:
        bot = getattr(event, "bot", None)
        if bot is None:
            return None
        try:
            info = await bot.api.call_action(
                "get_group_member_info",
                group_id=int(group_id),
                user_id=int(user_id),
                no_cache=True,
            )
            return str(info.get("card") or info.get("nickname") or "").strip() or None
        except Exception:
            return None

    # ------------------------------------------------------------------
    # 获取陌生人资料昵称（退群时）
    # ------------------------------------------------------------------
    async def _fetch_stranger_name(self, event: AstrMessageEvent, user_id: str) -> str | None:
        bot = getattr(event, "bot", None)
        if bot is None:
            return None
        try:
            info = await bot.api.call_action("get_stranger_info", user_id=int(user_id), no_cache=True)
            return str(info.get("nickname") or "").strip() or None
        except Exception:
            return None

    # ------------------------------------------------------------------
    # 黑白名单：黑名单优先；白名单非空时仅白名单内群生效
    # ------------------------------------------------------------------
    def _is_group_allowed(self, group_id: str) -> bool:
        black = [str(g).strip() for g in self.config.get("blacklist_groups", [])]
        if group_id in black:
            return False
        white = [str(g).strip() for g in self.config.get("whitelist_groups", [])]
        if white and group_id not in white:
            return False
        return True
