"""微博媒体推送插件 Plugin Page 的后端接口。"""

import asyncio
import re
from typing import Any

from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request

PLUGIN_NAME = "astrbot_plugin_weibo_media_push"
MESSAGE_TYPES = {"GroupMessage", "FriendMessage", "OtherMessage"}
GROUP_LIST_TIMEOUT_SECONDS = 6

_UID_PATTERNS = [
    re.compile(r"weibo\.com/u/(\d+)"),
    re.compile(r"m\.weibo\.cn/u/(\d+)"),
    re.compile(r"weibo\.cn/(\d+)"),
    re.compile(r"^\s*(\d{6,})\s*$"),
]


def extract_uid(text: str) -> str | None:
    """从主页链接或纯数字中提取微博 UID。"""
    text = (text or "").strip()
    for pattern in _UID_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1)
    return None


_NAME_PATTERNS = [
    re.compile(r"weibo\.com/(?!u/|n/|aj_|at/weibo)([A-Za-z0-9_\-\.]+)"),
    re.compile(r"^\s*([A-Za-z][A-Za-z0-9_\-\.]{2,})\s*$"),
]


def extract_name(text: str) -> str | None:
    """从个性域名主页链接（如 weibo.com/AoiMomoko813）提取自定义名。"""
    text = (text or "").strip()
    for pattern in _NAME_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1)
    return None


