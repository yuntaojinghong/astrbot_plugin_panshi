"""基础群管理：禁言 / 解禁 / 全体禁言 / 踢人 / 拉黑 / 撤回 / 改名 / 头衔 / 精华 等。"""

from __future__ import annotations

from ..utils import format_duration, get_ats, get_nickname
from ..utils.helpers import extract_image_url, get_reply_id
from .base_handle import BaseHandle


class NormalHandle(BaseHandle):
    """封装常规群管理操作。"""

    # ---------- 禁言 ----------
    async def set_ban(self, event, target_id: str | int | None, seconds: int) -> str:
        """禁言一个或多个用户。target_id 为空时对所有 @ 生效。"""
        group_id = self.group_id(event)
        seconds = self.cfg.clamp_ban_time(seconds)
        targets = [str(target_id)] if target_id else get_ats(event)
        if not targets:
            return "❓ 请 @某人、引用其消息或直接给出 QQ 号。"

        results = []
        for tid in targets:
            ok, err = await self.call_api(
                event,
                "set_group_ban",
                group_id=group_id,
                user_id=int(tid),
                duration=seconds,
            )
            name = await get_nickname(event, tid)
            if ok:
                if seconds == 0:
                    results.append(f"✅ 已解除 {name} 的禁言")
                else:
                    results.append(f"✅ 已禁言 {name} {format_duration(seconds)}")
            else:
                results.append(f"❌ 对 {name} 的操作失败：{err}")
        return "\n".join(results)

    async def cancel_ban(self, event, target_id: str | int | None) -> str:
        """解除禁言。"""
        return await self.set_ban(event, target_id, 0)

    # ---------- 全体禁言 ----------
    async def whole_ban(self, event, enable: bool) -> str:
        ok, err = await self.call_api(
            event,
            "set_group_whole_ban",
            group_id=self.group_id(event),
            enable=enable,
        )
        if not ok:
            return f"❌ 操作失败：{err}"
        return "🔇 已开启全体禁言" if enable else "🔊 已解除全体禁言"

    # ---------- 踢人 / 拉黑 ----------
    async def kick(self, event, target_id: str | int | None, reject: bool = False, reason: str = "") -> str:
        group_id = self.group_id(event)
        targets = [str(target_id)] if target_id else get_ats(event)
        if not targets:
            return "❓ 请 @某人、引用其消息或直接给出 QQ 号。"

        results = []
        for tid in targets:
            name = await get_nickname(event, tid)
            ok, err = await self.call_api(
                event,
                "set_group_kick",
                group_id=group_id,
                user_id=int(tid),
                reject_add_request=reject,
            )
            if ok:
                action = "踢出并拉黑" if reject else "踢出"
                tip = f"（原因：{reason}）" if reason else ""
                results.append(f"✅ 已将 {name} {action}{tip}")
                if reject:
                    self.db.add_blacklist(group_id, tid)
            else:
                results.append(f"❌ 操作 {name} 失败：{err}")
        return "\n".join(results)

    # ---------- 撤回 / 净化 ----------
    async def recall(self, event, count: int = 1, target_id: str | int | None = None) -> str:
        """撤回消息。

        - 有引用消息时，撤回被引用的一条。
        - 否则撤回最近 count 条（含目标用户过滤）。
        """
        group_id = self.group_id(event)

        # 优先撤回引用消息
        reply_id = get_reply_id(event)
        if reply_id:
            ok, err = await self.call_api(event, "delete_msg", message_id=int(reply_id))
            return "✅ 已撤回该消息" if ok else f"❌ 撤回失败：{err}"

        # 撤回最近 N 条（从历史缓存中取）
        msg_ids = self._recent_message_ids(event, count, target_id)
        if not msg_ids:
            return "❓ 没有可撤回的消息（可能未缓存到）。请引用要撤回的消息。"

        success = 0
        for mid in msg_ids:
            ok, _ = await self.call_api(event, "delete_msg", message_id=int(mid))
            if ok:
                success += 1
        return f"✅ 已撤回 {success}/{len(msg_ids)} 条消息"

    async def purge(self, event, count: int = 30) -> str:
        """批量撤回最近 count 条消息（清屏）。"""
        return await self.recall(event, count=count)

    def _recent_message_ids(self, event, count: int, target_id=None) -> list[str]:
        """从共享的消息缓存中取最近的消息 ID。

        缓存由 GuardHandle 维护，通过 main.py 注入到 self.db 之外的位置；
        这里优先读 event 上挂载的缓存，其次读 handle 注入的 provider。
        """
        cache = getattr(event, "_panshi_msg_cache", None)
        if cache is None:
            provider = getattr(self, "_cache_provider", None)
            if provider is not None:
                try:
                    cache = provider.get_recent(str(event.get_group_id()), limit=100)
                except Exception:
                    cache = None
        if not cache:
            return []
        ids = []
        for item in reversed(cache):
            if len(ids) >= count:
                break
            if target_id and str(item.get("user_id")) != str(target_id):
                continue
            if item.get("message_id"):
                ids.append(str(item["message_id"]))
        return ids

    def bind_cache(self, provider) -> None:
        """由 main.py 注入消息缓存提供者（GuardHandle）。"""
        self._cache_provider = provider

    # ---------- 群名片 / 头衔 ----------
    async def set_card(self, event, target_id: str | int | None, card: str) -> str:
        group_id = self.group_id(event)
        tids = [str(target_id)] if target_id else (get_ats(event) or [str(self.sender_id(event))])
        results = []
        for tid in tids:
            name = await get_nickname(event, tid)
            ok, err = await self.call_api(
                event,
                "set_group_card",
                group_id=group_id,
                user_id=int(tid),
                card=card or "",
            )
            if ok:
                results.append(
                    f"✅ 已将 {name} 的群昵称改为「{card}」" if card else f"✅ 已清除 {name} 的群昵称"
                )
            else:
                results.append(f"❌ 修改 {name} 昵称失败：{err}")
        return "\n".join(results)

    async def set_special_title(self, event, target_id: str | int | None, title: str) -> str:
        group_id = self.group_id(event)
        tids = [str(target_id)] if target_id else (get_ats(event) or [str(self.sender_id(event))])
        results = []
        for tid in tids:
            name = await get_nickname(event, tid)
            ok, err = await self.call_api(
                event,
                "set_group_special_title",
                group_id=group_id,
                user_id=int(tid),
                special_title=title or "",
                duration=-1,
            )
            if ok:
                results.append(
                    f"✅ 已设置 {name} 的头衔为「{title}」" if title else f"✅ 已清除 {name} 的头衔"
                )
            else:
                results.append(f"❌ 设置 {name} 头衔失败：{err}")
        return "\n".join(results)

    # ---------- 管理员任免 ----------
    async def set_admin(self, event, target_id: str | int | None, enable: bool) -> str:
        group_id = self.group_id(event)
        tids = [str(target_id)] if target_id else get_ats(event)
        if not tids:
            return "❓ 请 @要操作的人。"
        results = []
        for tid in tids:
            name = await get_nickname(event, tid)
            ok, err = await self.call_api(
                event,
                "set_group_admin",
                group_id=group_id,
                user_id=int(tid),
                enable=enable,
            )
            if ok:
                results.append(
                    f"✅ 已设置 {name} 为管理员" if enable else f"✅ 已取消 {name} 的管理员"
                )
            else:
                results.append(f"❌ 操作 {name} 失败：{err}")
        return "\n".join(results)

    # ---------- 精华 ----------
    async def set_essence(self, event, enable: bool) -> str:
        reply_id = get_reply_id(event)
        if not reply_id:
            return "❓ 请引用要设为精华的消息。"
        action = "set_essence_msg" if enable else "delete_essence_msg"
        ok, err = await self.call_api(event, action, message_id=int(reply_id))
        if ok:
            return "⭐ 已设为精华消息" if enable else "已取消精华消息"
        return f"❌ 操作失败：{err}"

    # ---------- 群名 / 群头像 / 公告 ----------
    async def set_group_name(self, event, name: str) -> str:
        if not name:
            return "❓ 请输入新的群名。"
        ok, err = await self.call_api(
            event, "set_group_name", group_id=self.group_id(event), group_name=name
        )
        return f"✅ 群名已更新为「{name}」" if ok else f"❌ 修改群名失败：{err}"

    async def set_group_portrait(self, event, image_url: str | None = None) -> str:
        url = image_url or extract_image_url(event)
        if not url:
            return "❓ 请引用一张图片作为新群头像。"
        ok, err = await self.call_api(
            event, "set_group_portrait", group_id=self.group_id(event), file=url
        )
        return "✅ 群头像已更新" if ok else f"❌ 修改群头像失败：{err}"

    async def send_notice(self, event, content: str, image_url: str | None = None) -> str:
        if not content:
            return "❓ 请输入公告内容。"
        params = {
            "group_id": self.group_id(event),
            "content": content,
        }
        if image_url:
            params["image"] = image_url
        ok, err = await self.call_api(event, "_send_group_notice", **params)
        if not ok:
            # 兼容不同接口名
            ok, err = await self.call_api(event, "send_group_notice", **params)
        return "📢 群公告已发布" if ok else f"❌ 发布公告失败：{err}"

    async def get_notice(self, event) -> str:
        ok, data = await self.call_api(event, "_get_group_notice", group_id=self.group_id(event))
        if not ok:
            ok, data = await self.call_api(event, "get_group_notice", group_id=self.group_id(event))
        if not ok or not data:
            return "❓ 获取群公告失败或暂无公告。"
        return f"📢 当前群公告：\n{data}"

    # ---------- 群成员信息 ----------
    async def member_overview(self, event) -> str:
        ok, data = await self.call_api(event, "get_group_member_list", group_id=self.group_id(event))
        if not ok or not isinstance(data, list):
            return "❌ 获取群成员列表失败（当前协议端可能不支持）。"
        total = len(data)
        admins = [m for m in data if m.get("role") in ("admin", "owner")]
        owner = next((m for m in data if m.get("role") == "owner"), None)
        lines = [f"👥 本群共 {total} 人"]
        if owner:
            lines.append(f"👑 群主：{owner.get('card') or owner.get('nickname')}")
        lines.append(f"🛡️ 管理员：{len(admins)} 人")
        return "\n".join(lines)
