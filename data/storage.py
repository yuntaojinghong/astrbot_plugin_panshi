"""JSON 持久化存储层。

数据保存在 AstrBot 的 data 目录下（而非插件目录），
避免插件更新/重装时数据被覆盖。

存储结构::

    {
      "groups": {
        "123456": { ...按群配置覆盖... }
      },
      "users": {
        "123456_789": {
          "points": 100,
          "checkin_date": "2026-09-12",
          "warnings": [{"reason": "刷屏", "time": "2026-09-12 14:00:00"}],
          "messages": 233
        }
      },
      "blacklist": {
        "123456": [789, 790]
      },
      "stats": {
        "123456_789": {"last_msg_ts": 1700000000, "count": 5}
      }
    }
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

try:
    from astrbot.api import logger
except Exception:  # 便于脱离 AstrBot 单测
    import logging

    logger = logging.getLogger("panshi")


class Storage:
    """线程安全的 JSON 存储。"""

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.file = os.path.join(data_dir, "panshi_data.json")
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {
            "groups": {},
            "users": {},
            "blacklist": {},
            "stats": {},
        }
        self._load()

    # ---------- 基础读写 ----------
    def _load(self):
        os.makedirs(self.data_dir, exist_ok=True)
        if os.path.exists(self.file):
            try:
                with open(self.file, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self._data.update(data)
            except Exception as e:
                logger.error(f"[磐石] 读取数据文件失败: {e}")

    def save(self):
        """保存到磁盘（原子写入）。"""
        with self._lock:
            try:
                os.makedirs(self.data_dir, exist_ok=True)
                tmp = self.file + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self._data, f, ensure_ascii=False, indent=2)
                os.replace(tmp, self.file)
            except Exception as e:
                logger.error(f"[磐石] 保存数据文件失败: {e}")

    # ---------- 用户数据 ----------
    @staticmethod
    def user_key(group_id: str | int, user_id: str | int) -> str:
        return f"{group_id}_{user_id}"

    def _user(self, group_id, user_id) -> dict:
        key = self.user_key(group_id, user_id)
        return self._data["users"].setdefault(
            key,
            {"points": 0, "checkin_date": "", "warnings": [], "messages": 0},
        )

    def get_user(self, group_id, user_id) -> dict:
        return dict(self._user(group_id, user_id))

    # ---------- 积分 / 签到 ----------
    def add_points(self, group_id, user_id, amount: int) -> int:
        with self._lock:
            u = self._user(group_id, user_id)
            u["points"] = int(u.get("points", 0)) + amount
            self.save()
            return u["points"]

    def get_points(self, group_id, user_id) -> int:
        return int(self._user(group_id, user_id).get("points", 0))

    def has_checked_in(self, group_id, user_id) -> bool:
        today = time.strftime("%Y-%m-%d")
        return self._user(group_id, user_id).get("checkin_date") == today

    def set_checkin(self, group_id, user_id) -> None:
        with self._lock:
            self._user(group_id, user_id)["checkin_date"] = time.strftime("%Y-%m-%d")
            self.save()

    def top_points(self, group_id, limit: int = 10) -> list[tuple[str, int]]:
        """返回群内积分排行 [(user_id, points), ...]。"""
        prefix = f"{group_id}_"
        rows = []
        for key, u in self._data["users"].items():
            if key.startswith(prefix):
                rows.append((key[len(prefix):], int(u.get("points", 0))))
        rows.sort(key=lambda x: x[1], reverse=True)
        return rows[:limit]

    # ---------- 警告 ----------
    def add_warning(self, group_id, user_id, reason: str, expire_days: int = 30) -> int:
        """记一次警告，返回累计有效警告数。"""
        with self._lock:
            u = self._user(group_id, user_id)
            warnings = u.setdefault("warnings", [])
            warnings.append(
                {"reason": reason or "违规", "time": time.strftime("%Y-%m-%d %H:%M:%S")}
            )
            u["warnings"] = self._purge_warnings(warnings, expire_days)
            self.save()
            return len(u["warnings"])

    def get_warnings(self, group_id, user_id, expire_days: int = 30) -> list[dict]:
        with self._lock:
            u = self._user(group_id, user_id)
            u["warnings"] = self._purge_warnings(u.get("warnings", []), expire_days)
            return list(u["warnings"])

    def clear_warnings(self, group_id, user_id, count: int = 0) -> int:
        """清除警告。count<=0 表示清空全部，否则移除最近 count 条。"""
        with self._lock:
            u = self._user(group_id, user_id)
            warnings = u.get("warnings", [])
            if count <= 0:
                u["warnings"] = []
            else:
                u["warnings"] = warnings[:-count] if count <= len(warnings) else []
            self.save()
            return len(u["warnings"])

    @staticmethod
    def _purge_warnings(warnings: list[dict], expire_days: int) -> list[dict]:
        if not expire_days or expire_days <= 0:
            return warnings
        cutoff = time.time() - expire_days * 86400
        kept = []
        for w in warnings:
            try:
                ts = time.mktime(time.strptime(w.get("time", ""), "%Y-%m-%d %H:%M:%S"))
            except Exception:
                ts = time.time()
            if ts >= cutoff:
                kept.append(w)
        return kept

    # ---------- 发言统计（刷屏/活跃）----------
    def record_message(self, group_id, user_id) -> int:
        """记录一条发言，返回该用户累计发言数。"""
        with self._lock:
            u = self._user(group_id, user_id)
            u["messages"] = int(u.get("messages", 0)) + 1
            self._data["stats"].setdefault(
                self.user_key(group_id, user_id), {"times": []}
            )
            # 统计不即时落盘，仅在 save() 时统一写（由调用方控制频率）
            return u["messages"]

    def get_message_count(self, group_id, user_id) -> int:
        return int(self._user(group_id, user_id).get("messages", 0))

    def top_messages(self, group_id, limit: int = 10) -> list[tuple[str, int]]:
        prefix = f"{group_id}_"
        rows = []
        for key, u in self._data["users"].items():
            if key.startswith(prefix):
                rows.append((key[len(prefix):], int(u.get("messages", 0))))
        rows.sort(key=lambda x: x[1], reverse=True)
        return rows[:limit]

    # ---------- 黑名单 ----------
    def add_blacklist(self, group_id, user_id) -> bool:
        with self._lock:
            lst = self._data["blacklist"].setdefault(str(group_id), [])
            uid = int(user_id)
            if uid not in lst:
                lst.append(uid)
                self.save()
                return True
            return False

    def remove_blacklist(self, group_id, user_id) -> bool:
        with self._lock:
            lst = self._data["blacklist"].setdefault(str(group_id), [])
            uid = int(user_id)
            if uid in lst:
                lst.remove(uid)
                self.save()
                return True
            return False

    def is_blacklisted(self, group_id, user_id) -> bool:
        return int(user_id) in self._data["blacklist"].get(str(group_id), [])

    # ---------- 按群配置覆盖 ----------
    def get_group_override(self, group_id) -> dict:
        return dict(self._data["groups"].get(str(group_id), {}))

    def set_group_override(self, group_id, key: str, value) -> None:
        with self._lock:
            self._data["groups"].setdefault(str(group_id), {})[key] = value
            self.save()

    def clear_group_override(self, group_id, key: str) -> None:
        """删除某群的某个覆盖项，但保留 ``follow_default`` 标记。

        注意：即使该群已无任何实际覆盖，也不能顺手删掉 follow_default，
        否则用户刚设为「独立配置」的群会被误判回「跟随全局」。
        """
        with self._lock:
            gid = str(group_id)
            grp = self._data["groups"].get(gid)
            if isinstance(grp, dict):
                grp.pop(key, None)
            self.save()

    def reset_group(self, group_id) -> None:
        with self._lock:
            self._data["groups"].pop(str(group_id), None)
            self.save()
