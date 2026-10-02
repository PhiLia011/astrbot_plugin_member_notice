"""
群成员入群/退群通知插件
- 入群时发送自定义欢迎语，退群时发送自定义退群语
- 入群时可按配置自动禁言新成员（可开关、可设时长、可按群覆盖）
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

# 入群自动禁言：默认时长与 OneBot 协议端上限（30 天）
DEFAULT_BAN_SECONDS = 600
MAX_BAN_SECONDS = 30 * 24 * 3600
DEFAULT_MUTE_NOTICE_MSG = "{nickname} 已加入本群，新人保护期已开启：禁言 {duration}，请先阅读群规～"


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
        sub_type = str(raw.get("sub_type") if isinstance(raw, dict) else getattr(raw, "sub_type", ""))
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
                await self._handle_increase(event, group_id, user_id, sub_type)
            else:
                await self._handle_decrease(event, group_id, user_id)
        except Exception as e:
            logger.error(f"[MemberNotice] 处理事件异常: {e}")

    # ------------------------------------------------------------------
    # 入群欢迎 + 自动禁言
    # ------------------------------------------------------------------
    async def _handle_increase(self, event: AstrMessageEvent, group_id: str, user_id: str, sub_type: str = ""):
        welcome_on = bool(self.config.get("welcome_enable", True))
        mute_on = bool(self.config.get("mute_enable", False))
        if not welcome_on and not mute_on:
            return

        nickname = await self._fetch_nickname(event, group_id, user_id) or user_id

        # 顺序：先禁言（尽快生效），再发欢迎语，最后发禁言提示
        ban_seconds = await self._mute_new_member(event, group_id, user_id, sub_type) if mute_on else 0

        if welcome_on:
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

        if ban_seconds:
            template = str(self.config.get("mute_notice_msg", DEFAULT_MUTE_NOTICE_MSG) or "").strip()
            if template:
                notice = _fmt(
                    template,
                    nickname=nickname, user_id=user_id, group_id=group_id,
                    duration=self._human_duration(ban_seconds),
                )
                await self._send(event, group_id, [Plain(text=notice)])
                logger.info(f"[MemberNotice] 禁言提示 -> 群 {group_id} / 成员 {user_id}")

    # ------------------------------------------------------------------
    # 自动禁言：返回实际禁言的秒数，跳过或失败返回 0
    # ------------------------------------------------------------------
    async def _mute_new_member(self, event: AstrMessageEvent, group_id: str, user_id: str,
                               sub_type: str = "") -> int:
        # 机器人自己被拉进群时不处理
        if str(user_id) == str(event.get_self_id() or ""):
            return 0
        # 群主/管理员邀请入群（sub_type=invite）
        if sub_type == "invite" and self.config.get("mute_skip_invite", True):
            logger.debug(f"[MemberNotice] 群 {group_id} 成员 {user_id} 由邀请入群，跳过禁言")
            return 0
        # 免禁言名单
        skip_users = [str(u).strip() for u in self.config.get("mute_skip_users", []) or []]
        if user_id in skip_users:
            logger.debug(f"[MemberNotice] 成员 {user_id} 在免禁言名单，跳过禁言")
            return 0

        # 时长：优先本群覆盖值，非法值回退全局默认值，再夹到 30 天以内
        seconds = 0
        for item in self.config.get("mute_per_group", []) or []:
            if not isinstance(item, dict) or str(item.get("group_id", "")).strip() != group_id:
                continue
            try:
                seconds = int(item.get("duration", 0))
            except (TypeError, ValueError):
                seconds = 0
            break
        if seconds < 1:
            try:
                seconds = int(self.config.get("mute_duration", DEFAULT_BAN_SECONDS))
            except (TypeError, ValueError):
                seconds = 0
        if seconds < 1:
            seconds = DEFAULT_BAN_SECONDS
        seconds = min(seconds, MAX_BAN_SECONDS)

        bot = getattr(event, "bot", None)
        if bot is None:
            logger.warning(f"[MemberNotice] 群 {group_id} 禁言 {user_id} 失败：未获取到 bot 实例")
            return 0
        try:
            result = await bot.api.call_action(
                "set_group_ban",
                group_id=int(group_id),
                user_id=int(user_id),
                duration=seconds,
            )
        except Exception as e:
            logger.warning(
                f"[MemberNotice] 群 {group_id} 禁言 {user_id} 失败：{e}"
                "（请确认机器人在该群是管理员或群主）"
            )
            return 0
        retcode = result.get("retcode") if isinstance(result, dict) else None
        if retcode not in (None, 0):
            logger.warning(f"[MemberNotice] 群 {group_id} 禁言 {user_id} 被协议端拒绝：{result}")
            return 0

        logger.info(f"[MemberNotice] 已自动禁言新成员 -> 群 {group_id} / 成员 {user_id} / {seconds} 秒")
        return seconds

    @staticmethod
    def _human_duration(seconds: int) -> str:
        """把秒数转成人类可读文本：600 -> 10 分钟"""
        days, rem = divmod(int(seconds), 86400)
        hours, rem = divmod(rem, 3600)
        minutes, secs = divmod(rem, 60)
        parts = []
        if days:
            parts.append(f"{days} 天")
        if hours:
            parts.append(f"{hours} 小时")
        if minutes:
            parts.append(f"{minutes} 分钟")
        if secs and not days:
            parts.append(f"{secs} 秒")
        return " ".join(parts) or f"{int(seconds)} 秒"

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
