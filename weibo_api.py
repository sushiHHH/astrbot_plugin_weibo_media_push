"""微博移动端 API 客户端（需 Cookie，仅取公开用户的时间线）。"""

import re
import time
import urllib.parse

import aiohttp

from astrbot.api import logger


class WeiboAuthError(Exception):
    """Cookie 无效或未登录。"""


class WeiboAPIError(Exception):
    """微博接口请求失败。"""


class WeiboAPI:
    """基于 m.weibo.cn 的微博数据源，返回结构化的微博列表。"""

    BASE = "https://m.weibo.cn"

    # 图片分辨率选项 -> sinaimg 尺寸前缀
    IMAGE_SIZES = {
        "original": "large",
        "high": "mw2000",
        "medium": "mw1024",
        "low": "mw690",
    }

    def __init__(
        self,
        cookie: str = "",
        timeout: float = 15.0,
        image_resolution: str = "high",
        video_resolution: str = "720p",
    ):
        self.timeout = timeout
        self.image_resolution = (
            image_resolution
            if image_resolution in self.IMAGE_SIZES
            else "high"
        )
        self.video_resolution = (
            video_resolution
            if video_resolution in ("1080p", "720p", "standard", "360p")
            else "720p"
        )
        self.update_cookie(cookie)

    def update_cookie(self, cookie: str) -> None:
        self.cookie = str(cookie or "").strip()
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 "
                "Mobile/15E148 Safari/604.1"
            ),
            "Referer": f"{self.BASE}/",
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
        }
        if self.cookie:
            self.headers["Cookie"] = self.cookie

    async def _get_json(self, url: str) -> dict:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    headers=self.headers,
                    timeout=aiohttp.ClientTimeout(total=self.timeout),
                ) as resp:
                    if resp.status != 200:
                        raise WeiboAPIError(
                            f"微博接口 HTTP {resp.status}: {url}"
                        )
                    return await resp.json()
        except WeiboAPIError:
            raise
        except Exception as exc:
            raise WeiboAPIError(f"请求微博失败: {exc}") from exc

    @staticmethod
    def _check_ok(data: dict) -> None:
        ok = data.get("ok")
        if ok == -100:
            raise WeiboAuthError("微博 Cookie 失效或未登录 (ok=-100)")
        if ok != 1:
            raise WeiboAPIError(f"微博接口返回异常 ok={ok}")

    async def get_user_info(self, uid: str) -> dict | None:
        """获取用户昵称等公开信息。"""
        url = (
            f"{self.BASE}/api/container/getIndex"
            f"?type=uid&value={uid}&containerid=100505{uid}"
        )
        data = await self._get_json(url)
        self._check_ok(data)
        user = (data.get("data") or {}).get("userInfo") or {}
        if not user:
            return None
        return {
            "uid": str(uid),
            "screen_name": str(user.get("screen_name") or ""),
            "followers_count": user.get("followers_count", 0),
        }

    async def resolve_uid_by_name(self, name: str) -> str | None:
        """通过个性域名/自定义名解析数字 UID（weibo.com ajax 接口）。"""
        url = (
            "https://weibo.com/ajax/profile/info"
            f"?custom={urllib.parse.quote(str(name).strip())}"
        )
        headers = dict(self.headers)
        headers["User-Agent"] = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        headers["Referer"] = "https://weibo.com/"
        headers.pop("X-Requested-With", None)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=self.timeout),
                ) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
        except Exception as exc:
            logger.warning(f"解析微博个性域名失败 {name}: {exc}")
            return None
        if data.get("ok") != 1:
            return None
        user = (data.get("data") or {}).get("user") or {}
        uid = user.get("idstr") or user.get("id")
        return str(uid) if uid else None

    async def get_long_text(self, post_id: str) -> str | None:
        """获取长微博/文章的完整正文。"""
        url = f"{self.BASE}/statuses/extend?id={urllib.parse.quote(str(post_id))}"
        try:
            data = await self._get_json(url)
            self._check_ok(data)
            raw = (data.get("data") or {}).get("longTextContent")
            if not raw:
                raw = (data.get("data") or {}).get("text")
            if not raw:
                return None
            return self._clean_text(str(raw))
        except Exception as exc:
            logger.warning(f"获取微博 {post_id} 全文失败: {exc}")
            return None

    async def get_user_timeline(self, uid: str, page: int = 1) -> list[dict]:
        """获取用户微博列表，返回标准化后的微博列表。"""
        url = (
            f"{self.BASE}/api/container/getIndex"
            f"?type=uid&value={uid}&containerid=107603{uid}&page={page}"
        )
        data = await self._get_json(url)
        self._check_ok(data)
        cards = (data.get("data") or {}).get("cards") or []
        posts = []
        for card in cards:
            if card.get("card_type") != 9:
                continue
            mblog = card.get("mblog") or {}
            if not mblog:
                continue
            post = self._parse_mblog(mblog)
            if post:
                posts.append(post)
        return posts

    @staticmethod
    def _clean_text(html: str) -> str:
        text = re.sub(r"<[^>]+>", "", html or "")
        return text.replace("&nbsp;", " ").strip()

    def _build_image_url(self, pic: dict, mblog: dict) -> str | None:
        """按配置的分辨率构建图片 URL。"""
        size = self.IMAGE_SIZES.get(self.image_resolution, "mw2000")

        # 当前图片的 large URL 是可靠的尺寸/图床来源。原图模式不能优先
        # 使用 pic.url：该字段在转发微博中经常只是缩略图，直接发送会很糊。
        large_url = str((pic.get("large") or {}).get("url") or "")
        pid = str(pic.get("pid") or "")

        # 微博图床支持按 pid 重新拼尺寸；保留接口返回的图床子域名，兼容
        # wx、tva、tvax 等域名，而不是只匹配 wx 数字域名。
        if pid and large_url.startswith("http"):
            match = re.match(r"(https://[^/]+\.sinaimg\.cn)/", large_url)
            if match:
                return f"{match.group(1)}/{size}/{pid}.jpg"

        if self.image_resolution == "original":
            # 接口明确给出的当前图片原图字段才作为回退，绝不把缩略图 url
            # 当成原图；没有时 large_url 仍比 pic.url 清晰。
            for key in ("original", "original_url"):
                value = pic.get(key)
                if value and str(value).startswith("http"):
                    return str(value)
            return large_url or None

        if self.image_resolution == "high" and large_url:
            return large_url
        fallback = pic.get("url") or pic.get("original")
        return fallback if fallback and str(fallback).startswith("http") else None

    def _build_video_url(self, media_info: dict) -> str | None:
        """按配置的清晰度选择视频 URL。

        实际接口 media_info 常见字段：
          stream_url / stream_url_hd（直链 mp4，带 label/template 签名参数）、
          mp4_1080p_mp4 / mp4_720p_mp4 / mp4_hd_url / mp4_url / url。
        """
        resolution = self.video_resolution
        if resolution == "1080p":
            video_url = (
                media_info.get("mp4_1080p_mp4")
                or media_info.get("mp4_hd_url")
                or media_info.get("stream_url_hd")
                or media_info.get("mp4_720p_mp4")
                or media_info.get("stream_url")
                or media_info.get("mp4_url")
                or media_info.get("url")
            )
        elif resolution == "720p":
            video_url = (
                media_info.get("stream_url_hd")
                or media_info.get("mp4_hd_url")
                or media_info.get("mp4_720p_mp4")
                or media_info.get("stream_url")
                or media_info.get("mp4_1080p_mp4")
                or media_info.get("mp4_url")
                or media_info.get("url")
            )
        elif resolution == "standard":  # 标清
            video_url = (
                media_info.get("stream_url")
                or media_info.get("mp4_url")
                or media_info.get("url")
                or media_info.get("stream_url_hd")
                or media_info.get("mp4_hd_url")
            )
        else:  # 360p：取最低可用源
            video_url = (
                media_info.get("mp4_360p_mp4")
                or media_info.get("stream_url")
                or media_info.get("mp4_url")
                or media_info.get("url")
                or media_info.get("stream_url_hd")
            )
        if video_url and ".m3u8" in str(video_url).lower():
            return None
        return str(video_url) if video_url else None

    def _parse_mblog(self, mblog: dict) -> dict | None:
        mid = str(mblog.get("id") or "")
        if not mid:
            return None

        # 转发微博的媒体往往在 retweeted_status；外层没有媒体时继承它。
        media_blog = mblog
        retweeted = mblog.get("retweeted_status") or {}
        if not (mblog.get("pics") or mblog.get("page_info")) and isinstance(retweeted, dict):
            media_blog = retweeted

        pics = []
        seen_pic_urls = set()
        for pic in media_blog.get("pics") or []:
            if not isinstance(pic, dict):
                continue
            url = self._build_image_url(pic, media_blog)
            if url and url not in seen_pic_urls:
                pics.append(url)
                seen_pic_urls.add(url)
        # 兼容部分接口只有 original_pic 的情况。
        if not pics and media_blog.get("original_pic"):
            original = str(media_blog["original_pic"])
            if original.startswith("http"):
                pics.append(original)

        video_url = None
        page_info = media_blog.get("page_info") or {}
        if page_info.get("type") == "video":
            video_url = self._build_video_url(
                page_info.get("media_info") or {}
            )

        return {
            "id": mid,
            "created_at": str(mblog.get("created_at") or ""),
            "text": self._clean_text(mblog.get("text")),
            "retweeted_text": self._clean_text(retweeted.get("text")) if isinstance(retweeted, dict) else "",
            "is_long_text": bool(mblog.get("isLongText") or mblog.get("is_long_text")),
            "screen_name": str((mblog.get("user") or {}).get("screen_name") or "微博用户"),
            "pics": pics,
            "video_url": str(video_url) if video_url else None,
            "is_retweet": bool(mblog.get("retweeted_status")),
        }

    @staticmethod
    def parse_created_at(s: str, now: float | None = None) -> float | None:
        """解析微博 created_at 为 Unix 时间戳；无法解析返回 None。"""
        now = now if now is not None else time.time()
        s = str(s or "").strip()
        if not s:
            return None
        if s == "刚刚":
            return now

        # RFC2822 风格: "Thu Aug 20 23:17:52 +0800 2026"
        try:
            from datetime import datetime

            dt = datetime.strptime(s, "%a %b %d %H:%M:%S %z %Y")
            return dt.timestamp()
        except ValueError:
            pass

        match = re.match(r"(\d+)分钟前", s)
        if match:
            return now - int(match.group(1)) * 60
        match = re.match(r"(\d+)小时前", s)
        if match:
            return now - int(match.group(1)) * 3600
        match = re.match(r"(\d+)天前", s)
        if match:
            return now - int(match.group(1)) * 86400

        lt = time.localtime(now)
        year, month, day = lt.tm_year, lt.tm_mon, lt.tm_mday

        def mktime(y, mo, d, hh=0, mm=0):
            return time.mktime((y, mo, d, hh, mm, 0, 0, 0, -1))

        if s.startswith("今天"):
            match = re.search(r"(\d{1,2}):(\d{2})", s)
            if match:
                return mktime(
                    year, month, day,
                    int(match.group(1)), int(match.group(2)),
                )
        if s.startswith("昨天"):
            from datetime import date, timedelta

            y, mo, d = (date.today() - timedelta(days=1)).timetuple()[:3]
            match = re.search(r"(\d{1,2}):(\d{2})", s)
            if match:
                return mktime(
                    y, mo, d,
                    int(match.group(1)), int(match.group(2)),
                )
            return mktime(y, mo, d)

        match = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
        if match:
            return mktime(
                int(match.group(1)), int(match.group(2)), int(match.group(3))
            )
        match = re.match(r"(\d{1,2})-(\d{1,2})", s)
        if match:
            return mktime(year, int(match.group(1)), int(match.group(2)))
        return None
