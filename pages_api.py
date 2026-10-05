"""面板后端 Web API。

通过 ``context.register_web_api()`` 注册路由，供 ``pages/settings/`` 下的
前端页面经 ``window.AstrBotPluginPage`` bridge 调用。

约定：
- 注册的路由带插件名前缀 ``/astrbot_plugin_panshi/...``
- 前端 bridge 调用时**不带**前缀，例如 ``apiGet("groups")``
- 响应统一用 ``astrbot.api.web`` 的 ``json_response`` / ``error_response``
"""

from __future__ import annotations

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
        # 这一处我来回错过三次，每次都是面板全部接口失效（「未找到该路由」），
        # 所以不再赌某一种写法，改成**只按后缀识别端点**。
        #
        # 真实 URL（用户实测）：
        #   /api/v1/plugins/extensions/astrbot_plugin_panshi/astrbot_plugin_panshi/shop/items
        #                            └────────── plugin_path 参数（整条） ──────────┘
        #
        # 路由声明是 /plugins/extensions/{plugin_path:path}，plugin_path 取整条剩余路径；
        # _match_registered_web_api 再拿它去 fullmatch 注册路径。
        # 这条 URL 里插件名出现了两次——外部 SDK 补了一层，调用方又拼了一层。
        # 与其去赌到底补几层，不如用通配匹配、靠后缀认端点：
        #
        #   /<path:rest>          匹配任何深度，rest 就是整条 plugin_path
        #
        # 这样无论 URL 里插件名出现 0 次、1 次还是 2 次，都能命中。
        rest_routes: list[tuple[str, Callable, list[str], str]] = [
            ("/<path:rest>", self.api_dispatch, ["GET"], "磐石面板接口（GET）"),
            ("/<path:rest>", self.api_dispatch, ["POST"], "磐石面板接口（POST）"),
            ("/<path:rest>", self.api_dispatch, ["DELETE"], "磐石面板接口（DELETE）"),
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

    #: 端点表：后缀 -> (处理函数名, 允许的方法)
    def _endpoint_table(self) -> dict[str, tuple[str, set[str]]]:
        return {
            "bootstrap": ("api_bootstrap", {"GET"}),
            "overview": ("api_overview", {"GET"}),
            "connection": ("api_connection", {"GET"}),
            "export": ("api_export", {"GET"}),
            "import": ("api_import", {"POST"}),
            "groups": ("api_groups", {"GET"}),
            "groups/refresh": ("api_groups_refresh", {"POST"}),
            "global": ("api_get_global", {"GET"}),
            "group": ("api_get_group", {"GET"}),
            "group/reset": ("api_reset_group", {"POST"}),
            "shop/items": ("api_shop_items", {"GET"}),
            "shop/prizes": ("api_shop_prizes", {"GET"}),
            "shop/settings": ("api_shop_settings", {"GET"}),
            "shop/reset": ("api_shop_reset", {"POST"}),
            "shop/save-items": ("api_shop_save_items", {"POST"}),
            "shop/save-prizes": ("api_shop_save_prizes", {"POST"}),
            "shop/save-settings": ("api_shop_save_settings", {"POST"}),
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

    async def api_dispatch(self, rest: str = "", **kwargs):
        """通配入口：按 URL 后缀把请求分派到具体处理函数。

        为什么这样做：插件 API 的真实 URL 形状在不同调用方下不一致
        （插件名可能出现 0~2 次），写死任何一种注册路径都可能全部失配。
        用通配匹配、按后缀认端点，就与 URL 前缀无关了。

        ``rest`` 是框架从 ``/<path:rest>`` 里解出来的通配内容，会作为
        **关键字参数**传进来——所以这个签名里必须收它，否则直接
        ``TypeError: got an unexpected keyword argument 'rest'``。
        线上就是这么炸的。多收一个 ``**kwargs`` 兜住其它版本可能多传的参数。

        方法不符返回 405，端点不认识返回 404，都带可读说明。
        """
        # 优先用框架给的 rest（最可靠），取不到再从请求路径里推断
        endpoint = str(rest or "").strip("/")
        if not endpoint:
            endpoint = self._tail_of(request)
        else:
            endpoint = self._normalize_endpoint(endpoint)

        req_obj = getattr(request, "_request", None) or request
        method = str(getattr(req_obj, "method", "GET") or "GET").upper()

        table = self._endpoint_table()
        hit = table.get(endpoint)
        if hit is None:
            logger.warning(f"[磐石] 面板请求了未知端点: {endpoint!r}")
            return _err(f"未知接口 {endpoint!r}，请更新插件", 404)

        handler_name, allowed = hit
        if method not in allowed:
            return _err(
                f"{endpoint} 不接受 {method}（允许 {'/'.join(sorted(allowed))}）", 405)

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

    async def api_shop_settings(self):
        shop = self._shop()
        if shop is None:
            return _err("商城模块不可用", 503)
        return _ok(shop.editable_settings())

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
