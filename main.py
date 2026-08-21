"""
AstrBot 微博媒体推送插件

订阅微博博主，新微博自动推送。推送内容仅包含图片和视频，不含任何文字。
仿 Twitter 推文转发插件的媒体推送模式。

用法:
  /微博订阅 <uid或主页链接>     订阅博主
  /微博退订 <uid>               退订
  /微博列表                     查看订阅
  /微博推送 开启|关闭           开关当前会话的推送
  /微博测试 <uid>               立即推送该博主最新一条含媒体的微博
  /微博设置cookie <Cookie>     更新微博登录 Cookie (管理员)
  /微博清空订阅                 清空所有订阅 (管理员)
"""

import asyncio
import re

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from .delivery import MediaDeliveryService
from .weibo_api import WeiboAPI, WeiboAPIError, WeiboAuthError

KV_KEY = "weibo_media_subs_v1"
# 已推送微博ID历史（跨重新订阅保留，避免重复推送）
PUSHED_KEY = "weibo_media_pushed_v1"
PUSHED_MAX_KEEP = 500

UID_PATTERNS = [
    re.compile(r"weibo\.com/u/(\d+)"),
    re.compile(r"m\.weibo\.cn/u/(\d+)"),
    re.compile(r"weibo\.cn/(\d+)"),
    re.compile(r"^\s*(\d{6,})\s*$"),
]

NAME_PATTERNS = [
    re.compile(r"weibo\.com/(?!u/|n/|aj_|at/weibo)([A-Za-z0-9_\-\.]+)"),
    re.compile(r"^\s*([A-Za-z][A-Za-z0-9_\-\.]{2,})\s*$"),
]


def extract_uid(text: str) -> str | None:
    """从主页链接或纯数字中提取微博 UID。"""
    text = (text or "").strip()
    for pattern in UID_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1)
    return None


def extract_name(text: str) -> str | None:
    """从个性域名主页链接（如 weibo.com/AoiMomoko813）提取自定义名。"""
    text = (text or "").strip()
    for pattern in NAME_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1)
    return None


