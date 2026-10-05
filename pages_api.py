"""面板后端 Web API。

通过 ``context.register_web_api()`` 注册路由，供 ``pages/settings/`` 下的
前端页面经 ``window.AstrBotPluginPage`` bridge 调用。

约定：
- 注册的路由带插件名前缀 ``/astrbot_plugin_panshi/...``
- 前端 bridge 调用时**不带**前缀，例如 ``apiGet("groups")``
- 响应统一用 ``astrbot.api.web`` 的 ``json_response`` / ``error_response``
"""

from __future__ import annotations

import re
from typing import Any, Callable

from astrbot.api import logger

PLUGIN_NAME = "astrbot_plugin_panshi"

# astrbot.api.web 在较新版本才提供；老版本回退到 quart
_WEB_IMPORT_ERROR = ""
try:
    from astrbot.api.web import error_response, json_response, request
except Exception as e:  # pragma: no cover - 取决于 AstrBot 版本
    _WEB_IMPORT_ERROR = str(e)
    json_response = None  # type: ignore
    error_response = None  # type: ignore
    request = None  # type: ignore

    try:
        from quart import jsonify, request as _quart_request

        def json_response(payload: dict):  # type: ignore
            return jsonify(payload)

        def error_response(msg: str, status_code: int = 400):  # type: ignore
            return jsonify({"status": "error", "message": msg}), status_code

        request = _quart_request  # type: ignore
    except Exception:
        pass


