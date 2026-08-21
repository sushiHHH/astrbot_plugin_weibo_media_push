"""微博媒体下载与发送：仅发送图片和视频，不发送任何文字。"""

import os
import time

import aiohttp

from astrbot.api import logger
from astrbot.api.event import MessageChain
import astrbot.api.message_components as Comp
from astrbot.api.message_components import Node, Nodes

DOWNLOAD_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 "
        "Mobile/15E148 Safari/604.1"
    ),
    "Referer": "https://m.weibo.cn/",
}


class MediaDeliveryService:
    """下载微博媒体并逐条推送，严格不携带文字。"""

    def __init__(
        self,
        context,
        max_video_size_mb: int = 200,
        remove_watermark: bool = True,
        watermark_crop_bottom: float = 0.10,
        forward_windows: set[str] | None = None,
    ):
        self.context = context
        self.forward_windows = set(forward_windows or ())
        self.max_video_size_mb = max_video_size_mb
        self.remove_watermark = remove_watermark
        self.watermark_crop_bottom = watermark_crop_bottom
        # 图片临时目录（容器内，图片以 base64 发送，无需宿主机可见）
        self.tmp_dir = "/tmp/astrbot_weibo_media"
        os.makedirs(self.tmp_dir, exist_ok=True)
        # 视频临时目录：放在插件目录下（/AstrBot/data 是挂载卷，宿主机可见），
        # 视频以本地路径发送给 NapCat，必须让宿主机能访问到
        self.video_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "media_tmp"
        )
        os.makedirs(self.video_dir, exist_ok=True)

    def _container_to_host_path(self, path: str) -> str:
        """把容器内路径映射为宿主机路径（通过 /proc/self/mountinfo 推断 bind mount）。"""
        try:
            best_mount = None  # (挂载点, 根路径)
            with open(
                "/proc/self/mountinfo",
                "r",
                encoding="utf-8",
                errors="ignore",
            ) as f:
                for line in f:
                    parts = line.split()
                    # 格式: ID PARENT MAJ:MIN ROOT MOUNTPOINT OPTIONS [- FSTYPE SOURCE ...]
                    if len(parts) < 6:
                        continue
                    mount_point = parts[4].replace("\\040", " ")
                    root = parts[3].replace("\\040", " ")
                    if mount_point == "/":
                        continue
                    if path.startswith(mount_point):
                        if best_mount is None or len(mount_point) > len(
                            best_mount[0]
                        ):
                            best_mount = (mount_point, root)
            if best_mount:
                mount_point, root = best_mount
                host_path = root + path[len(mount_point):]
                if host_path != path:
                    logger.debug(
                        f"路径映射 {path} -> {host_path}"
                    )
                    return host_path
        except Exception as exc:
            logger.warning(f"路径映射失败，使用容器内路径: {exc}")
        return path

    def _cleanup_old_files(self, max_age_seconds: int = 3600) -> None:
        """清理残留的临时媒体文件（默认保留不超过1小时）。"""
        for directory in (self.tmp_dir, self.video_dir):
            try:
                now = time.time()
                for name in os.listdir(directory):
                    path = os.path.join(directory, name)
                    try:
                        # 微博媒体不应长期落盘；启动清理传入 0 时全部删除。
                        if max_age_seconds <= 0 or now - os.path.getmtime(path) > max_age_seconds:
                            os.remove(path)
                    except OSError:
                        pass
            except OSError as exc:
                logger.warning(f"清理微博媒体临时文件失败: {exc}")

    def _maybe_crop_watermark(self, path: str, ext: str) -> None:
        """裁剪图片底部水印区域（微博水印位于图片底部边缘）。"""
        if not self.remove_watermark or self.watermark_crop_bottom <= 0:
            return
        if ext.lower() not in (".jpg", ".jpeg", ".png", ".webp"):
            return
        try:
            from PIL import Image

            with Image.open(path) as img:
                w, h = img.size
                if h <= 2:
                    return
                crop_h = max(1, int(h * self.watermark_crop_bottom))
                if crop_h >= h:
                    return
                cropped = img.crop((0, 0, w, h - crop_h))
                cropped.save(path, quality=92)
            logger.info(
                f"已裁除底部水印区域 {crop_h}px: {os.path.basename(path)}"
            )
        except Exception as exc:
            logger.warning(f"水印裁剪失败(跳过原图): {exc}")

    async def _download(
        self,
        url: str,
        ext: str,
        target_dir: str | None = None,
        process_image: bool = True,
    ) -> str | None:
        """下载文件到临时目录，返回本地路径；失败返回 None。

        target_dir 指定保存目录（视频存到挂载卷目录以便宿主机访问）。
        """
        directory = target_dir or self.tmp_dir
        path = None
        try:
            os.makedirs(directory, exist_ok=True)
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    headers=DOWNLOAD_HEADERS,
                    timeout=aiohttp.ClientTimeout(total=120),
                ) as resp:
                    if resp.status != 200:
                        logger.warning(
                            f"下载失败 HTTP {resp.status}: {url[:80]}"
                        )
                        return None
                    path = os.path.join(
                        directory,
                        f"{int(time.time() * 1000)}_{os.getpid()}{ext}",
                    )
                    with open(path, "wb") as f:
                        async for chunk in resp.content.iter_chunked(64 * 1024):
                            f.write(chunk)
                    if os.path.getsize(path) == 0:
                        os.remove(path)
                        return None
                    if process_image:
                        self._maybe_crop_watermark(path, ext)
                    return path
        except Exception as exc:
            logger.warning(f"下载媒体失败: {exc} url={url[:80]}")
            if path:
                self._remove_files([path])
            return None

    async def _check_video_size(self, url: str) -> int | None:
        """HEAD 请求获取视频大小（字节）；失败返回 None。"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.head(
                    url,
                    headers=DOWNLOAD_HEADERS,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status != 200:
                        return None
                    size = resp.headers.get("Content-Length")
                    return int(size) if size and size.isdigit() else None
        except Exception:
            return None

    def _forward_window_matches(self, umo: str) -> bool:
        """支持填写群号或完整 UMO。"""
        if not self.forward_windows:
            return False
        return umo in self.forward_windows or any(
            umo.endswith(f":GroupMessage:{item}")
            for item in self.forward_windows
            if item.isdigit()
        )

    async def send_post_media(self, umo: str, post: dict) -> bool:
        """推送一条微博；指定窗口使用包含文字的合并转发。"""
        if self._forward_window_matches(umo):
            return await self._send_post_forward(umo, post)
        return await self._send_post_media_legacy(umo, post)

    async def _send_post_forward(self, umo: str, post: dict) -> bool:
        """为指定窗口构建单条微博的合并转发节点。"""
        sent_any = False
        content = []
        name = str(post.get("screen_name") or "微博用户")
        text = str(post.get("text") or "").strip()
        if text:
            content.append(Comp.Plain(f"{name}:\n{text}"))
        else:
            content.append(Comp.Plain(name))

        downloaded: list[str] = []
        seen_urls = set()
        for url in post.get("pics") or []:
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            path = await self._download(url, ".jpg", process_image=False)
            if path:
                downloaded.append(path)
                content.append(Comp.File(name=os.path.basename(path), file=path))
            else:
                logger.warning(f"原图文件下载失败: {url[:60]}")

        try:
            if len(content) > 1:
                await self.context.send_message(
                    umo, MessageChain(chain=[Nodes([Node(content=content, name=name)])])
                )
                sent_any = True
            elif text:
                await self.context.send_message(
                    umo, MessageChain(chain=[Nodes([Node(content=content, name=name)])])
                )
                sent_any = True
        except Exception as exc:
            logger.warning(f"合并转发发送失败，回退普通消息: {exc}")
            try:
                await self.context.send_message(umo, MessageChain(chain=content))
                sent_any = True
            except Exception as fallback_exc:
                logger.warning(f"合并转发回退失败: {fallback_exc}")
        self._remove_files(downloaded)
        # 视频在 CQ 的合并转发节点中兼容性较差，发送为同一微博紧随其后的独立视频。
        video_url = post.get("video_url")
        if video_url:
            size = await self._check_video_size(video_url)
            if size is None or size <= self.max_video_size_mb * 1024 * 1024:
                vpath = await self._download(video_url, ".mp4", target_dir=self.video_dir)
                if vpath:
                    try:
                        await self.context.send_message(
                            umo, MessageChain(chain=[Comp.Video(file=self._container_to_host_path(vpath))])
                        )
                        sent_any = True
                    except Exception as exc:
                        logger.warning(f"合并转发视频发送失败: {exc}")
                    finally:
                        self._remove_files([vpath])
        return sent_any

    async def _send_post_media_legacy(self, umo: str, post: dict) -> bool:
        """原有的纯媒体发送流程。"""
        sent_any = False
        downloaded: list[str] = []  # 本次下载的本地文件，发送后立即删除

        # 1) 图片：以文件发送，保留微博原始 JPEG，不让 QQ 图片消息压缩
        img_components = []
        seen_urls = set()
        for url in post.get("pics") or []:
            # API 异常或历史数据可能包含重复 URL，发送前再做一次保险去重。
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            path = await self._download(url, ".jpg", process_image=False)
            if path:
                downloaded.append(path)
                image_files.append(path)
                img_components.append(Comp.File(name=os.path.basename(path), file=path))
            else:
                try:
                    comp = Comp.Image.fromURL(url)
                    if comp is not None:
                        img_components.append(comp)
                except Exception as exc:
                    logger.warning(f"图片组件构建失败: {url[:60]}, {exc}")

        if img_components:
            try:
                await self.context.send_message(
                    umo,
                    MessageChain(chain=img_components),
                )
                sent_any = True
            except Exception as exc:
                logger.warning(f"图片消息发送失败: {exc}")
                # 逐张重试
                for comp in img_components:
                    try:
                        await self.context.send_message(
                            umo,
                            MessageChain(chain=[comp]),
                        )
                        sent_any = True
                    except Exception as exc2:
                        logger.warning(f"单张图片发送失败: {exc2}")
            # 发送完毕立即清理本地图片文件，不留在服务器
            self._remove_files(downloaded)

        # 2) 视频：单独发送；过大或失败则静默跳过（不发送文字）
        video_url = post.get("video_url")
        if video_url:
            size = await self._check_video_size(video_url)
            if size is not None and size > self.max_video_size_mb * 1024 * 1024:
                logger.info(
                    f"视频过大({size/1024/1024:.0f}MB)已跳过: {video_url[:60]}"
                )
            else:
                # 下载到挂载卷目录，再映射成宿主机路径发送，
                # 否则 NapCat（宿主机）读不到容器内 /tmp 路径
                vpath = await self._download(
                    video_url, ".mp4", target_dir=self.video_dir
                )
                if vpath:
                    host_path = self._container_to_host_path(vpath)
                    try:
                        await self.context.send_message(
                            umo,
                            MessageChain(chain=[Comp.Video(file=host_path)]),
                        )
                        sent_any = True
                    except Exception as exc:
                        logger.warning(f"视频发送失败，静默跳过: {exc}")
                    finally:
                        # 无论成败，发送后立即删除视频文件
                        self._remove_files([vpath])
                else:
                    logger.info(f"视频下载失败，静默跳过: {video_url[:60]}")

        return sent_any

    @staticmethod
    def _remove_files(paths: list[str]) -> None:
        """删除本地临时媒体文件，避免残留在服务器上。"""
        for path in paths:
            try:
                if path and os.path.exists(path):
                    os.remove(path)
                    logger.info(f"已删除推送媒体: {path}")
            except OSError as exc:
                logger.warning(f"删除推送媒体失败: {path}, {exc}")