class WeiboMediaPushPlugin(Star):
    """微博媒体推送插件主类。"""

    def _cfg(self, key: str, default, *legacy_keys: str):
        """读取配置，兼容扁平结构与 _conf_schema 分组(basic)结构。"""
        # 扁平结构优先
        value = self.config.get(key)
        if value is not None:
            return value
        # 分组结构（_conf_schema.json 存在时 AstrBot 会存成 {"basic": {...}}）
        basic = self.config.get("basic", {})
        if isinstance(basic, dict):
            value = basic.get(key)
            if value is not None:
                return value
        for legacy in legacy_keys:
            value = self.config.get(legacy)
            if value is not None:
                return value
        return default

    @staticmethod
    def _parse_forward_windows(value) -> set[str]:
        """解析可编辑的合并转发窗口列表。"""
        if isinstance(value, (list, tuple, set)):
            parts = value
        else:
            parts = re.split(r"[,，\\n\\s]+", str(value or ""))
        result = set()
        for item in parts:
            item = str(item).strip()
            if item:
                result.add(item)
        return result

    def _set_cfg_value(self, key: str, value) -> None:
        """写入配置，跟随当前使用的存储结构。"""
        if "basic" in self.config and isinstance(self.config.get("basic"), dict):
            self.config["basic"][key] = value
        else:
            self.config[key] = value

    def __init__(
        self,
        context: Context,
        config: AstrBotConfig | None = None,
    ):
        super().__init__(context)
        # 新插件尚未生成配置文件时，AstrBot 不会传入 config，这里兜底
        self.config = config if config is not None else {}

        self.cookie = str(self._cfg("weibo_cookie", "") or "").strip()
        self.poll_interval = max(
            1, int(self._cfg("poll_interval_minutes", 5))
        )
        self.include_retweets = bool(
            self._cfg("include_retweets", False)
        )
        self.max_video_size_mb = max(
            1, int(self._cfg("max_video_size_mb", 200))
        )
        self.remove_watermark = bool(
            self._cfg("remove_watermark", True)
        )
        try:
            self.watermark_crop_bottom = max(
                0.0, min(0.5, float(self._cfg("watermark_crop_bottom", 0.10)))
            )
        except (TypeError, ValueError):
            self.watermark_crop_bottom = 0.10
        self.push_recent_hours = max(
            0, int(self._cfg("push_recent_hours", 24))
        )
        self.image_resolution = str(
            self._cfg("image_resolution", "high") or "high"
        ).strip()
        self.video_resolution = str(
            self._cfg("video_resolution", "720p") or "720p"
        ).strip()
        self.forward_windows = self._parse_forward_windows(
            self._cfg("forward_windows", "634179473")
        )

        self.api = WeiboAPI(
            self.cookie,
            image_resolution=self.image_resolution,
            video_resolution=self.video_resolution,
        )
        self.delivery = MediaDeliveryService(
            context,
            self.max_video_size_mb,
            self.remove_watermark,
            self.watermark_crop_bottom,
            self.forward_windows,
        )

        self._poll_task: asyncio.Task | None = None
        self._running = False
        self._poll_wakeup = asyncio.Event()
        # 后台任务（追更等），持有引用防止被 GC
        self._bg_tasks: list[asyncio.Task] = []

        # 注册 Plugin Page 网页管理界面
        self._webui_controller = None
        register_web_api = getattr(context, "register_web_api", None)
        if callable(register_web_api):
            try:
                from .weibo_webui import WeiboWebUIController

                self._webui_controller = WeiboWebUIController(self, context)
            except Exception as exc:
                logger.warning(f"微博订阅管理 WebUI 注册失败: {exc}")
        else:
            logger.info("当前 AstrBot 版本不支持 Plugin Pages，跳过订阅管理 WebUI")

    # ---------- 生命周期 ----------

    async def initialize(self):
        logger.info("微博媒体推送插件初始化中...")
        if not self.cookie:
            logger.warning(
                "未配置微博 Cookie，推送功能不可用。"
                "请用 /微博设置cookie <Cookie> 配置"
            )
        # 启动时清理上次残留的图片和视频，服务器不保留媒体
        self.delivery._cleanup_old_files(0)
        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info(
            f"微博媒体推送已启动，轮询间隔 {self.poll_interval} 分钟"
        )

    async def terminate(self):
        self._running = False
        self._poll_wakeup.set()
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        for task in self._bg_tasks:
            task.cancel()
        self._bg_tasks.clear()
        logger.info("微博媒体推送插件已停止")

    # ---------- 轮询 ----------

    async def _poll_loop(self):
        while self._running:
            try:
                await self._check_all_subscriptions()
                self.delivery._cleanup_old_files()
            except Exception as exc:
                logger.error(f"微博轮询出错: {exc}")
            self._poll_wakeup.clear()
            try:
                await asyncio.wait_for(
                    self._poll_wakeup.wait(),
                    timeout=self.poll_interval * 60,
                )
            except TimeoutError:
                pass

    async def _check_all_subscriptions(self):
        if not self.cookie:
            return
        subs = await self.get_kv_data(KV_KEY, {}) or {}
        for umo, entries in list(subs.items()):
            for uid, info in list(entries.items()):
                if not info.get("status", True):
                    continue
                try:
                    posts = await self.api.get_user_timeline(uid)
                except WeiboAuthError as exc:
                    logger.warning(
                        f"微博Cookie失效，跳过 {uid}: {exc}"
                    )
                    continue
                except Exception as exc:
                    logger.warning(f"获取 {uid} 时间线失败: {exc}")
                    continue

                last_id = info.get("last_id")
                new_posts = []
                for post in posts:
                    try:
                        post_id = int(post["id"])
                    except (TypeError, ValueError):
                        continue
                    if last_id and post_id <= int(last_id):
                        continue
                    if not post["pics"] and not post["video_url"]:
                        continue
                    if post["is_retweet"] and not self.include_retweets:
                        continue
                    new_posts.append(post)

                if new_posts:
                    new_posts.sort(key=lambda p: int(p["id"]))
                    pushed_new_ids = []
                    for post in new_posts:
                        try:
                            sent = await self.delivery.send_post_media(
                                umo, post
                            )
                            if sent:
                                logger.info(
                                    f"已推送 {uid} 微博 {post['id']} 媒体"
                                )
                                info["pushed_count"] = (
                                    int(info.get("pushed_count", 0)) + 1
                                )
                                pushed_new_ids.append(post["id"])
                        except Exception as exc:
                            logger.error(
                                f"推送 {uid} 微博 {post['id']} 失败: {exc}"
                            )
                    info["last_id"] = str(
                        max(int(p["id"]) for p in new_posts)
                    )
                    await self.put_kv_data(KV_KEY, subs)
                    if pushed_new_ids:
                        await self._record_pushed_ids(umo, uid, pushed_new_ids)

    # ---------- 工具 ----------

    async def _save_config(self) -> None:
        try:
            save_config = getattr(self.config, "save_config", None)
            if not callable(save_config):
                raise RuntimeError("当前配置对象不支持持久化")
            result = save_config()
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:
            logger.warning(f"配置持久化失败: {exc}")

    def _cookie_ready(self) -> bool:
        if self.cookie:
            return True
        logger.warning("微博 Cookie 未配置")
        return False

    async def _get_pushed_ids(self, umo: str, uid: str) -> set[str]:
        """读取该会话该博主已推送过的微博ID集合。"""
        data = await self.get_kv_data(PUSHED_KEY, {}) or {}
        return set(data.get(umo, {}).get(uid, []))

    async def _record_pushed_ids(
        self,
        umo: str,
        uid: str,
        ids: list[str],
    ) -> None:
        """记录已推送的微博ID（跨重新订阅保留，最多保留最近 PUSHED_MAX_KEEP 条）。"""
        if not ids:
            return
        data = await self.get_kv_data(PUSHED_KEY, {}) or {}
        entries = data.setdefault(umo, {}).setdefault(uid, [])
        existing = set(entries)
        for post_id in ids:
            if post_id not in existing:
                entries.append(post_id)
                existing.add(post_id)
        if len(entries) > PUSHED_MAX_KEEP:
            data[umo][uid] = entries[-PUSHED_MAX_KEEP:]
        await self.put_kv_data(PUSHED_KEY, data)

    async def _resolve_target(self, target: str) -> str | None:
        """把数字UID、个性域名主页链接解析成数字 UID。"""
        uid = extract_uid(target)
        if uid:
            return uid
        name = extract_name(target)
        if not name:
            return None
        return await self.api.resolve_uid_by_name(name)

    async def _fetch_latest_media_post(self, uid: str) -> dict | None:
        """获取该博主最新一条含媒体的微博。"""
        posts = await self.api.get_user_timeline(uid)
        for post in posts:
            if not post["pics"] and not post["video_url"]:
                continue
            if post["is_retweet"] and not self.include_retweets:
                continue
            return post
        return None

    # ---------- WebUI 委托 ----------

    async def _get_subs_data(self) -> dict:
        return await self.get_kv_data(KV_KEY, {}) or {}

    async def _fetch_recent_media_posts(
        self,
        uid: str,
        hours: int,
        max_pages: int = 3,
    ) -> list[dict]:
        """拉取最近 hours 小时内含媒体的微博（最多翻 max_pages 页）。"""
        import time as _time

        cutoff = _time.time() - max(1, hours) * 3600
        recent: list[dict] = []
        for page in range(1, max_pages + 1):
            try:
                posts = await self.api.get_user_timeline(uid, page=page)
            except Exception as exc:
                logger.warning(f"获取 {uid} 第{page}页时间线失败: {exc}")
                break
            if not posts:
                break
            for post in posts:
                ts = self.api.parse_created_at(post.get("created_at"))
                if ts is None:
                    continue
                if ts < cutoff:
                    continue
                if not post["pics"] and not post["video_url"]:
                    continue
                if post["is_retweet"] and not self.include_retweets:
                    continue
                recent.append(post)
            oldest_ts = self.api.parse_created_at(posts[-1].get("created_at"))
            if oldest_ts is not None and oldest_ts < cutoff:
                break
        return recent

    async def _add_subscription(self, umo: str, target: str) -> dict:
        """WebUI/命令共用：添加订阅（支持UID/个性域名），并追更最近 N 小时。"""
        if not self.cookie:
            return {"reason": "cookie_invalid"}
        uid = await self._resolve_target(target)
        if not uid:
            return {"reason": "bad_uid"}
        try:
            user = await self.api.get_user_info(uid)
        except WeiboAuthError:
            return {"reason": "cookie_invalid"}
        except Exception as exc:
            logger.warning(f"获取微博用户 {uid} 信息失败: {exc}")
            return {"reason": "fetch_failed"}
        if not user:
            return {"reason": "not_found"}

        try:
            posts = await self.api.get_user_timeline(uid)
        except Exception as exc:
            logger.warning(f"获取微博用户 {uid} 时间线失败: {exc}")
            return {"reason": "fetch_failed"}

        last_id = "0"
        if posts:
            last_id = str(max(int(p["id"]) for p in posts))

        subs = await self.get_kv_data(KV_KEY, {}) or {}
        entries = subs.setdefault(umo, {})
        if uid in entries:
            return {
                "reason": "duplicate",
                "uid": uid,
                "screen_name": user["screen_name"],
            }
        # 从历史记录恢复累计推送数（重新订阅不丢计数）
        history_count = len(await self._get_pushed_ids(umo, uid))
        entries[uid] = {
            "screen_name": user["screen_name"],
            "last_id": last_id,
            "status": True,
            "pushed_count": history_count,
        }
        await self.put_kv_data(KV_KEY, subs)

        # 追更最近 N 小时内的媒体微博：放入后台任务，
        # 避免 WebUI 请求/命令被长时间阻塞（下载图片很慢）
        if self.push_recent_hours > 0:
            task = asyncio.create_task(self._catch_up_push(umo, uid))
            self._bg_tasks.append(task)
            task.add_done_callback(
                lambda t: self._bg_tasks.remove(t)
                if t in self._bg_tasks
                else None
            )

        return {
            "ok": True,
            "uid": uid,
            "screen_name": user["screen_name"],
            "followers_count": user["followers_count"],
        }

    async def _catch_up_push(self, umo: str, uid: str) -> None:
        """后台追更最近 N 小时的媒体微博（跳过已推送过的，避免重复）。"""
        try:
            recent = await self._fetch_recent_media_posts(
                uid, self.push_recent_hours
            )
            recent.sort(key=lambda p: int(p["id"]))
            already_pushed = await self._get_pushed_ids(umo, uid)
            pushed = 0
            new_ids = []
            for post in recent:
                if post["id"] in already_pushed:
                    # 之前已推送过（含轮询推送、历史追更），重新订阅不重复发
                    continue
                # 若已退订则停止
                subs = await self.get_kv_data(KV_KEY, {}) or {}
                if uid not in subs.get(umo, {}):
                    logger.info(f"追更中断：{uid} 已退订")
                    break
                try:
                    if await self.delivery.send_post_media(umo, post):
                        pushed += 1
                        new_ids.append(post["id"])
                        already_pushed.add(post["id"])
                        logger.info(f"追更推送 {uid} 微博 {post['id']}")
                except Exception as exc:
                    logger.error(
                        f"追更推送 {uid} 微博 {post['id']} 失败: {exc}"
                    )
            if new_ids:
                await self._record_pushed_ids(umo, uid, new_ids)
            if pushed:
                subs = await self.get_kv_data(KV_KEY, {}) or {}
                info = subs.get(umo, {}).get(uid)
                if info is not None:
                    info["pushed_count"] = (
                        int(info.get("pushed_count", 0)) + pushed
                    )
                    await self.put_kv_data(KV_KEY, subs)
            logger.info(f"追更完成：{uid} 共推送 {pushed} 条")
        except Exception as exc:
            logger.error(f"追更任务失败 {uid}: {exc}")

    async def _update_subscription(self, umo: str, uid: str, changes: dict) -> dict:
        subs = await self.get_kv_data(KV_KEY, {}) or {}
        entries = subs.get(umo, {})
        if uid not in entries:
            return {"ok": False, "uid": uid}
        info = entries[uid]
        for key, value in changes.items():
            info[key] = value
        await self.put_kv_data(KV_KEY, subs)
        return {"ok": True, "uid": uid}

    async def _remove_subscription(self, umo: str, uid: str) -> dict:
        subs = await self.get_kv_data(KV_KEY, {}) or {}
        entries = subs.get(umo, {})
        if uid not in entries:
            return {"ok": False, "uid": uid}
        del entries[uid]
        if not entries:
            subs.pop(umo, None)
        await self.put_kv_data(KV_KEY, subs)
        return {"ok": True, "uid": uid}

    async def _set_session_status(self, umo: str, enabled: bool) -> int:
        subs = await self.get_kv_data(KV_KEY, {}) or {}
        entries = subs.get(umo, {})
        for info in entries.values():
            info["status"] = enabled
        await self.put_kv_data(KV_KEY, subs)
        return len(entries)

    async def _set_poll_interval(self, minutes: int) -> None:
        self._set_cfg_value("poll_interval_minutes", minutes)
        await self._save_config()
        self.poll_interval = minutes
        self._poll_wakeup.set()

    async def _set_cookie(self, cookie: str) -> bool:
        """更新 Cookie 并验证；验证通过返回 True。"""
        self._set_cfg_value("weibo_cookie", cookie)
        await self._save_config()
        self.cookie = cookie
        self.api.update_cookie(cookie)
        try:
            user = await self.api.get_user_info("2803301701")
            return bool(user)
        except WeiboAuthError:
            return False
        except Exception:
            return False

    # ---------- 命令 ----------

    @filter.command("微博订阅", alias={"weibo_follow"})
    async def subscribe(
        self,
        event: AstrMessageEvent,
        target: str = "",
    ):
        """订阅博主，用法: /微博订阅 <uid或主页链接或个性域名>"""
        if not self._cookie_ready():
            yield event.plain_result(
                "请先配置微博 Cookie: /微博设置cookie <Cookie>"
            )
            return
        if not target:
            yield event.plain_result(
                "请提供博主UID、主页链接或个性域名，"
                "用法: /微博订阅 <uid或主页链接>"
            )
            return

        result = await self._add_subscription(event.unified_msg_origin, target)
        reason = result.get("reason")
        if reason == "cookie_invalid":
            yield event.plain_result("微博Cookie失效或未配置，请先 /微博设置cookie")
            return
        if reason == "bad_uid":
            yield event.plain_result(
                "无法识别的博主地址，请提供数字UID、主页链接或个性域名，"
                "例如 /微博订阅 2803301701 或 /微博订阅 weibo.com/AoiMomoko813"
            )
            return
        if reason == "not_found":
            yield event.plain_result(
                f"未找到博主（{target[:40]}）对应的微博账号"
            )
            return
        if reason == "fetch_failed":
            yield event.plain_result("获取微博数据失败，请稍后重试")
            return
        if reason == "duplicate":
            yield event.plain_result(
                f"已订阅过 {result.get('screen_name', '')} "
                f"({result.get('uid', '')})"
            )
            return

        hours = self.push_recent_hours if self.push_recent_hours > 0 else 0
        recent_note = (
            f"正在后台追更最近{hours}小时内的媒体微博，"
            "请稍候（数量可在订阅管理页面查看）"
            if hours > 0
            else "从下次发新微博开始推送"
        )
        yield event.plain_result(
            f"订阅成功!\n"
            f"UID: {result['uid']}\n"
            f"昵称: {result['screen_name']}\n"
            f"粉丝数: {result['followers_count']}\n"
            f"{recent_note}\n"
            f"说明: 仅推送包含图片或视频的微博，不含文字"
        )

    @filter.command("微博退订", alias={"weibo_unfollow"})
    async def unsubscribe(
        self,
        event: AstrMessageEvent,
        target: str = "",
    ):
        """退订博主，用法: /微博退订 <uid>"""
        if not target:
            yield event.plain_result("请提供博主UID，用法: /微博退订 <uid>")
            return
        uid = extract_uid(target)
        if not uid:
            yield event.plain_result("无法识别UID")
            return

        umo = event.unified_msg_origin
        subs = await self.get_kv_data(KV_KEY, {}) or {}
        entries = subs.get(umo, {})
        if uid not in entries:
            yield event.plain_result(f"当前会话未订阅 {uid}")
            return
        del entries[uid]
        if not entries:
            subs.pop(umo, None)
        await self.put_kv_data(KV_KEY, subs)
        yield event.plain_result(f"已退订 {uid}")

    @filter.command("微博列表", alias={"weibo_list"})
    async def list_subscriptions(self, event: AstrMessageEvent):
        """查看当前会话的订阅列表。"""
        umo = event.unified_msg_origin
        subs = await self.get_kv_data(KV_KEY, {}) or {}
        entries = subs.get(umo, {})
        if not entries:
            yield event.plain_result("当前没有订阅任何博主")
            return

        lines = []
        for uid, info in entries.items():
            status = "🟢" if info.get("status", True) else "🔴"
            name = info.get("screen_name", "未知")
            pushed = int(info.get("pushed_count", 0))
            lines.append(f"{status} {uid} ({name}) - 已推送 {pushed} 条")
        yield event.plain_result(
            "当前微博订阅列表:\n" + "\n".join(
                f"{i}. {line}" for i, line in enumerate(lines, 1)
            )
        )

    @filter.command("微博推送", alias={"weibo_push"})
    async def toggle_push(
        self,
        event: AstrMessageEvent,
        action: str = "",
    ):
        """开启或关闭当前会话的推送。"""
        if action not in ("开启", "关闭"):
            yield event.plain_result("用法: /微博推送 开启 或 /微博推送 关闭")
            return
        enabled = action == "开启"
        umo = event.unified_msg_origin
        subs = await self.get_kv_data(KV_KEY, {}) or {}
        entries = subs.get(umo, {})
        if not entries:
            yield event.plain_result("当前没有订阅任何博主")
            return
        for info in entries.values():
            info["status"] = enabled
        await self.put_kv_data(KV_KEY, subs)
        yield event.plain_result(
            f"微博推送已{'开启' if enabled else '关闭'} "
            f"(影响 {len(entries)} 个订阅)"
        )

    @filter.command("微博测试", alias={"weibo_test"})
    async def test_push(
        self,
        event: AstrMessageEvent,
        target: str = "",
    ):
        """立即推送该博主最新一条含媒体的微博。"""
        if not self._cookie_ready():
            yield event.plain_result(
                "请先配置微博 Cookie: /微博设置cookie <Cookie>"
            )
            return
        if not target:
            yield event.plain_result(
                "请提供博主UID或主页链接，用法: /微博测试 <uid>"
            )
            return
        uid = await self._resolve_target(target)
        if not uid:
            yield event.plain_result(
                "无法识别的博主地址，请提供数字UID、主页链接或个性域名"
            )
            return

        yield event.plain_result(f"正在获取 {uid} 的最新微博，请稍候...")
        try:
            post = await self._fetch_latest_media_post(uid)
        except WeiboAuthError as exc:
            yield event.plain_result(f"微博Cookie失效: {exc}")
            return
        except Exception as exc:
            yield event.plain_result(f"获取失败: {exc}")
            return

        if not post:
            yield event.plain_result(f"未找到 {uid} 的最新含媒体微博")
            return

        sent = await self.delivery.send_post_media(
            event.unified_msg_origin, post
        )
        if not sent:
            yield event.plain_result(
                "该微博没有可发送的媒体（可能只有文字，或下载失败）"
            )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("微博设置cookie", alias={"weibo_set_cookie"})
    async def set_cookie(self, event: AstrMessageEvent):
        """更新微博登录 Cookie。"""
        rest = (event.message_str or "").strip()
        tokens = rest.split(None, 1)
        cookie = tokens[1].strip().strip("\"'") if len(tokens) > 1 else ""
        if not cookie:
            yield event.plain_result(
                "用法: /微博设置cookie <完整Cookie>\n"
                "获取方法: 电脑浏览器登录 weibo.com，F12 → Network → "
                "刷新页面 → 任选请求 → 复制 Request Headers 里的 Cookie"
            )
            return

        self._set_cfg_value("weibo_cookie", cookie)
        await self._save_config()
        self.cookie = cookie
        self.api.update_cookie(cookie)

        # 立即验证
        try:
            user = await self.api.get_user_info("2803301701")
            if user:
                yield event.plain_result(
                    "Cookie 已保存并验证通过 ✅\n"
                    f"(测试访问: {user['screen_name']})"
                )
            else:
                yield event.plain_result("Cookie 已保存，但验证未返回用户信息")
        except WeiboAuthError:
            yield event.plain_result(
                "Cookie 已保存但无效（ok=-100，需要重新登录微博获取）"
            )
        except Exception as exc:
            yield event.plain_result(f"Cookie 已保存，验证时出错: {exc}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("微博清空订阅", alias={"weibo_clear_all"})
    async def clear_all(self, event: AstrMessageEvent):
        """清空所有会话的全部微博订阅。"""
        subs = await self.get_kv_data(KV_KEY, {}) or {}
        total = sum(len(v) for v in subs.values())
        await self.put_kv_data(KV_KEY, {})
        yield event.plain_result(f"已清空全部订阅: 共 {total} 个")
