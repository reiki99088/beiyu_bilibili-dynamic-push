"""
核心监控逻辑

作者: 北遇 (Beiyu)
原作者: 白狐whitefox
"""
import asyncio
import base64
import json
import os
import shutil
import subprocess
import tempfile
import time
import random
import re
from datetime import datetime
from urllib.parse import unquote
from typing import Dict, List, Optional, Tuple

import aiohttp
from bilibili_api import user, Credential
from bilibili_api import dynamic as bili_dynamic

from .image_composer import async_compose_dynamic_image, get_default_avatar_path
from .utils import BiliUtils
from .subscription import sub_manager


class BiliMonitor:
    def __init__(self):
        self.running = False
        self.history = BiliUtils.load_history()
        self.credential = None
        self._tasks = []
        self.ctx = None
        self.config = None
        self.session = None
        self.uid_to_stream_ids = {}

    # 工具
    @staticmethod
    def _is_top_dynamic(item: Dict) -> bool:
        try:
            modules = item.get("modules") or {}
            tag_text = (modules.get("module_tag") or {}).get("text") or ""
            if "置顶" in tag_text:
                return True
        except Exception:
            pass
        try:
            if (item.get("modules", {}).get("module_author", {}) or {}).get("is_top"):
                return True
        except Exception:
            pass
        return False

    @staticmethod
    def _is_forward_dynamic(item: Dict) -> bool:
        """判断是否为转发类型动态。"""
        try:
            return item.get("type") == "DYNAMIC_TYPE_FORWARD"
        except Exception:
            return False

    # 生命周期
    async def update_subscription_map(self):
        if self.config:
            config_users = self._parse_subscription_lines(
                self.config.subscriptions.users or []
            )
            await sub_manager.sync_static(config_users)
        self.uid_to_stream_ids = sub_manager.get_merged_map()
        if self.ctx:
            self.ctx.logger.info(
                f"🔄 订阅映射已更新：当前共监控 {len(self.uid_to_stream_ids)} 个 B站 UID"
            )

    @staticmethod
    def _parse_subscription_lines(lines):
        """
        将形如 "114514 => 1919810, 123456" 的字符串列表
        解析成 [{"uid": "114514", "groups": ["1919810", "123456"]}, ...]
        分隔符兼容：=>  |  :  全角逗号  半角逗号  空格
        """
        import re

        result = []
        for raw in lines:
            if not isinstance(raw, str):
                continue
            line = raw.strip()
            if not line or line.startswith("#"):
                continue

            # 先把 UID 与 群号部分切开
            parts = re.split(r"\s*(?:=>|->|:|：|\|)\s*", line, maxsplit=1)
            if len(parts) != 2:
                # 兜底：第一个空白当分隔符
                parts = line.split(None, 1)
                if len(parts) != 2:
                    continue

            uid_str = parts[0].strip()
            groups_str = parts[1].strip()
            if not uid_str.isdigit():
                continue

            groups = [
                g.strip()
                for g in re.split(r"[,，\s]+", groups_str)
                if g.strip().isdigit()
            ]
            if not groups:
                continue

            result.append({"uid": uid_str, "groups": groups})

        return result

    async def start(self, ctx, config):
        if self.running:
            return
        self.running = True
        self.ctx = ctx
        self.config = config

        # 设置运行数据目录到 SDK 分配的持久化/临时目录
        try:
            data_dir = str(ctx.paths.data_dir)
            runtime_dir = str(ctx.paths.runtime_dir)
        except Exception:
            # 兼容旧版 SDK 无 paths 的情况，回退到插件目录
            data_dir = os.path.dirname(__file__)
            runtime_dir = os.path.dirname(__file__)

        from .utils import set_data_dir
        set_data_dir(data_dir)
        sub_manager.set_data_dir(data_dir)

        from .image_composer import set_avatar_cache_dir
        set_avatar_cache_dir(os.path.join(runtime_dir, "avatar_cache"))

        # 迁移后重新加载历史和订阅
        self.history = BiliUtils.load_history()

        if not self.session or self.session.closed:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))

        self.ctx.logger.info("🟢 启动 Bilibili 监控任务...")

        cred_obj = self.config.credential
        cred_dict = cred_obj.model_dump() if hasattr(cred_obj, "model_dump") else (cred_obj or {})

        if cred_dict:
            valid_cred = {}
            for k, v in cred_dict.items():
                if not v:
                    continue
                if isinstance(v, str) and "%" in v:
                    try:
                        valid_cred[k] = unquote(v)
                    except Exception:
                        valid_cred[k] = v
                else:
                    valid_cred[k] = v
            if valid_cred:
                try:
                    self.credential = Credential(**valid_cred)
                    self.ctx.logger.info("✅ B站凭证加载成功 (已自动解码)")
                except Exception as e:
                    self.ctx.logger.error(f"❌ 凭证加载失败: {e}")

        await self.update_subscription_map()
        self._tasks.append(asyncio.create_task(self.loop()))
        self._tasks.append(asyncio.create_task(self.refresh_credential_loop()))

    async def stop(self):
        self.running = False
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

        if self.session and not self.session.closed:
            await self.session.close()
            self.session = None

        if self.ctx:
            self.ctx.logger.info("🛑 Bilibili 监控停止")

    async def refresh_credential_loop(self):
        while self.running:
            await asyncio.sleep(3600 * 6)
            if self.credential:
                try:
                    if await self.credential.check_refresh():
                        await self.credential.refresh()
                        self.ctx.logger.info("🔄 B站凭据已自动刷新")
                except Exception as e:
                    self.ctx.logger.error(f"凭据刷新失败: {e}")

    # 主循环
    async def loop(self):
        while self.running:
            try:
                if not self.config.plugin.enabled:
                    await asyncio.sleep(10)
                    continue

                base_interval = self.config.settings.poll_interval
                jitter = self.config.settings.poll_jitter
                max_imgs = self.config.settings.max_images

                if not self.uid_to_stream_ids:
                    await asyncio.sleep(base_interval)
                    continue

                actual_interval = base_interval
                if jitter > 0:
                    actual_interval = random.randint(
                        max(5, base_interval - jitter), base_interval + jitter
                    )

                found_new_things = False
                for uid, stream_ids_set in self.uid_to_stream_ids.items():
                    target_stream_ids = list(stream_ids_set)
                    pushed_dyn = await self.check_dynamic(uid, target_stream_ids, max_imgs)
                    pushed_live = await self.check_live(uid, target_stream_ids)
                    pushed_fans = await self.check_fans(uid, target_stream_ids) 
                    if pushed_dyn or pushed_live:
                        found_new_things = True
                    await asyncio.sleep(1)

                if found_new_things:
                    self.ctx.logger.info(
                        f"✅ 本轮轮询完成：发现新动态/直播事件！等待 {actual_interval} 秒后进行下一轮。"
                    )
                else:
                    self.ctx.logger.info(
                        f"💤 本轮轮询完成：未发现新动态。等待 {actual_interval} 秒后进行下一轮。"
                    )

                await asyncio.sleep(actual_interval)
            except Exception as e:
                self.ctx.logger.error(f"❌ 轮询错误: {e}")
                await asyncio.sleep(60)

    # 动态检查
    async def check_dynamic(self, uid: str, stream_ids: List[str], max_imgs: int) -> bool:
        try:
            u = user.User(int(uid), credential=self.credential)
            dynamics = await u.get_dynamics_new()
            items = dynamics.get("items", [])
            if not items:
                return False

            skip_forward = self.config.settings.skip_forward if self.config else True

            user_hist = self.history.get(uid, {})
            if isinstance(user_hist, str):
                user_hist = {"dyn_id": user_hist}

            last_saved_id = user_hist.get("dyn_id")
            last_top_id = user_hist.get("top_dyn_id")

            top_item = None
            normal_items = []
            for item in items:
                if item.get("type") == "DYNAMIC_TYPE_LIVE_RCMD":
                    continue
                try:
                    major_type = (
                        item.get("modules", {})
                        .get("module_dynamic", {})
                        .get("major", {})
                        or {}
                    ).get("type")
                    if major_type == "MAJOR_TYPE_LIVE_RCMD":
                        continue
                except Exception:
                    pass

                if skip_forward and self._is_forward_dynamic(item):
                    self.ctx.logger.info(
                        f"🔀 UID {uid} 跳过转发动态 (ID: {item.get('id_str', '?')})"
                    )
                    continue

                if self._is_top_dynamic(item) and top_item is None:
                    top_item = item
                else:
                    normal_items.append(item)

            if not last_saved_id:
                if normal_items:
                    latest_id = max(int(it["id_str"]) for it in normal_items)
                    user_hist["dyn_id"] = str(latest_id)
                else:
                    user_hist["dyn_id"] = str(items[0]["id_str"])

                if top_item:
                    user_hist["top_dyn_id"] = str(top_item["id_str"])

                self.ctx.logger.info(
                    f"UID {uid} 首次初始化动态，基准ID: {user_hist['dyn_id']}, "
                    f"置顶ID: {user_hist.get('top_dyn_id', '无')}"
                )
                self.history[uid] = user_hist
                await BiliUtils.save_history(self.history)
                return False

            new_items = []
            for item in normal_items:
                curr_id = int(item["id_str"])
                if curr_id > int(last_saved_id):
                    new_items.append(item)
                else:
                    break

            if top_item:
                top_id_str = str(top_item["id_str"])
                if top_id_str != str(last_top_id or ""):
                    if int(top_id_str) > int(last_saved_id):
                        new_items.append(top_item)
                        self.ctx.logger.info(
                            f"UID {uid} 检测到新的置顶动态: {top_id_str}（将推送）"
                        )
                    else:
                        self.ctx.logger.info(
                            f"UID {uid} 置顶动态变更为旧动态 {top_id_str}，仅更新记录，不推送"
                        )
                    user_hist["top_dyn_id"] = top_id_str
            else:
                if last_top_id:
                    self.ctx.logger.info(f"UID {uid} 置顶动态已被取消")
                    user_hist["top_dyn_id"] = None

            if not new_items:
                self.history[uid] = user_hist
                await BiliUtils.save_history(self.history)
                return False
            
            # 新动态自动点赞
            if self.config and self.config.settings.auto_like:
                for it in new_items:
                    try:
                        await self._auto_like_dynamic(it)
                        await asyncio.sleep(0.5)  # 防风控小间隔
                    except Exception as e:
                        self.ctx.logger.error(f"自动点赞调度异常: {e}")

            latest_item_to_push = max(new_items, key=lambda it: int(it["id_str"]))
            latest_id_str = str(latest_item_to_push["id_str"])

            max_age = self.config.settings.max_dynamic_age if self.config else 3600
            try:
                raw_pub_ts = (
                    latest_item_to_push.get("modules", {})
                    .get("module_author", {})
                    .get("pub_ts", 0)
                )
                pub_ts = int(raw_pub_ts) if raw_pub_ts else 0
            except (ValueError, TypeError, AttributeError):
                pub_ts = 0

            now_ts = time.time()
            if pub_ts > 0 and (now_ts - pub_ts) > max_age:
                age_str = BiliUtils.format_duration(now_ts - pub_ts)
                self.ctx.logger.info(
                    f"⏳ UID {uid} 发现新动态 {latest_id_str}，但发布于 {age_str} 前，"
                    f"超过设定阈值 {max_age} 秒，静默更新基准ID不推送。"
                )
                if not self._is_top_dynamic(latest_item_to_push):
                    user_hist["dyn_id"] = latest_id_str
                self.history[uid] = user_hist
                await BiliUtils.save_history(self.history)
                return False

            is_top_push = self._is_top_dynamic(latest_item_to_push)
            tag_str = "（📌置顶）" if is_top_push else ""
            self.ctx.logger.info(
                f"🎉 UID {uid} 发现新动态{tag_str}: {latest_id_str} "
                f"(准备推送到 {len(stream_ids)} 个流节点)"
            )
            await self.process_and_push(latest_item_to_push, stream_ids, max_imgs)

            if not is_top_push:
                user_hist["dyn_id"] = latest_id_str
            normal_new = [it for it in new_items if not self._is_top_dynamic(it)]
            if normal_new:
                max_normal_id = str(max(int(it["id_str"]) for it in normal_new))
                if int(max_normal_id) > int(user_hist.get("dyn_id", 0)):
                    user_hist["dyn_id"] = max_normal_id

            self.history[uid] = user_hist
            await BiliUtils.save_history(self.history)
            return True

        except Exception as e:
            self.ctx.logger.error(f"UID {uid} 动态检查失败: {e}")
            return False

    # 直播检查
    async def check_live(self, uid: str, stream_ids: List[str]) -> bool:
        try:
            u = user.User(int(uid), credential=self.credential)
            raw_info = await u.get_live_info()

            live_room = raw_info.get("live_room", {})
            current_status = live_room.get("liveStatus", 0)
            room_title = live_room.get("title", "直播间")
            url = live_room.get("url", "")
            cover = live_room.get("cover", "")
            uname = raw_info.get("name", "UP主")

            user_hist = self.history.get(uid, {})
            if isinstance(user_hist, str):
                user_hist = {"dyn_id": user_hist}

            last_status = user_hist.get("live_status", 0)
            start_time = user_hist.get("live_start_time", 0)

            if "live_status" not in user_hist:
                user_hist["live_status"] = current_status
                if current_status == 1:
                    user_hist["live_start_time"] = time.time()
                self.history[uid] = user_hist
                await BiliUtils.save_history(self.history)
                return False

            has_event = False
            if current_status == 1 and last_status == 0:
                self.ctx.logger.info(f"UID {uid} 开播")
                msg = (
                    f"🔴 【{uname}】开播了！\n"
                    f"📺 标题：{room_title}\n"
                    f"🔗 传送门：{url}\n"
                    f"⏰ 时间：{datetime.now().strftime('%H:%M:%S')}"
                )
                await self.push_simple(msg, cover, stream_ids)
                user_hist["live_start_time"] = time.time()
                has_event = True

            elif current_status == 0 and last_status == 1:
                self.ctx.logger.info(f"UID {uid} 下播")
                duration_str = "未知"
                if start_time:
                    duration_str = BiliUtils.format_duration(time.time() - start_time)
                msg = f"🏁 【{uname}】下播了~\n⏱️ 本次直播时长：{duration_str}"
                await self.push_simple(msg, "", stream_ids)
                user_hist["live_start_time"] = 0
                has_event = True

            if current_status != last_status:
                user_hist["live_status"] = current_status
                self.history[uid] = user_hist
                await BiliUtils.save_history(self.history)

            return has_event

        except Exception:
            return False

    # 推送
    async def push_simple(self, text: str, image_url: str, group_ids: List[int]):
        b64 = None
        if image_url:
            b64 = await BiliUtils.url_to_base64(image_url, self.session)

        for gid in group_ids:
            message_chain = [{"type": "text", "data": {"text": text}}]
            if b64:
                message_chain.append({"type": "text", "data": {"text": "\n"}})
                message_chain.append({"type": "image", "data": {"file": f"base64://{b64}"}})
            try:
                await self.ctx.api.call(
                    "adapter.napcat.message.send_msg",
                    params={
                        "message_type": "group",
                        "group_id": gid,
                        "message": message_chain,
                    },
                )
            except Exception as e:
                self.ctx.logger.error(f"发送普通消息失败: {e}")

    async def process_and_push(self, item: Dict, group_ids: List[int], max_imgs: int):
        parsed = self.parse_dynamic(item)
        if not parsed:
            return

        author = parsed.get("author", "UP主")
        pub_ts = parsed.get("pub_ts", 0)
        try:
            pub_ts = int(pub_ts) if pub_ts else 0
        except (ValueError, TypeError):
            pub_ts = 0

        pub_time_str = ""
        if pub_ts > 0:
            try:
                pub_time_str = datetime.fromtimestamp(pub_ts).strftime("%Y-%m-%d %H:%M:%S")
            except Exception as e:
                self.ctx.logger.warning(f"格式化发布时间失败: {e}, pub_ts={pub_ts}")

        text = parsed.get("text", "")
        url = parsed.get("url", "")
        images = parsed.get("images", [])[:9]
        video_info = parsed.get("video_info")

        # 提取UP主头像信息
        modules = item.get("modules") or {}
        module_author = modules.get("module_author") or {}
        avatar_face = module_author.get("face", "") or ""
        avatar_uid = str(module_author.get("mid", "") or "")

        # 获取头像源路径
        avatar_path = get_default_avatar_path()

        # 合成图片
        self.ctx.logger.info(
            f"🎨 正在合成动态图片: 【{author}】 - {url}"
        )
        img_bytes = await async_compose_dynamic_image(
            author=author,
            text=text,
            url=url,
            image_urls=images,
            avatar_source_path=avatar_path,
            session=self.session,
            pub_time=pub_time_str,
            avatar_face_url=avatar_face,
            avatar_uid=avatar_uid,
        )

        if not img_bytes:
            self.ctx.logger.error("❌ 图片合成失败，尝试使用旧方式发送")
            await self._push_legacy(parsed, group_ids, max_imgs)
        else:
            # 转换为base64
            b64_str = base64.b64encode(img_bytes).decode("utf-8")

            # 发送合成图片
            for gid in group_ids:
                try:
                    await self.ctx.api.call(
                        "adapter.napcat.message.send_msg",
                        params={
                            "message_type": "group",
                            "group_id": gid,
                            "message": [
                                {"type": "image", "data": {"file": f"base64://{b64_str}"}},
                            ],
                        },
                    )
                    await asyncio.sleep(0.5)
                except Exception as e:
                    self.ctx.logger.error(f"发送合成图片失败: {e}")

            # 非视频动态附送原图；少量图片合成一个消息，超过阈值合并转发。
            if not video_info and images:
                await self._push_original_images(images, group_ids, max_imgs, author)

        if video_info:
            settings = getattr(self.config, "settings", None)
            send_video_file = getattr(settings, "send_video_file", True)
            if send_video_file:
                self.ctx.logger.info("🎬 视频动态将在后台下载并发送视频文件")
                asyncio.create_task(
                    self._process_video_dynamic(video_info, author, group_ids)
                )
            else:
                await self._push_video_link(video_info, author, url, group_ids)

    async def _push_original_images(
        self, images: List[str], group_ids: List[int], max_imgs: int, author: str,
    ):
        """将原始图片作为单条图文消息发送，超阈值时改为合并转发。"""
        tmp_dir = tempfile.mkdtemp(prefix="bili_imgs_")
        downloaded = []
        try:
            for index, img_url in enumerate(images):
                try:
                    img_data = await self._download_url(img_url)
                    if not img_data:
                        continue

                    url_path = img_url.split("?", 1)[0].lower()
                    ext = os.path.splitext(url_path)[1]
                    if ext not in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
                        ext = ".jpg"
                    tmp_file = os.path.join(tmp_dir, f"img_{index}{ext}")
                    with open(tmp_file, "wb") as image_file:
                        image_file.write(img_data)
                    downloaded.append((tmp_file, img_data))
                except Exception as e:
                    self.ctx.logger.error(f"下载原始图片失败: {e}")

            if not downloaded:
                return

            try:
                threshold = max(0, int(max_imgs))
            except (TypeError, ValueError):
                threshold = 3

            if len(downloaded) > threshold:
                forward_nodes = [
                    {
                        "type": "node",
                        "data": {
                            "name": author,
                            "uin": "10000",
                            "content": [
                                {
                                    "type": "image",
                                    "data": {
                                        "file": f"base64://{base64.b64encode(img_data).decode('utf-8')}"
                                    },
                                }
                            ],
                        },
                    }
                    for _, img_data in downloaded
                ]
                for gid in group_ids:
                    try:
                        await self.ctx.api.call(
                            "adapter.napcat.message.send_group_forward_msg",
                            params={"group_id": gid, "message": forward_nodes},
                        )
                        await asyncio.sleep(0.5)
                    except Exception as e:
                        self.ctx.logger.error(f"发送原始图片合并转发到群 {gid} 失败: {e}")
            else:
                message_chain = [
                    {"type": "image", "data": {"file": os.path.abspath(path)}}
                    for path, _ in downloaded
                ]
                for gid in group_ids:
                    try:
                        await self.ctx.api.call(
                            "adapter.napcat.message.send_msg",
                            params={
                                "message_type": "group",
                                "group_id": gid,
                                "message": message_chain,
                            },
                        )
                        await asyncio.sleep(0.5)
                    except Exception as e:
                        self.ctx.logger.error(f"发送原始图片到群 {gid} 失败: {e}")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    async def _push_video_link(
        self, video_info: Dict, author: str, dynamic_url: str, group_ids: List[int],
    ):
        """仅发送视频/动态链接，不下载视频流或调用 ffmpeg。"""
        bvid = str(video_info.get("bvid") or "").strip()
        aid = str(video_info.get("aid") or "").strip()
        title = str(video_info.get("title") or "视频投稿").strip()
        video_id = bvid or (f"av{aid}" if aid else "")

        message_lines = [f"🎬 【{author}】视频投稿：{title}"]
        if video_id:
            message_lines.append(f"🔗 视频链接：https://www.bilibili.com/video/{video_id}")
        if dynamic_url:
            message_lines.append(f"📝 动态链接：{dynamic_url}")
        message = "\n".join(message_lines)

        for gid in group_ids:
            try:
                await self.ctx.api.call(
                    "adapter.napcat.message.send_msg",
                    params={
                        "message_type": "group",
                        "group_id": gid,
                        "message": [{"type": "text", "data": {"text": message}}],
                    },
                )
                await asyncio.sleep(0.5)
            except Exception as e:
                self.ctx.logger.error(f"发送视频链接到群 {gid} 失败: {e}")

    async def _push_legacy(self, parsed: Dict, group_ids: List[int], max_imgs: int):
        """降级方案：使用旧的文本+图片方式"""
        author = parsed.get("author", "UP主")
        text = parsed.get("text", "")
        url = parsed.get("url", "")
        images = parsed.get("images", [])[:9]

        pub_ts = parsed.get("pub_ts", 0)
        try:
            pub_ts = int(pub_ts) if pub_ts else 0
        except (ValueError, TypeError):
            pub_ts = 0

        time_line = ""
        if pub_ts > 0:
            try:
                pub_time_str = datetime.fromtimestamp(pub_ts).strftime("%Y-%m-%d %H:%M:%S")
                time_line = f"🕒 发布时间: {pub_time_str}\n"
            except Exception:
                pass

        text_msg = (
            f"📢 【{author}】发布了新动态！\n{time_line}{text}\n"
            f"🔗 链接: {url}"
        )

        cached_b64s = []
        for img_url in images:
            b64 = await BiliUtils.url_to_base64(img_url, self.session)
            if b64:
                cached_b64s.append(b64)

        num_imgs = len(cached_b64s)

        if num_imgs > max_imgs:
            forward_nodes = []
            for b64 in cached_b64s:
                forward_nodes.append({
                    "type": "node",
                    "data": {
                        "name": author,
                        "uin": "10000",
                        "content": [{"type": "image", "data": {"file": f"base64://{b64}"}}],
                    },
                })

            for gid in group_ids:
                try:
                    await self.ctx.api.call(
                        "adapter.napcat.message.send_msg",
                        params={
                            "message_type": "group",
                            "group_id": gid,
                            "message": [{"type": "text", "data": {"text": text_msg}}],
                        },
                    )
                    await self.ctx.api.call(
                        "adapter.napcat.message.send_group_forward_msg",
                        params={"group_id": gid, "message": forward_nodes},
                    )
                    await asyncio.sleep(0.5)
                except Exception as e:
                    self.ctx.logger.error(f"发送合并转发(仅图片)失败: {e}")
        else:
            message_chain = [{"type": "text", "data": {"text": text_msg + "\n"}}]
            for b64 in cached_b64s:
                message_chain.append({"type": "image", "data": {"file": f"base64://{b64}"}})
            for gid in group_ids:
                try:
                    await self.ctx.api.call(
                        "adapter.napcat.message.send_msg",
                        params={
                            "message_type": "group",
                            "group_id": gid,
                            "message": message_chain,
                        },
                    )
                    await asyncio.sleep(0.5)
                except Exception as e:
                    self.ctx.logger.error(f"发送同气泡图文失败: {e}")

    async def _process_video_dynamic(
        self, video_info: Dict, author: str, group_ids: List[int]
    ):
        """
        处理视频动态：下载视频和音频，使用ffmpeg合成为MP4后发送
        使用 bilibili_api 的 Video.get_cid + get_download_url + VideoDownloadURLDataDetecter
        """
        bvid = video_info.get("bvid", "")
        aid = video_info.get("aid", "")
        title = video_info.get("title", "")

        if not bvid and not aid:
            self.ctx.logger.warning("⚠️ 视频动态缺少bvid/aid，跳过视频合成")
            return

        try:
            self.ctx.logger.info(f"🎬 准备下载视频: {title} (bvid={bvid}, aid={aid})")

            from bilibili_api import video as bili_video

            bvid_str = str(bvid) if bvid else ""
            aid_int = int(aid) if aid else 0

            v = bili_video.Video(
                bvid=bvid_str,
                aid=aid_int,
                credential=self.credential,
            )

            # 获取CID（默认第一P）
            self.ctx.logger.info("🔍 获取视频CID...")
            try:
                cid = await v.get_cid(page_index=0)
            except Exception as e:
                self.ctx.logger.error(f"获取CID失败: {e}")
                return

            # 获取下载URL数据
            self.ctx.logger.info("🔍 获取下载链接...")
            try:
                download_data = await v.get_download_url(cid=cid)
            except Exception as e:
                self.ctx.logger.error(f"获取下载链接失败: {e}")
                return

            # 使用 VideoDownloadURLDataDetecter 解析
            detector = bili_video.VideoDownloadURLDataDetecter(download_data)
            streams = detector.detect()

            # 从detector获取主URL，同时从原始API响应获取备用URL
            video_urls = []
            audio_urls = []

            # 分离视频流和音频流（主URL）
            for stream in streams:
                if isinstance(stream, bili_video.VideoStreamDownloadURL):
                    video_urls.append(stream.url)
                elif isinstance(stream, bili_video.AudioStreamDownloadURL):
                    audio_urls.append(stream.url)
                elif isinstance(stream, (bili_video.FLVStreamDownloadURL, bili_video.MP4StreamDownloadURL)):
                    video_urls.append(stream.url)

            # 从原始API响应提取backupUrl备用
            dash_data = download_data.get("dash", {})
            if dash_data:
                for v in dash_data.get("video", []):
                    backup = v.get("backupUrl") or v.get("backup_url")
                    if backup:
                        video_urls.append(backup)
                for a in dash_data.get("audio", []) or []:
                    backup = a.get("backupUrl") or a.get("backup_url")
                    if backup:
                        audio_urls.append(backup)

            if not video_urls:
                self.ctx.logger.warning("⚠️ 无法获取视频流URL，跳过视频合成")
                return

            # Bilibili视频流需要完整的请求头（含Referer和User-Agent）
            video_page_url = f"https://www.bilibili.com/video/{bvid_str}" if bvid_str else "https://www.bilibili.com"
            headers = {
                "Referer": video_page_url,
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                "Origin": "https://www.bilibili.com",
            }

            # 如果有credential，带上cookies
            if self.credential:
                try:
                    cookies = self.credential.get_cookies()
                    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items() if v)
                    if cookie_str:
                        headers["Cookie"] = cookie_str
                except Exception:
                    pass

            # 下载视频流（尝试多个URL）
            video_data = None
            for i, vurl in enumerate(video_urls):
                self.ctx.logger.info(f"⬇️ 正在下载视频流... (尝试 {i+1}/{len(video_urls)})")
                video_data = await self._download_url(vurl, headers=headers)
                if video_data:
                    break
                self.ctx.logger.warning(f"  视频流URL {i+1} 下载失败，尝试下一个...")

            if not video_data:
                self.ctx.logger.error("❌ 所有视频流下载失败")
                return

            # 下载音频流（尝试多个URL）
            audio_data = None
            for i, aurl in enumerate(audio_urls):
                self.ctx.logger.info(f"⬇️ 正在下载音频流... (尝试 {i+1}/{len(audio_urls)})")
                audio_data = await self._download_url(aurl, headers=headers)
                if audio_data:
                    break
                self.ctx.logger.warning(f"  音频流URL {i+1} 下载失败，尝试下一个...")

            # 使用ffmpeg合成
            self.ctx.logger.info("🎞️ 正在使用ffmpeg合成视频...")
            result = await asyncio.to_thread(
                self._ffmpeg_combine, video_data, audio_data, bvid or str(aid)
            )

            if result:
                output_path, tmp_dir = result
                try:
                    # 发送合成后的视频
                    await self._send_video_file(output_path, title, author, group_ids)
                finally:
                    # 无论发送成功或失败，都删除临时目录
                    if tmp_dir and os.path.exists(tmp_dir):
                        self.ctx.logger.info(f"🗑️ 清理临时文件: {tmp_dir}")
                        shutil.rmtree(tmp_dir, ignore_errors=True)
            else:
                self.ctx.logger.error("❌ ffmpeg合成失败")

        except Exception as e:
            self.ctx.logger.error(f"❌ 视频处理失败: {e}")
            import traceback
            traceback.print_exc()

    async def _download_url(self, url: str, headers: Dict = None) -> Optional[bytes]:
        """下载URL内容，返回字节数据"""
        try:
            request_headers = headers or {}
            async with self.session.get(url, headers=request_headers, timeout=aiohttp.ClientTimeout(total=120)) as resp:
                if resp.status == 200:
                    return await resp.read()
                else:
                    self.ctx.logger.error(f"下载失败，HTTP {resp.status}: {url[:120]}...")
                    return None
        except asyncio.TimeoutError:
            self.ctx.logger.error(f"下载超时: {url[:80]}...")
            return None
        except Exception as e:
            self.ctx.logger.error(f"下载异常: {e}")
            return None

    def _ffmpeg_combine(
        self, video_data: bytes, audio_data: Optional[bytes], output_name: str,
    ) -> Optional[Tuple[str, str]]:
        """
        使用ffmpeg将视频和音频合成为MP4
        :return: (输出文件路径, 临时目录路径)，失败返回None
        """
        video_path = None
        audio_path = None
        output_path = None
        tmp_dir = None

        try:
            tmp_dir = tempfile.mkdtemp(prefix="bili_video_")
            video_path = os.path.join(tmp_dir, "video.m4v")
            with open(video_path, "wb") as f:
                f.write(video_data)

            output_path = os.path.join(tmp_dir, f"{output_name}.mp4")

            if audio_data:
                audio_path = os.path.join(tmp_dir, "audio.m4a")
                with open(audio_path, "wb") as f:
                    f.write(audio_data)

                cmd = [
                    "ffmpeg",
                    "-y",
                    "-i", video_path,
                    "-i", audio_path,
                    "-c:v", "libx264",
                    "-preset", "medium",
                    "-crf", "23",
                    "-c:a", "aac",
                    "-b:a", "128k",
                    "-shortest",
                    output_path,
                ]
            else:
                cmd = [
                    "ffmpeg",
                    "-y",
                    "-i", video_path,
                    "-c:v", "libx264",
                    "-preset", "medium",
                    "-crf", "23",
                    "-an",
                    output_path,
                ]

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,
            )

            if result.returncode != 0:
                self.ctx.logger.error(f"ffmpeg错误: {result.stderr}")
                # 清理临时目录
                if tmp_dir and os.path.exists(tmp_dir):
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                return None

            if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                size_mb = os.path.getsize(output_path) / (1024 * 1024)
                self.ctx.logger.info(f"ffmpeg合成完成: {size_mb:.1f}MB")
                return (output_path, tmp_dir)
            else:
                self.ctx.logger.error("ffmpeg输出文件为空")
                if tmp_dir and os.path.exists(tmp_dir):
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                return None

        except FileNotFoundError:
            self.ctx.logger.error("❌ 未找到ffmpeg命令，请确保ffmpeg已安装并加入PATH")
            if tmp_dir and os.path.exists(tmp_dir):
                shutil.rmtree(tmp_dir, ignore_errors=True)
            return None
        except subprocess.TimeoutExpired:
            self.ctx.logger.error("❌ ffmpeg合成超时")
            if tmp_dir and os.path.exists(tmp_dir):
                shutil.rmtree(tmp_dir, ignore_errors=True)
            return None
        except Exception as e:
            self.ctx.logger.error(f"❌ ffmpeg合成异常: {e}")
            if tmp_dir and os.path.exists(tmp_dir):
                shutil.rmtree(tmp_dir, ignore_errors=True)
            return None
        finally:
            for path in [video_path, audio_path]:
                if path and os.path.exists(path):
                    try:
                        os.remove(path)
                    except Exception:
                        pass

    async def _send_video_file(
        self, file_path: str, title: str, author: str, group_ids: List[int],
    ):
        """发送视频文件到群聊（使用本地路径，避免WebSocket帧大小限制）"""
        try:
            file_size = os.path.getsize(file_path)
            size_mb = file_size / (1024 * 1024)
            abs_path = os.path.abspath(file_path)

            self.ctx.logger.info(f"📤 发送视频: {title} ({size_mb:.1f}MB)")

            for gid in group_ids:
                try:
                    # 使用本地文件路径而非base64，通过HTTP上传绕过帧大小限制
                    message_chain = [
                        {"type": "text", "data": {"text": f"🎬 【{author}】视频投稿：{title}"}},
                        {"type": "video", "data": {"file": abs_path}},
                    ]
                    await self.ctx.api.call(
                        "adapter.napcat.message.send_msg",
                        params={
                            "message_type": "group",
                            "group_id": gid,
                            "message": message_chain,
                        },
                    )
                    await asyncio.sleep(1)
                except Exception as e:
                    self.ctx.logger.error(f"发送视频到群 {gid} 失败: {e}")
        except Exception as e:
            self.ctx.logger.error(f"读取视频文件失败: {e}")

    async def _auto_like_dynamic(self, item: Dict):
        """对单条动态自动点赞。"""
        if not self.config or not self.config.settings.auto_like:
            return
        if not self.credential:
            self.ctx.logger.warning("⚠️ 自动点赞已开启，但 B 站凭证未加载，跳过。")
            return

        dyn_id = str(item.get("id_str") or "")
        if not dyn_id:
            return

        # 取作者名仅为日志好看
        try:
            author = (
                (item.get("modules") or {})
                .get("module_author", {})
                .get("name", "UP主")
            )
        except Exception:
            author = "UP主"

        try:
            d = bili_dynamic.Dynamic(int(dyn_id), credential=self.credential)
            # bilibili_api 的 set_like(status=True) -> 点赞；False -> 取消
            await d.set_like(True)
            self.ctx.logger.info(f"👍 自动点赞成功: 【{author}】 动态 {dyn_id}")
        except Exception as e:
            self.ctx.logger.error(f"❌ 自动点赞失败: 动态 {dyn_id}, 原因: {e}")

    # 粉丝数
    async def check_fans(self, uid: str, stream_ids: List[str]) -> bool:
        try:
            u = user.User(int(uid), credential=self.credential)
            rel = await u.get_relation_info()
            current_fans = int(rel.get("follower", 0))
        except Exception as e:
            self.ctx.logger.error(f"UID {uid} 粉丝数获取失败: {e}")
            return False

        user_hist = self.history.get(uid, {})
        if isinstance(user_hist, str):
            user_hist = {"dyn_id": user_hist}

        current_milestone = BiliUtils.get_current_milestone(current_fans)
        # 用 "键是否存在" 判断首次初始化，而不是用值
        is_first_init = "fans_milestone" not in user_hist
        try:
            last_milestone = int(user_hist.get("fans_milestone") or 0)
        except (TypeError, ValueError):
            last_milestone = 0
        milestones = user_hist.get("fans_milestones", {}) or {}

        user_hist["fans"] = current_fans  # 始终缓存最新粉丝数

        # 首次初始化，不推送
        if is_first_init:
            user_hist["fans_milestone"] = current_milestone
            user_hist["fans_milestones"] = milestones
            # 把当前里程碑的达成时间补一个占位（用 0 表示"未记录精确时间"）
            if current_milestone >= 10_000:
                milestones.setdefault(str(current_milestone), 0)
            self.history[uid] = user_hist
            await BiliUtils.save_history(self.history)
            self.ctx.logger.info(
                f"UID {uid} 首次初始化粉丝里程碑，当前 {current_fans}，基准 {current_milestone}"
            )
            return False

        has_event = False
        # 防暴推：单轮跨度超过 2 档视为异常（数据被清/手动改动），静默更新
        step_now = BiliUtils.get_milestone_step(current_milestone) or 10_000
        if last_milestone > 0 and (current_milestone - last_milestone) > step_now * 2:
            self.ctx.logger.warning(
                f"⚠️ UID {uid} 粉丝里程碑跨度异常 "
                f"({last_milestone} → {current_milestone})，静默更新不推送"
            )
            user_hist["fans_milestone"] = current_milestone
            user_hist["fans_milestones"] = milestones
            self.history[uid] = user_hist
            await BiliUtils.save_history(self.history)
            return False
        
        if current_milestone > last_milestone and current_milestone >= 10_000:
            now_ts = time.time()
            # 起点 m：上一里程碑的下一档；若上次 < 1 万，则从 1 万开始
            if last_milestone >= 10_000:
                start_step = BiliUtils.get_milestone_step(last_milestone + 1) or 10_000
                m = last_milestone + start_step
            else:
                m = 10_000
            # 避免极端数据导致死循环
            guard = 0
            while m <= current_milestone and guard < 1000:
                milestones.setdefault(str(m), now_ts)
                next_step = BiliUtils.get_milestone_step(m + 1) or 10_000
                m += next_step
                guard += 1

            time_str = datetime.fromtimestamp(now_ts).strftime("%Y-%m-%d %H:%M:%S")
            uname = sub_manager.get_name(uid) or "UP主"
            msg = (
                f"🎊 【{uname}】粉丝数突破 {BiliUtils.format_fans(current_milestone)}！\n"
                f"📊 当前粉丝数：{current_fans:,}\n"
                f"⏰ 达成时间：{time_str}\n"
                f"🔗 https://space.bilibili.com/{uid}"
            )
            await self.push_simple(msg, "", stream_ids)
            user_hist["fans_milestone"] = current_milestone
            user_hist["fans_milestones"] = milestones
            has_event = True
            self.ctx.logger.info(
                f"🎊 UID {uid} 粉丝达到里程碑 {current_milestone}（当前 {current_fans}）"
            )

        self.history[uid] = user_hist
        await BiliUtils.save_history(self.history)
        return has_event

    # 解析
    def _extract_major_data(self, module_dynamic: Dict) -> Tuple[str, List[str], Optional[Dict]]:
        """
        从 module_dynamic 中提取主要内容
        :return: (文本, 图片列表, 视频信息或None)
        """
        text = ""
        images = []
        video_info = None
        major = module_dynamic.get("major") or {}
        major_type = major.get("type")

        if major_type in ["MAJOR_TYPE_OPUS", "MAJOR_TYPE_ARTICLE"]:
            opus = major.get("opus") or {}
            text = opus.get("summary", {}).get("text", "") or opus.get("title", "")
            images = [p.get("url") for p in opus.get("pics", [])]
        elif major_type == "MAJOR_TYPE_DRAW":
            items = major.get("draw", {}).get("items", [])
            images = [i.get("src") for i in items]
        elif major_type in ["MAJOR_TYPE_ARCHIVE", "MAJOR_TYPE_VIDEO"]:
            video_data = major.get("archive") or major.get("video") or {}
            title = video_data.get("title", "视频投稿")
            desc = video_data.get("desc", "")
            cover = video_data.get("cover", "")
            bvid = video_data.get("bvid", "")
            aid = video_data.get("aid", "")
            text = f"📺 {title}\n{desc}"
            if cover:
                images.append(cover)
            # 视频信息，用于后续下载合成
            video_info = {
                "bvid": bvid,
                "aid": aid,
                "title": title,
                "cover": cover,
            }

        return text, images, video_info

    def parse_dynamic(self, item: Dict) -> Optional[Dict]:
        try:
            id_str = item.get("id_str")
            modules = item.get("modules") or {}
            module_dynamic = modules.get("module_dynamic") or {}
            module_author = modules.get("module_author") or {}

            main_text, main_images, main_video_info = self._extract_major_data(module_dynamic)
            desc_text = (module_dynamic.get("desc") or {}).get("text", "")

            ignore_lottery = self.config.settings.ignore_lottery if self.config else True
            if ignore_lottery:
                full_text_for_check = f"{desc_text}\n{main_text}"
                if re.search(r"恭喜@.*?中奖.*?详情请点击.*?查看", full_text_for_check, re.DOTALL):
                    self.ctx.logger.info(
                        f"🛑 拦截到开奖通知动态 (ID: {id_str})，已丢弃，不进行推送。"
                    )
                    return None

            raw_pub_ts = module_author.get("pub_ts", 0)
            try:
                pub_ts = int(raw_pub_ts) if raw_pub_ts else 0
            except (ValueError, TypeError):
                pub_ts = 0

            result = {
                "type": "unknown",
                "text": "",
                "images": [],
                "video_info": main_video_info,
                "url": f"https://t.bilibili.com/{id_str}",
                "author": module_author.get("name", "UP主"),
                "pub_ts": pub_ts,
            }

            if desc_text:
                result["text"] += desc_text
            if main_text:
                result["text"] += f"\n{main_text}"
            result["images"].extend(main_images)

            if item.get("type") == "DYNAMIC_TYPE_FORWARD":
                orig = item.get("orig") or {}
                if orig.get("type") == "DYNAMIC_TYPE_NONE":
                    result["text"] += "\n\n[原动态已被删除]"
                else:
                    orig_modules = orig.get("modules") or {}
                    orig_author = (orig_modules.get("module_author") or {}).get("name", "未知用户")
                    orig_dynamic = orig_modules.get("module_dynamic") or {}
                    orig_desc = (orig_dynamic.get("desc") or {}).get("text", "")
                    orig_major_text, orig_major_images, orig_video_info = self._extract_major_data(orig_dynamic)

                    result["text"] += f"\n\n🔁 转发 @{orig_author}:"
                    if orig_desc:
                        result["text"] += f"\n{orig_desc}"
                    if orig_major_text:
                        result["text"] += f"\n{orig_major_text}"
                    result["images"].extend(orig_major_images)
                    # 转发的视频也保留
                    if orig_video_info:
                        result["video_info"] = orig_video_info

            return result
        except Exception as e:
            self.ctx.logger.error(f"解析出错: {e}")
            return None

# 全局单例
monitor_instance = BiliMonitor()