class PanshiWebController:
    """注册并处理面板相关的 Web API。

    Args:
        context: AstrBot 的 Context
        service: :class:`PageService` 实例
    """

    def __init__(self, context: Any, service: Any):
        self.context = context
        self.service = service
        self._registered = False
        # 可选钩子：全局配置保存成功后调用（用于宵禁等设置即时生效）
        self.on_config_saved = None

    # ==================================================================
    #  注册
    # ==================================================================
    def register_routes(self) -> bool:
        """注册所有路由。返回是否成功。

        低版本 AstrBot 上没有 ``register_web_api``，此时返回 False，
        由调用方决定是否提示用户。
        """
        register = getattr(self.context, "register_web_api", None)
        if not callable(register):
            logger.warning(
                "[磐石] 当前 AstrBot 版本不支持插件 Pages（缺少 register_web_api），"
                "配置面板将不可用，请升级到 4.24.2 以上"
            )
            return False

        if request is None:
            logger.warning(f"[磐石] Web 请求模块不可用，跳过面板注册: {_WEB_IMPORT_ERROR}")
            return False

        routes: list[tuple[str, Callable, list[str], str]] = [
            ("/bootstrap", self.api_bootstrap, ["GET"], "面板初始化数据"),
            ("/overview", self.api_overview, ["GET"], "运行状态概览"),
            ("/connection", self.api_connection, ["GET"], "协议端连接诊断"),
            ("/export", self.api_export, ["GET"], "导出全部配置"),
            ("/import", self.api_import, ["POST"], "导入配置备份"),
            ("/groups", self.api_groups, ["GET"], "群列表"),
            ("/groups/refresh", self.api_groups_refresh, ["POST"], "刷新群列表"),
            ("/global", self.api_get_global, ["GET"], "全局默认配置"),
            ("/global", self.api_update_global, ["POST"], "保存全局默认配置"),
            ("/group", self.api_get_group, ["GET"], "单个群的配置"),
            ("/group", self.api_update_group, ["POST"], "保存单个群的配置"),
            ("/group/reset", self.api_reset_group, ["POST"], "重置单个群的配置"),
            # 积分商城 / 奖池的可视化编辑（省得在配置页手写 JSON 数组）
            ("/shop/items", self.api_shop_items, ["GET"], "商品列表"),
            ("/shop/items", self.api_shop_save_items, ["POST"], "保存商品列表"),
            ("/shop/prizes", self.api_shop_prizes, ["GET"], "奖池列表"),
            ("/shop/prizes", self.api_shop_save_prizes, ["POST"], "保存奖池"),
            ("/shop/reset", self.api_shop_reset, ["POST"], "恢复商品与奖池的默认值"),
            ("/shop/settings", self.api_shop_settings, ["GET"], "商城与抽奖的开关参数"),
            ("/shop/settings", self.api_shop_save_settings, ["POST"], "保存商城与抽奖参数"),
        ]

        # 注册路径形式。
        #
        # 这一处我来回错过几次，最初的版本写成裸的 `/<path:rest>`，结果
        # **抢了别的插件的接口**（线上日志实证）：
        #
        #   [磐石] 面板请求了未知端点: 'astrbot_plugin_proactive_care/config'
        #
        # 原因：`/<path:rest>` 里的 `.*` 能匹配**任何** plugin_path，于是
        # 「微光」插件的请求也被磐石先截胡，磐石认不出就回 404，
        # 真正该响应的插件处理器根本没机会执行。
        #
        # 现在带上**自己的插件名前缀**再通配：
        #
        #   /astrbot_plugin_panshi/<path:rest>
        #
        # 这样只匹配属于自己的请求，别人的请求原样落到对方处理器。
        # 同时**仍然容忍插件名重复出现**——真实 URL 里插件名会出现 0~2 次
        # （外部 SDK 补一层、调用方再拼一层）：
        #
        #   astrbot_plugin_panshi/shop/items                      → rest = shop/items
        #   astrbot_plugin_panshi/astrbot_plugin_panshi/shop/items → rest = astrbot_plugin_panshi/shop/items
        #
        # 后一种多出来的前缀由 `_normalize_endpoint()` 按「已知端点后缀」剥掉。
        prefix = f"/{PLUGIN_NAME}"
        rest_routes: list[tuple[str, Callable, list[str], str]] = [
            (f"{prefix}/<path:rest>", self.api_dispatch, ["GET"], "磐石面板接口（GET）"),
            (f"{prefix}/<path:rest>", self.api_dispatch, ["POST"], "磐石面板接口（POST）"),
            (f"{prefix}/<path:rest>", self.api_dispatch, ["DELETE"], "磐石面板接口（DELETE）"),
        ]

        ok_count = 0
        registered_paths: list[str] = []
        for path, handler, methods, desc in rest_routes:
            try:
                register(path, self._wrap(handler), methods, desc)
                ok_count += 1
                registered_paths.append(path)
            except Exception as e:
                logger.error(f"[磐石] 注册路由 {path} 失败: {e}")

        self._registered = ok_count > 0
        if self._registered:
            logger.info(
                f"[磐石] 配置面板已注册 {ok_count} 条通配接口"
                f"（按后缀识别端点，共 {len(routes)} 个端点）"
            )
        else:
            logger.warning(
                "[磐石] 配置面板一个接口都没注册上。"
                f"register_web_api 是否可用: {callable(register)}；"
                f"Web 请求模块: {'可用' if request is not None else _WEB_IMPORT_ERROR}"
            )
        return self._registered

    #: 端点表：后缀 -> {HTTP 方法: 处理函数名}
    #
    # 这里必须按**方法**分别映射，不能写成「一个处理函数 + 允许的方法集合」。
    # 原因：``global`` 与 ``group`` 是**同名不同方法的两个端点**——
    #   GET  /global  -> 读全局默认配置
    #   POST /global  -> 保存全局默认配置
    # 之前的表把 ``global`` / ``group`` 只映射到了 GET 的处理函数，于是面板
    # 「保存配置」发出的 POST 会被自己的路由判断挡下，回一句
    # 「global 不接受 POST（允许 GET）」，表现为**读得出来、就是存不进去**。
    # 因为 GET 一切正常、页面也打得开，这个 bug 藏了很久才被用户点到。
    def _endpoint_table(self) -> dict[str, dict[str, str]]:
        return {
            "bootstrap": {"GET": "api_bootstrap"},
            "overview": {"GET": "api_overview"},
            "connection": {"GET": "api_connection"},
            "export": {"GET": "api_export"},
            "import": {"POST": "api_import"},
            "groups": {"GET": "api_groups"},
            "groups/refresh": {"POST": "api_groups_refresh"},
            # 同一个端点名，GET 读 / POST 存——两个都要在，缺一个就「存不进去」
            "global": {"GET": "api_get_global", "POST": "api_update_global"},
            "group": {"GET": "api_get_group", "POST": "api_update_group"},
            "group/reset": {"POST": "api_reset_group"},
            "shop/items": {"GET": "api_shop_items"},
            "shop/prizes": {"GET": "api_shop_prizes"},
            "shop/settings": {"GET": "api_shop_settings"},
            "shop/reset": {"POST": "api_shop_reset"},
            "shop/save-items": {"POST": "api_shop_save_items"},
            "shop/save-prizes": {"POST": "api_shop_save_prizes"},
            "shop/save-settings": {"POST": "api_shop_save_settings"},
            # 发放方式预设（面板「一键填入」）
            "shop/preset": {"POST": "api_shop_preset"},
            # 人工发放订单：看待办 / 结单
            "shop/orders": {"GET": "api_shop_orders"},
            "shop/close-order": {"POST": "api_shop_close_order"},
            # 诊断：把后端此刻的状态原样给前端看，便于定位「保存成功但界面为空」
            "shop/debug": {"GET": "api_shop_debug"},
        }

    def _tail_of(self, request_: Any) -> str:
        """从请求里取出「端点后缀」，形如 ``"shop/items"``。

        优先用 FastAPI 解析出的通配参数；取不到再从完整路径里按
        **已知端点后缀**定位——这样与 URL 前头有多少层前缀无关。
        """
        params = getattr(request_, "path_params", None)
        if isinstance(params, dict) and params.get("rest"):
            return str(params["rest"]).strip("/")

        raw_path = ""
        for src in (request_, getattr(request_, "_request", None)):
            p = getattr(src, "path", None)
            if isinstance(p, str) and p:
                raw_path = p
                break
        if not raw_path:
            return ""
        raw_path = raw_path.split("?", 1)[0].strip("/")
        return self._normalize_endpoint(raw_path)

    def _normalize_endpoint(self, tail: str) -> str:
        """把一段路径映射成端点名。

        真实 URL 里插件名可能出现 0~2 次，所以不能假定 ``tail`` 就是端点：
        按**已知端点的最长后缀**匹配来剥离多余前缀。
        认不出来时原样返回，交给调用方报 404。
        """
        tail = str(tail or "").strip("/")
        if not tail:
            return ""
        table = self._endpoint_table()
        if tail in table:
            return tail
        best = ""
        for ep in table:
            if tail.endswith("/" + ep) and len(ep) > len(best):
                best = ep
        if best:
            return best
        # 都不认识，返回最后两段，便于日志里看出请求的是什么
        parts = tail.split("/")
        return "/".join(parts[-2:]) if len(parts) >= 2 else tail

    #: 明显属于**别的插件**的请求前缀。
    #:
    #: 路由已经带了自己的插件名（见 :meth:`register_routes`），正常情况下
    #: 别人的请求根本不会进到这里。但历史上出过一件事：注册的是裸的
    #: ``/<path:rest>``，于是「微光」插件的 ``config`` 请求被磐石截胡，
    #: 磐石认不出就回 404，真正该响应的处理器没机会执行。
    #:
    #: 所以这里再加一道**语义防线**：URL 里若出现 ``astrbot_plugin_`` 开头的
    #: 其它插件名，就明确说「这不是磐石的接口」，而不是含糊地报「未知端点」。
    _OTHER_PLUGIN_RE = re.compile(r"(astrbot_plugin_[A-Za-z0-9_]+)")

    def _looks_like_other_plugin(self, endpoint: str) -> str:
        """endpoint 里若含别的插件名，返回那个名字；否则返回空串。"""
        for name in self._OTHER_PLUGIN_RE.findall(str(endpoint or "")):
            if name != PLUGIN_NAME:
                return name
        return ""

    async def api_dispatch(self, rest: str = "", **kwargs):
        """通配入口：按 URL 后缀把请求分派到具体处理函数。

        为什么这样做：插件 API 的真实 URL 形状在不同调用方下不一致
        （插件名可能出现 0~2 次），写死任何一种注册路径都可能全部失配。
        用带插件名前缀的通配匹配 + 按后缀认端点，就既与 URL 前缀无关，
        又不会抢别的插件的请求。

        ``rest`` 是框架从 ``/<path:rest>`` 里解出来的通配内容，会作为
        **关键字参数**传进来——所以这个签名里必须收它，否则直接
        ``TypeError: got an unexpected keyword argument 'rest'``。
        线上就是这么炸的。多收一个 ``**kwargs`` 兜住其它版本可能多传的参数。

        方法不符返回 405，端点不认识返回 404，都带可读说明。
        """
        # 优先用框架给的 rest（最可靠），取不到再从请求路径里推断
        raw_endpoint = str(rest or "").strip("/")
        endpoint = raw_endpoint
        if not endpoint:
            endpoint = self._tail_of(request)
        else:
            endpoint = self._normalize_endpoint(endpoint)

        # 不是磐石的请求 → 明确让开，别抢答。
        # 抢答的后果不是"多一条日志"，而是**对方的插件彻底不能用**：
        # 它收到的响应是磐石的 404，看起来就像它自己坏了。
        other = self._looks_like_other_plugin(raw_endpoint or endpoint)
        if other:
            logger.warning(
                f"[磐石] 收到属于 {other!r} 的请求，已让开（不拦截其他插件的接口）")
            return _err(
                f"这是 {other} 的接口，不是磐石的。请检查请求地址是否正确。", 404)

        req_obj = getattr(request, "_request", None) or request
        method = str(getattr(req_obj, "method", "GET") or "GET").upper()

        table = self._endpoint_table()
        handlers = table.get(endpoint)
        if not handlers:
            logger.warning(f"[磐石] 面板请求了未知端点: {endpoint!r}")
            return _err(f"未知接口 {endpoint!r}，请更新插件", 404)

        handler_name = handlers.get(method)
        if handler_name is None:
            return _err(
                f"{endpoint} 不接受 {method}"
                f"（允许 {'/'.join(sorted(handlers))}）", 405)

        return await getattr(self, handler_name)()

    def _wrap(self, handler: Callable) -> Callable:
        """统一异常处理：把 ValueError 转成 400，其余转 500。"""

        async def wrapped(*args, **kwargs):
            try:
                return await handler(*args, **kwargs)
            except ValueError as e:
                return _err(str(e), 400)
            except Exception as e:  # pragma: no cover - 兜底
                logger.exception("[磐石] 面板接口异常")
                return _err(f"服务器内部错误: {e}", 500)

        wrapped.__name__ = getattr(handler, "__name__", "handler")
        return wrapped

    # ==================================================================
    #  Handlers
    # ==================================================================
    async def api_bootstrap(self):
        return _ok(await self.service.bootstrap())

    async def api_overview(self):
        return _ok(self.service.overview())

    async def api_connection(self):
        """协议端连接诊断：供面板「重新检测」使用。"""
        return _ok(self.service.connection())

    async def api_export(self):
        return _ok(self.service.export_all())

    async def api_import(self):
        payload = await _json_body()
        result = _ok(self.service.import_all(payload))
        # 导入会替换全局配置与各群覆盖，必须重新同步宵禁/公告任务
        await self._notify_saved()
        return result

    async def api_groups(self):
        force = _query_bool("force")
        return _ok(await self.service.list_groups(force=force))

    async def api_groups_refresh(self):
        # 重置缓存后重新拉取
        try:
            self.service.group_cache.invalidate()
        except Exception:
            pass
        groups = await self.service.list_groups(force=True)
        err = getattr(self.service.group_cache, "last_error", "")
        return _ok({"groups": groups, "error": err})

    async def api_get_global(self):
        return _ok(self.service.get_global_config())

    async def api_update_global(self):
        payload = await _json_body()
        config = payload.get("config", payload)
        result = _ok(self.service.update_global_config(config))
        await self._notify_saved()
        return result

    async def _notify_saved(self) -> None:
        """通知宿主插件做即时同步（宵禁启停 / 定时公告），失败不影响保存结果。

        按群配置也会影响宵禁：某个群若把 ``automate.curfew_enable`` 关掉，
        必须立刻重新同步，否则面板上「已关掉」而群里仍被执行全体禁言。
        """
        hook = getattr(self, "on_config_saved", None)
        if callable(hook):
            try:
                await hook()
            except Exception as e:
                logger.warning(f"[磐石] 配置保存后同步失败: {e}")

    async def api_get_group(self):
        gid = _query_str("group_id")
        if not gid:
            return _err("缺少参数 group_id", 400)
        return _ok(self.service.get_group_config(gid))

    async def api_update_group(self):
        payload = await _json_body()
        gid = payload.get("group_id")
        if not gid:
            return _err("缺少参数 group_id", 400)
        config = payload.get("config", {})
        result = _ok(self.service.update_group_config(str(gid), config))
        await self._notify_saved()
        return result

    async def api_reset_group(self):
        payload = await _json_body()
        gid = payload.get("group_id")
        if not gid:
            return _err("缺少参数 group_id", 400)
        result = _ok(self.service.reset_group_config(str(gid)))
        await self._notify_saved()
        return result

    # ------------------------------------------------------------------ #
    #  积分商城 / 奖池的可视化编辑
    # ------------------------------------------------------------------ #
    #
    # 商品和奖池原来只能去 AstrBot 配置页手写 JSON 数组，加一件商品要
    # 小心翼翼补逗号引号。这几个接口配合 pages/settings 里的管理页，
    # 让用户点「＋」填三个框就能加一件。
    #
    # 数据存在插件的 storage 里（配置里的默认值作为初始内容），
    # 所以改完立即生效，不需要重载插件。

    def _shop(self):
        """取 ShopHandle；没有（旧版本/未初始化）时返回 None。"""
        return getattr(self.service, "shop", None)

    async def api_shop_items(self):
        shop = self._shop()
        if shop is None:
            return _err("商城模块不可用", 503)
        return _ok({"items": shop.editable_items()})

    async def api_shop_save_items(self):
        shop = self._shop()
        if shop is None:
            return _err("商城模块不可用", 503)
        payload = await _json_body()
        items = payload.get("items")
        if not isinstance(items, list):
            return _err("缺少参数 items（数组）", 400)
        result = shop.save_items(items)
        if not result.get("ok"):
            # 校验失败要原样返回原因，前端才能逐条展示给用户
            return _err("；".join(result.get("problems") or ["保存失败"]), 400)
        return _ok({"items": shop.editable_items(), "saved": len(result["items"])})

    async def api_shop_prizes(self):
        shop = self._shop()
        if shop is None:
            return _err("商城模块不可用", 503)
        return _ok({"prizes": shop.editable_prizes()})

    async def api_shop_save_prizes(self):
        shop = self._shop()
        if shop is None:
            return _err("商城模块不可用", 503)
        payload = await _json_body()
        prizes = payload.get("prizes")
        if not isinstance(prizes, list):
            return _err("缺少参数 prizes（数组）", 400)
        result = shop.save_prizes(prizes)
        if not result.get("ok"):
            return _err("；".join(result.get("problems") or ["保存失败"]), 400)
        return _ok({"prizes": shop.editable_prizes(),
                    "saved": len(result["prizes"]),
                    "total_chance": result.get("total_chance", 0)})

    async def api_shop_reset(self):
        shop = self._shop()
        if shop is None:
            return _err("商城模块不可用", 503)
        shop.reset_shop_data()
        return _ok({"items": shop.editable_items(),
                    "prizes": shop.editable_prizes(),
                    "message": "已恢复为配置里的默认商品与奖池"})

    async def api_shop_debug(self):
        """诊断：后端此刻的内存状态 + 数据文件情况。

        只读，不改任何东西。用于排查「保存成功但界面显示为空」：
        对比这里返回的 items 数量和界面上看到的，就能判断是
        存储层、读取层还是渲染层的问题。
        """
        shop = self._shop()
        if shop is None:
            return _err("商城模块不可用", 503)
        db = getattr(shop, "db", None)
        info: dict = {
            "settings": shop.editable_settings(),
            "items_count": len(shop.editable_items()),
            "prizes_count": len(shop.editable_prizes()),
        }
        if db is not None:
            info["file"] = getattr(db, "file", "")
            try:
                import os as _os
                info["file_exists"] = _os.path.exists(info["file"])
                info["file_size"] = _os.path.getsize(info["file"]) \
                    if info["file_exists"] else 0
            except Exception as e:  # pragma: no cover
                info["file_error"] = str(e)
            raw_items = db.get_shop_items()
            info["stored_items_count"] = len(raw_items) if raw_items else 0
            info["stored_items"] = raw_items or []
            info["stored_settings"] = db.get_shop_settings()
        return _ok(info)

    async def api_shop_settings(self):
        shop = self._shop()
        if shop is None:
            return _err("商城模块不可用", 503)
        return _ok(shop.editable_settings())

    async def api_shop_preset(self):
        """一键填入推荐的商品/奖池（发放方式已配好）。

        **会覆盖**现有条目，所以前端必须先弹确认框。
        """
        shop = self._shop()
        if shop is None:
            return _err("商城模块不可用", 503)
        payload = await _json_body()
        kind = str(payload.get("kind") or "items").strip().lower()
        if kind not in ("items", "prizes"):
            return _err("kind 只能是 items 或 prizes", 400)
        result = shop.apply_preset(kind)
        if not result.get("ok"):
            return _err("；".join(result.get("problems") or ["填入失败"]), 400)
        if kind == "prizes":
            return _ok({"prizes": shop.editable_prizes(),
                        "count": result.get("count", 0),
                        "total_chance": result.get("total_chance", 0),
                        "message": f"已填入 {result.get('count', 0)} 个推荐奖品"})
        return _ok({"items": shop.editable_items(),
                    "count": result.get("count", 0),
                    "message": f"已填入 {result.get('count', 0)} 件推荐商品"})

    async def api_shop_orders(self):
        """人工发放订单列表（默认只看待发放）。

        面板顶栏的「📮 订单」用它：管理员一眼看到谁在等发货，
        点「核销」结单，群里那条通知不用再翻聊天记录。
        """
        shop = self._shop()
        if shop is None:
            return _err("商城模块不可用", 503)
        status = str(_query_str("status", "") or "").strip().lower()
        if status not in ("pending", "done", "cancel", ""):
            return _err("status 只能是 pending / done / cancel", 400)
        group_id = _query_str("group_id", "").strip()
        orders = shop.orders_view(group_id=group_id or None, limit=50)
        if status:
            orders = [o for o in orders if str(o.get("status")) == status]
        pending = [o for o in orders if str(o.get("status")) == "pending"]
        return _ok({
            "orders": orders,
            "pending_count": len(pending),
            "settings": shop.order_settings(),
        })

    async def api_shop_close_order(self):
        """核销 / 取消订单。"""
        shop = self._shop()
        if shop is None:
            return _err("商城模块不可用", 503)
        payload = await _json_body()
        no = payload.get("no")
        if no in (None, ""):
            return _err("缺少参数 no（单号）", 400)
        status = str(payload.get("status") or "done").strip().lower()
        if status not in ("done", "cancel"):
            return _err("status 只能是 done 或 cancel", 400)
        order = shop.db.close_order(no, by="panel", note=str(payload.get("note") or ""),
                                    status=status)
        if order is None:
            return _err(f"没有找到订单 #{no}", 404)
        return _ok({"order": order, "message":
                    f"订单 #{no} 已{'核销' if status == 'done' else '取消'}"})

    async def api_shop_save_settings(self):
        """保存开关与参数（启用商城/抽奖、消耗、每日次数、保底）。

        这些原本只能去 AstrBot 原生配置页改，现在直接在插件面板里改，
        不用来回跳。
        """
        shop = self._shop()
        if shop is None:
            return _err("商城模块不可用", 503)
        payload = await _json_body()
        values = payload.get("settings", payload)
        if not isinstance(values, dict) or not values:
            return _err("缺少参数 settings（对象）", 400)
        result = shop.save_settings(values)
        if not result.get("ok"):
            return _err("；".join(result.get("problems") or ["保存失败"]), 400)
        return _ok(result["settings"])


# ======================================================================
#  请求 / 响应辅助
# ======================================================================
def _ok(data: Any):
    if json_response is None:
        raise RuntimeError("Web 响应模块不可用")
    return json_response(data)


def _err(message: str, status_code: int = 400):
    if error_response is None:
        raise RuntimeError("Web 响应模块不可用")
    return error_response(message, status_code=status_code)


def _query_str(key: str, default: str = "") -> str:
    try:
        val = request.query.get(key, default)
        return str(val) if val is not None else default
    except Exception:
        return default


def _query_bool(key: str) -> bool:
    raw = _query_str(key, "").strip().lower()
    return raw in ("1", "true", "yes", "on")


async def _json_body() -> dict:
    try:
        payload = await request.json(default={})
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}