class WeiboWebUIController:
    """注册并实现微博订阅管理页面所需的 Web API。"""

    def __init__(self, plugin: Any, context: Any):
        self.plugin = plugin
        self.context = context
        self._register_routes()

    def _register_routes(self) -> None:
        routes = (
            ("overview", self.overview, ["GET"], "微博订阅概览"),
            (
                "subscriptions/add",
                self.add_subscription,
                ["POST"],
                "新增微博订阅",
            ),
            (
                "subscriptions/update",
                self.update_subscription,
                ["POST"],
                "修改微博订阅",
            ),
            (
                "subscriptions/remove",
                self.remove_subscription,
                ["POST"],
                "移除微博订阅",
            ),
            (
                "groups/status",
                self.update_group_status,
                ["POST"],
                "统一修改群聊微博推送状态",
            ),
            (
                "settings/poll-interval",
                self.save_poll_interval,
                ["POST"],
                "修改微博轮询间隔",
            ),
            ("cookie", self.save_cookie, ["POST"], "更新微博 Cookie"),
        )
        for endpoint, handler, methods, description in routes:
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/{endpoint}",
                handler,
                methods,
                description,
            )

    # ---------- 工具 ----------

    @staticmethod
    def _meta_value(meta: Any, field: str, default: str = "") -> str:
        if isinstance(meta, dict):
            return str(meta.get(field) or default)
        return str(getattr(meta, field, None) or default)

    @staticmethod
    def _parse_umo(value: Any) -> tuple[str, str, str] | None:
        if not isinstance(value, str) or not value or len(value) > 512:
            return None
        if any(ord(char) < 32 for char in value):
            return None
        parts = value.split(":", 2)
        if len(parts) != 3 or not all(parts):
            return None
        platform_id, message_type, session_id = parts
        if message_type not in MESSAGE_TYPES:
            return None
        return platform_id, message_type, session_id

    def _platform_instances(self) -> list[Any]:
        manager = getattr(self.context, "platform_manager", None)
        if manager is None:
            return []
        instances = getattr(manager, "platform_insts", None)
        if instances is None:
            get_insts = getattr(manager, "get_insts", None)
            instances = get_insts() if callable(get_insts) else []
        return list(instances or [])

    def _aiocqhttp_platforms(self) -> list[Any]:
        result = []
        for platform in self._platform_instances():
            try:
                meta = platform.meta()
            except Exception:
                continue
            if self._meta_value(meta, "name").casefold() == "aiocqhttp":
                result.append(platform)
        return result

    async def _fetch_platform_groups(self, platform: Any) -> dict:
        meta = platform.meta()
        platform_id = self._meta_value(meta, "id")
        platform_label = self._meta_value(meta, "name", "aiocqhttp")
        result = {
            "platform_id": platform_id,
            "platform_name": platform_label,
            "available": False,
            "error": None,
            "groups": [],
        }
        if not platform_id:
            result["error"] = "平台实例缺少 ID"
            return result

        try:
            client = platform.get_client()
            response = await asyncio.wait_for(
                client.call_action(action="get_group_list"),
                timeout=GROUP_LIST_TIMEOUT_SECONDS,
            )
            if isinstance(response, dict):
                groups = response.get("data", response.get("groups", []))
            else:
                groups = response
            if not isinstance(groups, (list, tuple)):
                raise ValueError("get_group_list 返回格式不正确")

            normalized = []
            for group in groups:
                if not isinstance(group, dict):
                    continue
                group_id = str(group.get("group_id") or "").strip()
                if not group_id:
                    continue
                normalized.append(
                    {
                        "group_id": group_id,
                        "group_name": str(group.get("group_name") or "").strip(),
                    }
                )
            result["groups"] = normalized
            result["available"] = True
        except Exception as exc:
            result["error"] = str(exc) or exc.__class__.__name__
            logger.warning(f"获取 QQ 群列表失败 ({platform_id}): {exc}")
        return result

    async def _load_group_sources(self) -> list[dict]:
        platforms = self._aiocqhttp_platforms()
        if not platforms:
            return []
        return list(
            await asyncio.gather(
                *(self._fetch_platform_groups(p) for p in platforms)
            )
        )

    @staticmethod
    def _session_label(message_type: str, session_id: str) -> str:
        labels = {"FriendMessage": "私聊", "OtherMessage": "其他会话"}
        return f"{labels.get(message_type, message_type)} {session_id}"

    def _subscription_row(self, uid: str, info: dict) -> dict:
        return {
            "uid": str(uid),
            "screen_name": str(info.get("screen_name") or uid),
            "enabled": bool(info.get("status", True)),
            "pushed": int(info.get("pushed_count", 0)),
        }

    async def _build_overview(self) -> dict:
        subs, group_sources = await asyncio.gather(
            self.plugin._get_subs_data(),
            self._load_group_sources(),
        )
        subs = subs or {}

        session_subscriptions: dict[str, list[dict]] = {}
        total_relations = 0
        active_relations = 0
        for umo, entries in subs.items():
            if not isinstance(entries, dict):
                continue
            for uid, info in entries.items():
                if not isinstance(info, dict):
                    continue
                row = self._subscription_row(str(uid), info)
                session_subscriptions.setdefault(str(umo), []).append(row)
                total_relations += 1
                if row["enabled"]:
                    active_relations += 1

        groups_by_umo: dict[str, dict] = {}
        for source in group_sources:
            platform_id = source["platform_id"]
            for group in source["groups"]:
                umo = f"{platform_id}:GroupMessage:{group['group_id']}"
                groups_by_umo[umo] = {
                    "umo": umo,
                    "platform_id": platform_id,
                    "group_id": group["group_id"],
                    "group_name": group["group_name"] or f"群聊 {group['group_id']}",
                    "available": True,
                    "subscriptions": [],
                }

        other_sessions = []
        for umo, rows in session_subscriptions.items():
            parsed = self._parse_umo(umo)
            if parsed is None:
                other_sessions.append(
                    {
                        "umo": umo,
                        "platform_id": "",
                        "session_id": umo,
                        "session_type": "Unknown",
                        "session_name": umo,
                        "subscriptions": rows,
                    }
                )
                continue
            platform_id, message_type, session_id = parsed
            if message_type == "GroupMessage":
                group = groups_by_umo.setdefault(
                    umo,
                    {
                        "umo": umo,
                        "platform_id": platform_id,
                        "group_id": session_id,
                        "group_name": f"群聊 {session_id}",
                        "available": False,
                        "subscriptions": [],
                    },
                )
                group["subscriptions"] = rows
            else:
                other_sessions.append(
                    {
                        "umo": umo,
                        "platform_id": platform_id,
                        "session_id": session_id,
                        "session_type": message_type,
                        "session_name": self._session_label(message_type, session_id),
                        "subscriptions": rows,
                    }
                )

        groups = list(groups_by_umo.values())
        for group in groups:
            group["subscriptions"].sort(key=lambda row: row["uid"])
        for session in other_sessions:
            session["subscriptions"].sort(key=lambda row: row["uid"])
        groups.sort(
            key=lambda g: (g["group_name"].casefold(), g["group_id"])
        )
        other_sessions.sort(key=lambda s: s["session_name"].casefold())

        return {
            "polling": {
                "running": bool(getattr(self.plugin, "_running", False)),
                "interval_minutes": int(getattr(self.plugin, "poll_interval", 5)),
            },
            "cookie": {
                "configured": bool(getattr(self.plugin, "cookie", "")),
            },
            "totals": {
                "groups": len(groups),
                "sessions": len(session_subscriptions),
                "authors": len(subs),
                "subscriptions": total_relations,
                "active": active_relations,
            },
            "group_sources": [
                {
                    "platform_id": source["platform_id"],
                    "available": source["available"],
                    "error": source["error"],
                    "group_count": len(source["groups"]),
                }
                for source in group_sources
            ],
            "groups": groups,
            "other_sessions": other_sessions,
        }

    @staticmethod
    async def _payload() -> dict | None:
        payload = await request.json(default={})
        return payload if isinstance(payload, dict) else None

    async def _validate_live_group(
        self,
        parsed_umo: tuple[str, str, str],
    ) -> tuple[str, int] | None:
        platform_id, message_type, session_id = parsed_umo
        if message_type != "GroupMessage":
            return "只能为群聊新增订阅", 400
        platform = next(
            (
                item
                for item in self._aiocqhttp_platforms()
                if self._meta_value(item.meta(), "id") == platform_id
            ),
            None,
        )
        if platform is None:
            return "未找到对应的 aiocqhttp 平台实例", 400
        source = await self._fetch_platform_groups(platform)
        if not source["available"]:
            return "暂时无法确认机器人所在群列表", 503
        if not any(group["group_id"] == session_id for group in source["groups"]):
            return "机器人当前不在这个群聊中", 404
        return None

    # ---------- 接口 ----------

    async def overview(self):
        try:
            return json_response(await self._build_overview())
        except Exception as exc:
            logger.exception(f"生成微博订阅概览失败: {exc}")
            return error_response("读取订阅数据失败", status_code=500)

    async def save_poll_interval(self):
        payload = await self._payload()
        if payload is None:
            return error_response("请求内容必须是 JSON 对象", status_code=400)
        minutes = payload.get("minutes")
        if type(minutes) is not int or minutes < 1:
            return error_response("轮询间隔必须是不少于 1 的整数", status_code=400)
        try:
            await self.plugin._set_poll_interval(minutes)
        except Exception as exc:
            logger.error(f"保存微博轮询间隔失败: {exc}")
            return error_response("轮询间隔保存失败，原配置已恢复", status_code=500)
        return json_response({"saved": True, "minutes": minutes})

    async def save_cookie(self):
        payload = await self._payload()
        if payload is None:
            return error_response("请求内容必须是 JSON 对象", status_code=400)
        cookie = str(payload.get("cookie") or "").strip()
        if not cookie:
            return error_response("Cookie 不能为空", status_code=400)
        try:
            result = await self.plugin._set_cookie(cookie)
        except Exception as exc:
            logger.error(f"保存微博 Cookie 失败: {exc}")
            return error_response("Cookie 保存失败", status_code=500)
        if not result:
            return error_response("Cookie 无效（ok=-100），请重新登录微博获取", status_code=400)
        return json_response({"saved": True})

    async def add_subscription(self):
        payload = await self._payload()
        if payload is None:
            return error_response("请求内容必须是 JSON 对象", status_code=400)
        parsed_umo = self._parse_umo(payload.get("umo"))
        if parsed_umo is None:
            return error_response("会话 UMO 格式不正确", status_code=400)
        target = str(payload.get("uid") or "").strip()
        if not target:
            return error_response("请填写微博 UID 或主页链接", status_code=400)
        if extract_uid(target) is None and extract_name(target) is None:
            return error_response(
                "微博 UID、主页链接或个性域名格式不正确",
                status_code=400,
            )

        group_error = await self._validate_live_group(parsed_umo)
        if group_error:
            message, status_code = group_error
            return error_response(message, status_code=status_code)

        try:
            result = await self.plugin._add_subscription(
                payload["umo"],
                target,
            )
        except Exception as exc:
            logger.warning(f"WebUI 新增微博订阅 {target} 失败: {exc}")
            return error_response("获取微博用户资料或时间线失败", status_code=502)

        reason = result.get("reason")
        if reason == "duplicate":
            return error_response("当前群已订阅这个博主", status_code=409)
        if reason == "not_found":
            return error_response(
                "未找到这个博主（UID 或个性域名可能不正确）",
                status_code=404,
            )
        if reason == "bad_uid":
            return error_response(
                "无法识别的博主地址，请提供数字UID、主页链接或个性域名",
                status_code=400,
            )
        if reason == "cookie_invalid":
            return error_response("微博 Cookie 无效或未配置", status_code=503)
        return json_response(
            {
                "saved": True,
                "uid": result["uid"],
                "screen_name": result["screen_name"],
            }
        )

    async def update_subscription(self):
        payload = await self._payload()
        if payload is None:
            return error_response("请求内容必须是 JSON 对象", status_code=400)
        if self._parse_umo(payload.get("umo")) is None:
            return error_response("会话 UMO 格式不正确", status_code=400)
        uid = extract_uid(str(payload.get("uid") or ""))
        if uid is None:
            return error_response("微博 UID 格式不正确", status_code=400)

        changes = {}
        if "enabled" in payload:
            if type(payload["enabled"]) is not bool:
                return error_response("enabled 必须是布尔值", status_code=400)
            changes["status"] = payload["enabled"]
        if not changes:
            return error_response("没有可保存的订阅选项", status_code=400)

        result = await self.plugin._update_subscription(
            payload["umo"],
            uid,
            changes,
        )
        if not result["ok"]:
            return error_response("没有找到这条订阅", status_code=404)
        return json_response({"saved": True, "uid": result["uid"]})

    async def update_group_status(self):
        payload = await self._payload()
        if payload is None:
            return error_response("请求内容必须是 JSON 对象", status_code=400)
        parsed_umo = self._parse_umo(payload.get("umo"))
        if parsed_umo is None or parsed_umo[1] != "GroupMessage":
            return error_response("群聊 UMO 格式不正确", status_code=400)
        enabled = payload.get("enabled")
        if type(enabled) is not bool:
            return error_response("enabled 必须是布尔值", status_code=400)

        count = await self.plugin._set_session_status(
            payload["umo"],
            enabled,
        )
        if not count:
            return error_response("这个群当前没有订阅", status_code=404)
        return json_response({"saved": True, "updated": count})

    async def remove_subscription(self):
        payload = await self._payload()
        if payload is None:
            return error_response("请求内容必须是 JSON 对象", status_code=400)
        if self._parse_umo(payload.get("umo")) is None:
            return error_response("会话 UMO 格式不正确", status_code=400)
        uid = extract_uid(str(payload.get("uid") or ""))
        if uid is None:
            return error_response("微博 UID 格式不正确", status_code=400)

        result = await self.plugin._remove_subscription(payload["umo"], uid)
        if not result["ok"]:
            return error_response("没有找到这条订阅", status_code=404)
        return json_response({"saved": True, "uid": result["uid"]})
