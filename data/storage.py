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

from astrbot.api import logger


class Storage:
    """线程安全的 JSON 存储。"""

    #: 「积分跨群共用」模式下所有群共用的保留群号。
    #:
    #: 用双下划线包住，和真实 QQ 群号（纯数字）不可能撞车；
    #: 也方便 ``overview()`` 之类的地方一眼认出"这不是一个真实的群"。
    SHARED_GROUP = "__shared__"

    def __init__(self, data_dir: str, points_shared: bool = False):
        self.data_dir = data_dir
        self.file = os.path.join(data_dir, "panshi_data.json")
        self._lock = threading.RLock()
        #: 积分（含签到日期）是否跨群共用；由插件按配置同步（set_points_shared）
        self.points_shared = bool(points_shared)
        self._data: dict[str, Any] = {
            "groups": {},
            "users": {},
            "blacklist": {},
            "stats": {},
            # 自主还手的冷却与当日计数（按群），需持久化以便重启后仍然限流
            "defense": {},
            # 积分商城的库存与销量（跨群全局）
            "shop": {"stock": {}, "sold": {}},
        }
        self._load()

    # ---------- 自主还手 ----------
    def get_defense(self) -> dict:
        """读取自主还手状态（供 DefenseState.from_dict）。"""
        with self._lock:
            d = self._data.get("defense")
            return dict(d) if isinstance(d, dict) else {}

    def set_defense(self, payload: dict) -> None:
        """写入自主还手状态并落盘。"""
        with self._lock:
            self._data["defense"] = dict(payload or {})
        self.save()

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

    # ---------- 积分商城 / 抽奖 ---------- #
    #
    # 这些是**跨群全局**的：商品与奖池由管理员统一配置，积分本身按群隔离
    # （复用 users 表的 points）。所以这里不再按 group 分表。
    #
    # 购买/抽奖记录保留在 users[key]["purchases"] / ["draws"] 里，便于限购统计。

    def _shop_table(self) -> dict:
        return self._data.setdefault("shop", {"stock": {}, "sold": {}})

    def get_stock(self, item_id: str) -> int | None:
        """剩余库存。``None`` = 不限量。"""
        raw = self._shop_table()["stock"].get(str(item_id))
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    def set_stock(self, item_id: str, value: int | None) -> None:
        with self._lock:
            t = self._shop_table()
            if value is None:
                t["stock"].pop(str(item_id), None)
            else:
                t["stock"][str(item_id)] = int(value)
            self.save()

    def decrement_stock(self, item_id: str, n: int = 1) -> bool:
        """扣库存。不限量时直接成功；库存不足返回 False。

        与 get_stock 之间有竞态风险，所以扣减在锁内用当前值重新判断，
        不依赖调用方先前读到的数字。
        """
        with self._lock:
            t = self._shop_table()
            key = str(item_id)
            cur = t["stock"].get(key)
            if cur is None:
                return True
            try:
                cur = int(cur)
            except (TypeError, ValueError):
                return True
            if cur < n:
                return False
            t["stock"][key] = cur - n
            self.save()
            return True

    def sold_count(self, item_id: str) -> int:
        return int(self._shop_table()["sold"].get(str(item_id), 0) or 0)

    def bump_sold(self, item_id: str, n: int = 1) -> None:
        with self._lock:
            t = self._shop_table()
            key = str(item_id)
            t["sold"][key] = int(t["sold"].get(key, 0) or 0) + n
            self.save()

    # ---------- 商城 / 奖池的可编辑数据 ---------- #
    #
    # 商品与奖池本来只存在 _conf_schema.json 里，用户要在配置页手写一大段
    # JSON 数组，很容易写错、也很难改。所以改成：
    #
    #   * 配置里的 items / prizes 作为**初始默认值**（首次或清空后使用）
    #   * 一旦用户在面板里改过，就以这里的数据为准
    #
    # 这样既有出厂默认，又能用图形界面自由增删。

    def get_shop_items(self) -> list | None:
        """用户在面板里编辑过的商品列表。``None`` = 从未编辑，用配置默认值。"""
        v = self._shop_table().get("items")
        return list(v) if isinstance(v, list) else None

    def debug_shop_state(self) -> str:
        """一行说清当前存了什么、存在哪个文件。

        排查「保存后刷新又回默认值」用：把它打进日志，
        就能区分「没写进去」和「读回来失败」。
        """
        t = self._shop_table()
        return (
            f"文件={self.file} 存在={os.path.exists(self.file)} "
            f"items={len(t.get('items') or [])} "
            f"prizes={len(t.get('prizes') or [])} "
            f"settings={t.get('settings') or {}}"
        )

    def set_shop_items(self, items: list) -> None:
        with self._lock:
            self._shop_table()["items"] = list(items or [])
            self.save()
        # 记一条读得懂的日志。线上出现过「保存后刷新又回默认值」，
        # 单看界面分不清是没写下去、还是读回来失败。
        # 这条 + get_shop_items 那条日志能直接对上：
        # 保存时写了什么、下次读的时候文件里有什么。
        logger.info(
            "[磐石] 已保存商品 %s 件 -> %s", len(items or []), self.file,
        )

    def get_prizes(self) -> list | None:
        """用户在面板里编辑过的奖池。``None`` = 从未编辑，用配置默认值。"""
        v = self._shop_table().get("prizes")
        return list(v) if isinstance(v, list) else None

    def set_prizes(self, prizes: list) -> None:
        with self._lock:
            self._shop_table()["prizes"] = list(prizes or [])
            self.save()

    def reset_shop_data(self) -> None:
        """清空面板编辑过的商品/奖池，回到配置默认值。"""
        with self._lock:
            t = self._shop_table()
            t.pop("items", None)
            t.pop("prizes", None)
            self.save()

    # ---------- 商城 / 抽奖 的开关与参数 ---------- #
    #
    # 和 items / prizes 一样：配置里的值作为**默认**，面板里改过就以这里为准。
    # 这样「启用商城」「每次抽奖消耗多少」这些也都能在面板里改，
    # 不用再跳回 AstrBot 原生配置页。

    def get_shop_settings(self) -> dict:
        """面板里保存过的商城/抽奖参数；没保存过返回空 dict。"""
        v = self._shop_table().get("settings")
        return dict(v) if isinstance(v, dict) else {}

    def set_shop_settings(self, values: dict) -> None:
        with self._lock:
            t = self._shop_table()
            cur = t.get("settings")
            cur = dict(cur) if isinstance(cur, dict) else {}
            cur.update(values or {})
            t["settings"] = cur
            self.save()
        logger.info(
            "[磐石] 已保存商城设置 %s -> %s", dict(values or {}), self.file,
        )

    def clear_shop_settings(self) -> None:
        with self._lock:
            self._shop_table().pop("settings", None)
            self.save()

    # ---- 每人限购 / 每人每日抽奖次数 ---- #

    def _purchase_count(self, group_id, user_id, item_id: str,
                        today: str = "") -> int:
        u = self._user(group_id, user_id)
        recs = u.get("purchases") or []
        iid = str(item_id)
        n = 0
        for r in recs:
            if not isinstance(r, dict) or str(r.get("item")) != iid:
                continue
            if today and str(r.get("date")) != today:
                continue
            n += 1
        return n

    def purchase_count_total(self, group_id, user_id, item_id: str) -> int:
        return self._purchase_count(group_id, user_id, item_id)

    def purchase_count_today(self, group_id, user_id, item_id: str) -> int:
        return self._purchase_count(group_id, user_id, item_id,
                                    time.strftime("%Y-%m-%d"))

    def record_purchase(self, group_id, user_id, item_id: str,
                        cost: int, note: str = "") -> None:
        with self._lock:
            u = self._user(group_id, user_id)
            recs = u.setdefault("purchases", [])
            recs.append({
                "item": str(item_id), "cost": int(cost),
                "date": time.strftime("%Y-%m-%d"), "ts": time.time(),
                "note": str(note or ""),
            })
            # 只留最近 200 条，避免无限增长
            if len(recs) > 200:
                del recs[:-200]
            self.save()

    def _draw_count(self, group_id, user_id, today: str = "") -> int:
        u = self._user(group_id, user_id)
        recs = u.get("draws") or []
        if not today:
            return len(recs)
        return sum(1 for r in recs
                   if isinstance(r, dict) and str(r.get("date")) == today)

    def draw_count_today(self, group_id, user_id) -> int:
        return self._draw_count(group_id, user_id, time.strftime("%Y-%m-%d"))

    def draw_count_total(self, group_id, user_id) -> int:
        return self._draw_count(group_id, user_id)

    def record_draw(self, group_id, user_id, prize_id: str,
                    cost: int = 0, note: str = "") -> None:
        with self._lock:
            u = self._user(group_id, user_id)
            recs = u.setdefault("draws", [])
            recs.append({
                "prize": str(prize_id), "cost": int(cost),
                "date": time.strftime("%Y-%m-%d"), "ts": time.time(),
                "note": str(note or ""),
            })
            if len(recs) > 200:
                del recs[:-200]
            self.save()

    def recent_purchases(self, group_id, user_id, limit: int = 10) -> list[dict]:
        recs = self._user(group_id, user_id).get("purchases") or []
        return [r for r in recs if isinstance(r, dict)][-max(1, int(limit)):][::-1]

    def recent_draws(self, group_id, user_id, limit: int = 10) -> list[dict]:
        recs = self._user(group_id, user_id).get("draws") or []
        return [r for r in recs if isinstance(r, dict)][-max(1, int(limit)):][::-1]

    # ---------- 人工发放订单 ---------- #
    #
    # 「需要管理员人工发放」的成交（买奶茶、抽到 Steam…）以前只在对话里说一句
    # 「已记录」，管理员一不看消息就丢了。这里落成一张**有单号的订单**：
    # 能通知、能查、能核销，事后可追溯。
    #
    # 只保留最近 MAX_ORDERS 条，避免长期运行无限增长。

    MAX_ORDERS = 500

    def new_order(self, *, group_id, user_id, user_name, kind: str,
                  item: str, cost: int, value: str = "",
                  deliver: str = "") -> dict:
        """开一张待人工发放的订单，返回订单 dict（含递增单号 ``no``）。"""
        with self._lock:
            seq = int(self._data.get("order_seq", 0) or 0) + 1
            self._data["order_seq"] = seq
            order = {
                "no": seq,
                "group_id": str(group_id),
                "user_id": str(user_id),
                "user_name": str(user_name or ""),
                "kind": str(kind or ""),          # shop / lottery
                "item": str(item or ""),
                "cost": int(cost or 0),
                "value": str(value or ""),        # 发放内容（头衔名/积分数…）
                "deliver": str(deliver or ""),    # 发放方式：points/title/manual/action
                "status": "pending",              # pending / done / cancel
                "created": int(time.time()),
                "done_at": 0,
                "done_by": "",
                "note": "",
            }
            orders = self._data.setdefault("orders", [])
            orders.append(order)
            if len(orders) > self.MAX_ORDERS:
                del orders[: len(orders) - self.MAX_ORDERS]
            self.save()
            return dict(order)

    def get_order(self, no) -> dict | None:
        try:
            target = int(no)
        except (TypeError, ValueError):
            return None
        for o in self._data.get("orders", []):
            if isinstance(o, dict) and int(o.get("no", 0) or 0) == target:
                return dict(o)
        return None

    def list_orders(self, *, group_id=None, status: str = "",
                    user_id=None, limit: int = 20) -> list[dict]:
        """按条件列订单，最新的在前。"""
        out = []
        for o in self._data.get("orders", []):
            if not isinstance(o, dict):
                continue
            if group_id not in (None, "") and str(o.get("group_id")) != str(group_id):
                continue
            if user_id not in (None, "") and str(o.get("user_id")) != str(user_id):
                continue
            if status and str(o.get("status")) != status:
                continue
            out.append(dict(o))
        out.reverse()
        return out[: max(1, int(limit))]

    def close_order(self, no, *, by: str = "", note: str = "",
                    status: str = "done") -> dict | None:
        """把订单标记为已完成（或已取消）；返回更新后的订单（不存在则 None）。"""
        try:
            target = int(no)
        except (TypeError, ValueError):
            return None
        want = str(status or "done").strip().lower()
        if want not in ("done", "cancel"):
            want = "done"
        with self._lock:
            for o in self._data.get("orders", []):
                if not isinstance(o, dict) or int(o.get("no", 0) or 0) != target:
                    continue
                o["status"] = want
                o["done_at"] = int(time.time())
                o["done_by"] = str(by or "")
                o["note"] = str(note or "")
                self.save()
                return dict(o)
        return None

    # ---------- 积分 / 签到 ----------
    #
    # 积分支持「跨群共用」：打开后所有群的积分（含签到日期）都落到
    # :attr:`SHARED_GROUP` 这个保留群号上，于是天然就是同一份。
    #
    # 注意只有积分与签到走这个映射；违规记录、发言数、购买/抽奖记录、
    # 限购计数**仍然按各自的群走**。「一个群的违规不该跨群累计」和
    # 「积分可以跨群花」是两件事，不能一起合并。

    def set_points_shared(self, enabled: bool) -> None:
        """切换「积分跨群共用」。

        只改一个标志位，**不动任何数据**：

        * 打开 → 之后的读写都落到 ``SHARED_GROUP`` 上，所有群看到同一份积分；
        * 关闭 → 回到「每个群各算各的」。

        按用户的要求，切换后积分**从零重算**：不去把各群历史分求和搬过来，
        那样会合成一个谁都看不懂的数字，也容易把限购/保底之类的计数算穿。
        历史数据仍留在原处，切回来还能看到。
        """
        enabled = bool(enabled)
        if enabled == self.points_shared:
            return
        self.points_shared = enabled
        logger.info(
            f"[磐石] 积分跨群共用已{'开启（所有群共用一份积分，签到每天只算一次）' if enabled else '关闭（每群各算各的）'}")

    def _points_group(self, group_id) -> str:
        """积分（含签到日期）归属的「群号」。"""
        if getattr(self, "points_shared", False):
            return self.SHARED_GROUP
        return str(group_id)

    def add_points(self, group_id, user_id, amount: int) -> int:
        with self._lock:
            u = self._user(self._points_group(group_id), user_id)
            u["points"] = int(u.get("points", 0)) + amount
            self.save()
            remaining = u["points"]
        # 卖身契分成：只在**加分**时抽成，扣分不抽。
        #
        # 放在 add_points 里统一处理，而不是在每个加分点（签到/发言/互动/
        # 商城/小游戏…）各挂一次——那些点散在四五个文件里，漏一个就会出现
        # 「签到能抽成、买商品抽不到」这种诡异的不一致。
        #
        # 钩子由 main 注入（指向 ContractHandle.tribute），存储层因此完全
        # 不需要知道"卖身契"这个概念，分层保持干净。扣分（amount<0）不抽。
        if amount > 0:
            hook = getattr(self, "_points_tribute_hook", None)
            if callable(hook):
                try:
                    hook(str(group_id), str(user_id), int(amount))
                except Exception as e:      # 分成失败绝不能影响加分本身
                    logger.info(f"[磐石] 积分分成钩子异常（已忽略）: {e}")
        return remaining

    def get_points(self, group_id, user_id) -> int:
        return int(self._user(self._points_group(group_id), user_id).get("points", 0))

    def has_checked_in(self, group_id, user_id) -> bool:
        today = time.strftime("%Y-%m-%d")
        return self._user(self._points_group(group_id), user_id).get("checkin_date") == today

    def set_checkin(self, group_id, user_id) -> None:
        with self._lock:
            u = self._user(self._points_group(group_id), user_id)
            u["checkin_date"] = time.strftime("%Y-%m-%d")
            self.save()

    def top_points(self, group_id, limit: int = 10) -> list[tuple[str, int]]:
        """返回积分排行 [(user_id, points), ...]。

        共用模式下 ``_points_group`` 会把所有群映射到同一个键上，
        所以这里自然就是「全服榜」——这正是"共用"该有的样子。
        """
        prefix = f"{self._points_group(group_id)}_"
        rows = []
        for key, u in self._data["users"].items():
            if not key.startswith(prefix):
                continue
            uid = key[len(prefix):]
            if uid.startswith("__"):
                continue          # 内部账本残留（历史假用户），不是人
            rows.append((uid, int(u.get("points", 0))))
        rows.sort(key=lambda x: x[1], reverse=True)
        return rows[:limit]

    # ---------- 积分获取途径用的小账本 ---------- #
    #
    # 连签天数、当日首次名额、当日计数、一次性名额。全部挂在
    # ``_points_group`` 上，于是**共用模式下它们也自动全局化**：
    # 「今天第一个签到的」在共用模式里就是全服第一个，语义正确。

    def get_checkin_date(self, group_id, user_id) -> str:
        """上次签到的日期（可能是很久以前，也可能是空串）。"""
        return str(
            self._user(self._points_group(group_id), user_id)
            .get("checkin_date", "") or "")

    def get_streak(self, group_id, user_id) -> int:
        """连续签到天数。"""
        try:
            return int(
                self._user(self._points_group(group_id), user_id)
                .get("checkin_streak", 0) or 0)
        except (TypeError, ValueError):
            return 0

    def set_streak(self, group_id, user_id, n: int) -> None:
        with self._lock:
            u = self._user(self._points_group(group_id), user_id)
            u["checkin_streak"] = int(n)
            self.save()

    def claim_once(self, group_id, scope: str, user_id: str = "") -> bool:
        """抢占一个「只发生一次」的名额；抢到返回 True。

        用于新人礼包这类一次性发奖。并发下不能"先读再写"，否则两个请求
        会同时读到"还没发过"，都发一次。
        """
        return self._claim_flag(self._points_group(group_id),
                                f"once:{scope}", user_id, scope_date="")

    def claim_daily(self, group_id, scope: str, user_id: str = "") -> bool:
        """抢占一个「当天唯一」的名额；抢到返回 True。

        用于「当日第一个签到」「每日首次发言」这类每天只该发生一次的发奖。
        日期参与比对，跨天自动重新可抢；同样是锁内判断 + 写入。
        """
        return self._claim_flag(self._points_group(group_id),
                                f"daily:{scope}", user_id,
                                scope_date=time.strftime("%Y-%m-%d"))

    def _claim_flag(self, group_id: str, key: str, user_id: str,
                    scope_date: str) -> bool:
        with self._lock:
            # 用**独立账本**而不是往 users 里塞一个 "__flags__" 假用户：
            # 假用户会被积分排行、纳管人数、活跃用户这些统计当成真人算进去。
            flags = self._data.setdefault("flags", {})
            full = f"{group_id}:{key}:{user_id}" if user_id else f"{group_id}:{key}"
            rec = flags.get(full)
            if isinstance(rec, dict):
                if not scope_date:          # 一次性名额：占过就永远不给
                    return False
                if rec.get("date") == scope_date:
                    return False
            flags[full] = {"date": scope_date or "once"}
            self.save()
            return True

    def daily_count(self, group_id, user_id, scope: str) -> int:
        """当日计数（跨天自动归零）。"""
        rec = self._user(self._points_group(group_id), user_id) \
            .get("counters", {}).get(scope)
        if isinstance(rec, dict) and rec.get("date") == time.strftime("%Y-%m-%d"):
            try:
                return int(rec.get("n", 0) or 0)
            except (TypeError, ValueError):
                return 0
        return 0

    def bump_daily_count(self, group_id, user_id, scope: str, n: int = 1) -> int:
        """当日计数 +``n`` 并返回新值。

        ``n`` 传的是**实际发放的积分数**而不是"次数"——于是「每人每天
        最多得 X 分」这类上限可以按分精确卡住，不会出现"上限 50 分、
        每笔 +20、第三笔还能拿"的溢出。
        """
        with self._lock:
            u = self._user(self._points_group(group_id), user_id)
            counters = u.setdefault("counters", {})
            today = time.strftime("%Y-%m-%d")
            rec = counters.get(scope)
            cur = 0
            if isinstance(rec, dict) and rec.get("date") == today:
                try:
                    cur = int(rec.get("n", 0) or 0)
                except (TypeError, ValueError):
                    cur = 0
            counters[scope] = {"date": today, "n": cur + int(n)}
            self.save()
            return cur + int(n)

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

    # ---------- 宵禁全体禁言状态（持久化，跨重启可恢复）----------
    def get_curfew_banned(self) -> list[str]:
        """返回「当前被磐石宵禁置为全体禁言」的群号列表。

        必须落盘：插件重启后内存里的 ``_enforcing`` 会丢失，若只剩内存标记，
        一个已被全体禁言的群将永远没人去解除（Issue #2 的残留形态）。
        """
        raw = self._data.get("curfew_banned", [])
        return [str(g) for g in raw] if isinstance(raw, list) else []

    def set_curfew_banned(self, group_ids) -> None:
        """整表替换宵禁禁言群列表。"""
        with self._lock:
            self._data["curfew_banned"] = [str(g) for g in (group_ids or []) if str(g)]
            self.save()

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
