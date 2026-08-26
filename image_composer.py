"""
图片合成器
将B站动态信息合成为一张美观的图片进行发送

作者: 北遇 (Beiyu)
原作者: 白狐whitefox
"""
import os
import io
import re
import time
import logging
import hashlib
from typing import Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger("bilibili_dynamic_push")

# ================ 布局常量 ================
CANVAS_WIDTH = 960
PADDING = 28              # 四周统一内边距
CONTENT_WIDTH = CANVAS_WIDTH - 2 * PADDING  # 904px

# 头像区域
AVATAR_SIZE = 96          # 圆形头像直径
AVATAR_X = PADDING        # 头像左上角x
AVATAR_Y = PADDING        # 头像左上角y
AVATAR_BORDER = 3         # 头像外圈边框宽度

# 文字区域
HEADER_Y = PADDING + 6    # 名称起始y（与头像顶部基本对齐）
NAME_X = AVATAR_X + AVATAR_SIZE + 20  # 名称x
TEXT_START_Y = PADDING + AVATAR_SIZE + 28  # 文案起始y
TEXT_MAX_WIDTH = CONTENT_WIDTH   # 文案最大宽度

# 图片网格
SINGLE_IMG_WIDTH = 900    # 1张图
MULTI_IMG_WIDTH = 436     # 2-4张图，每行2张
MANY_IMG_WIDTH = 280      # 5+张图，每行3张
IMG_GAP = 16              # 图片间距
IMG_RADIUS = 8            # 图片圆角半径
IMG_OFFSET_Y = 24         # 图片与文案的间距

# ================ 配色 ================
COLOR_BG = (15, 17, 21)           # 深灰黑背景 #0f1115
COLOR_AVATAR_BORDER = (255, 107, 157)  # 头像边框粉色 #ff6b9d
COLOR_NAME = (245, 245, 250)      # 名称近白色 #f5f5fa
COLOR_TIME = (140, 145, 155)     # 时间灰色 #8c919b
COLOR_DOT = (100, 105, 115)      # 分隔符圆点 #646973
COLOR_TEXT = (220, 222, 230)     # 正文浅灰白 #dedee6
COLOR_SEPARATOR = (42, 45, 53)   # 分隔线 #2a2d35
COLOR_IMG_BG = (30, 32, 38)      # 图片占位背景 #1e2026

# ================ 字体 ================
FONT_NAME_SIZE = 28
FONT_TEXT_SIZE = 26
FONT_META_SIZE = 18
FONT_NAME_BOLD = True   # 名称使用粗体

# 文字行间距
LINE_SPACING = 14         # 行与行之间额外间距
TEXT_LINE_HEIGHT = 38     # 正文行高（字号 + spacing）

# 头像缓存目录
AVATAR_CACHE_DIR = os.path.join(os.path.dirname(__file__), "assets", "avatar_cache")

# ================ 预编译正则 ================
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001F9FF"
    "\U0001FA00-\U0001FA6F"
    "\U00002600-\U000027BF"
    "\U0001F000-\U0001F02F"
    "\U0001F0A0-\U0001F0FF"
    "\U0001F100-\U0001F1FF"
    "\U0001F200-\U0001F24F"
    "\U0001F250-\U0001F2FF"
    "\U0001F300-\U0001F5FF"
    "\U0001F600-\U0001F64F"
    "\U0001F650-\U0001F67F"
    "\U0001F680-\U0001F6FF"
    "\U0001F700-\U0001F77F"
    "\U0001F780-\U0001F7FF"
    "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF"
    "\U00002300-\U000023FF"
    "\U00002700-\U000027BF"
    "\U00002B00-\U00002BFF"
    "\U00002C60-\U00002C7F"
    "\U000025A0-\U000025FF"
    "]",
    flags=re.UNICODE,
)

_CONTROL_RE = re.compile(
    "[\U0000200B-\U0000200D\U0000FE00-\U0000FE0F\U00002060-\U00002064]",
    flags=re.UNICODE,
)


def _sanitize_text(text: str) -> str:
    """清理文本中的emoji和不可见特殊字符"""
    if not text:
        return ""
    cleaned = _EMOJI_RE.sub("", text)
    cleaned = _CONTROL_RE.sub("", cleaned)
    result_chars = []
    i = 0
    while i < len(cleaned):
        ch = cleaned[i]
        cp = ord(ch)
        if cp <= 0xFFFF:
            result_chars.append(ch)
        elif 0x10000 <= cp <= 0x1FFFF:
            result_chars.append(ch)
        i += 1
    result = "".join(result_chars)
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()


def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """加载系统字体"""
    if bold:
        font_paths = [
            "C:/Windows/Fonts/msyhbd.ttc",
            "C:/Windows/Fonts/msyh.ttc",
            "C:/Windows/Fonts/simhei.ttf",
        ]
    else:
        font_paths = [
            "C:/Windows/Fonts/msyh.ttc",
            "C:/Windows/Fonts/msyhbd.ttc",
            "C:/Windows/Fonts/simhei.ttf",
            "C:/Windows/Fonts/simsun.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
            "/System/Library/Fonts/PingFang.ttc",
        ]
    for path in font_paths:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    logger.warning("⚠️ 未找到中文字体")
    return ImageFont.load_default()


def _create_circular_avatar(
    source_path: Optional[str] = None,
    source_bytes: Optional[bytes] = None,
    size: int = AVATAR_SIZE,
    border: int = AVATAR_BORDER,
) -> Optional[Image.Image]:
    """创建带边框的圆形头像"""
    try:
        outer_size = size + border * 2

        # 加载源图片
        if source_bytes:
            img = Image.open(io.BytesIO(source_bytes)).convert("RGBA")
        elif source_path and os.path.exists(source_path):
            img = Image.open(source_path).convert("RGBA")
        else:
            logger.error("头像源不存在")
            return None

        w, h = img.size

        # 等比例缩放
        if w < h:
            new_w = size
            new_h = int(h * (size / w))
        else:
            new_h = size
            new_w = int(w * (size / h))
        img = img.resize((new_w, new_h), Image.LANCZOS)

        # 居中裁剪
        left = (new_w - size) // 2
        top = (new_h - size) // 2
        cropped = img.crop((left, top, left + size, top + size))

        # 创建圆形蒙版
        mask = Image.new("L", (size, size), 0)
        draw = ImageDraw.Draw(mask)
        draw.ellipse((0, 0, size, size), fill=255)

        # 应用蒙版得到圆形头像
        circle = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        circle.paste(cropped, (0, 0), mask)

        # 创建带边框的头像：先画外圈圆，再贴圆形头像
        result = Image.new("RGBA", (outer_size, outer_size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(result)
        # 画外圈边框圆
        draw.ellipse((0, 0, outer_size, outer_size), fill=COLOR_AVATAR_BORDER)
        # 贴入圆形头像（居中）
        result.paste(circle, (border, border), circle)

        return result
    except Exception as e:
        logger.error(f"创建圆形头像失败: {e}")
        return None


# 头像缓存 (URL -> 本地文件路径)
_avatar_cache: Dict[str, str] = {}


async def _download_and_cache_avatar(
    face_url: str,
    session,
    uid: str = "",
) -> Optional[str]:
    """下载UP主头像并缓存到本地"""
    if not face_url:
        return None

    cache_key = uid or face_url
    if cache_key in _avatar_cache:
        cached_path = _avatar_cache[cache_key]
        if os.path.exists(cached_path):
            return cached_path

    os.makedirs(AVATAR_CACHE_DIR, exist_ok=True)

    safe_uid = uid if uid else hashlib.md5(face_url.encode()).hexdigest()
    cache_path = os.path.join(AVATAR_CACHE_DIR, f"{safe_uid}.jpg")

    if os.path.exists(cache_path):
        mtime = os.path.getmtime(cache_path)
        if time.time() - mtime < 7 * 86400:
            _avatar_cache[cache_key] = cache_path
            return cache_path

    try:
        if session is None:
            return None
        headers = {"Referer": "https://space.bilibili.com/"}
        async with session.get(face_url, headers=headers) as resp:
            if resp.status != 200:
                logger.error(f"头像下载失败 HTTP {resp.status}: {face_url}")
                return None
            data = await resp.read()

        with open(cache_path, "wb") as f:
            f.write(data)

        _avatar_cache[cache_key] = cache_path
        logger.info(f"头像已缓存: {safe_uid}")
        return cache_path

    except Exception as e:
        logger.error(f"头像下载失败: {e}")
        return None


def _make_avatar_placeholder(size: int = AVATAR_SIZE) -> Image.Image:
    """生成一个占位头像（带边框）"""
    outer_size = size + AVATAR_BORDER * 2
    result = Image.new("RGBA", (outer_size, outer_size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(result)
    # 边框
    draw.ellipse((0, 0, outer_size, outer_size), fill=COLOR_AVATAR_BORDER)
    # 内部灰色
    inner_bbox = (AVATAR_BORDER, AVATAR_BORDER, outer_size - AVATAR_BORDER, outer_size - AVATAR_BORDER)
    draw.ellipse(inner_bbox, fill=(55, 58, 68, 255))
    # 简单表情
    cx = outer_size // 2
    cy = outer_size // 2
    r = size // 4
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(80, 85, 95, 255))
    return result


def _wrap_text(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> List[str]:
    """对中文文本进行换行处理"""
    if not text:
        return [""]

    paragraphs = text.split("\n")
    wrapped_lines = []

    for para in paragraphs:
        if not para:
            wrapped_lines.append("")
            continue

        current_line = ""
        for char in para:
            test_line = current_line + char
            bbox = font.getbbox(test_line)
            text_width = bbox[2] - bbox[0]

            if text_width > max_width and current_line:
                wrapped_lines.append(current_line)
                current_line = char
            else:
                current_line = test_line

        if current_line:
            wrapped_lines.append(current_line)

    return wrapped_lines


def _draw_rounded_image(
    canvas: Image.Image,
    img: Image.Image,
    x: int,
    y: int,
    radius: int = IMG_RADIUS,
):
    """在画布上绘制圆角图片"""
    w, h = img.size
    # 创建圆角蒙版
    mask = Image.new("L", (w, h), 0)
    mask_draw = ImageDraw.Draw(mask)
    # 画圆角矩形
    mask_draw.rectangle((radius, 0, w - radius, h), fill=255)
    mask_draw.rectangle((0, radius, w, h - radius), fill=255)
    mask_draw.pieslice((0, 0, radius * 2, radius * 2), 180, 270, fill=255)
    mask_draw.pieslice((w - radius * 2, 0, w, radius * 2), 270, 360, fill=255)
    mask_draw.pieslice((0, h - radius * 2, radius * 2, h), 90, 180, fill=255)
    mask_draw.pieslice((w - radius * 2, h - radius * 2, w, h), 0, 90, fill=255)

    # 如果原图是RGB，转RGBA以支持蒙版
    if img.mode != "RGBA":
        img_rgba = img.convert("RGBA")
    else:
        img_rgba = img

    # 用蒙版贴入
    canvas.paste(img_rgba, (x, y), mask)


async def _download_and_resize_image(
    url: str, session, target_width: int
) -> Optional[Tuple[Image.Image, int]]:
    """下载图片并等比缩放到目标宽度"""
    try:
        if session is None:
            return None
        async with session.get(url) as resp:
            if resp.status != 200:
                return None
            data = await resp.read()
            img = Image.open(io.BytesIO(data)).convert("RGB")
            w, h = img.size

            if w > target_width:
                ratio = target_width / w
                new_h = int(h * ratio)
                img = img.resize((target_width, new_h), Image.LANCZOS)
            elif w < target_width:
                ratio = target_width / w
                new_h = int(h * ratio)
                img = img.resize((target_width, new_h), Image.LANCZOS)
            else:
                new_h = h

            return (img, new_h)
    except Exception as e:
        logger.error(f"图片下载/缩放失败: {url}, 错误: {e}")
        return None


async def _prepare_images_grid(
    image_urls: List[str],
    session,
) -> Tuple[List[Tuple[Image.Image, int, int]], int]:
    """准备图片网格布局，返回 [(image, x, y), ...] 和总高度"""
    count = len(image_urls)
    if count == 0:
        return ([], 0)

    # 确定单张宽度和每行数量
    if count == 1:
        single_width = SINGLE_IMG_WIDTH
        per_row = 1
    elif count <= 4:
        single_width = MULTI_IMG_WIDTH
        per_row = 2
    else:
        single_width = MANY_IMG_WIDTH
        per_row = 3

    # 下载并缩放
    resized = []
    for url in image_urls:
        result = await _download_and_resize_image(url, session, single_width)
        if result:
            resized.append(result)

    if not resized:
        return ([], 0)

    # 计算网格位置
    placements = []
    current_y = 0
    row_height = 0

    for idx, (img, h) in enumerate(resized):
        col = idx % per_row
        if col == 0 and idx > 0:
            current_y += row_height + IMG_GAP
            row_height = 0

        # 居中对齐行
        row_count = min(per_row, len(resized) - (idx - col))
        row_total_width = row_count * single_width + (row_count - 1) * IMG_GAP
        start_x = PADDING + (CONTENT_WIDTH - row_total_width) // 2
        x = start_x + col * (single_width + IMG_GAP)

        placements.append((img, x, current_y))
        row_height = max(row_height, h)

    total_height = current_y + row_height
    return (placements, total_height)


async def async_compose_dynamic_image(
    author: str,
    text: str,
    url: str,
    image_urls: List[str],
    avatar_source_path: str,
    session,
    pub_time: str = "",
    avatar_face_url: str = "",
    avatar_uid: str = "",
) -> Optional[bytes]:
    """合成B站动态为一张美观的图片"""
    try:
        # 清理文本
        author = _sanitize_text(author) or "UP主"
        text = _sanitize_text(text)

        # 加载字体
        font_name = _load_font(FONT_NAME_SIZE, bold=True)
        font_text = _load_font(FONT_TEXT_SIZE)
        font_meta = _load_font(FONT_META_SIZE)

        # 下载UP主头像
        avatar_file = None
        if avatar_face_url and session:
            avatar_file = await _download_and_cache_avatar(
                avatar_face_url, session, avatar_uid
            )

        # 计算名称尺寸
        name_bbox = font_name.getbbox(author or "UP主")
        name_height = name_bbox[3] - name_bbox[1]
        name_width = name_bbox[2] - name_bbox[0]

        # 名称区域
        name_x = NAME_X
        name_y = HEADER_Y

        # 时间区域
        time_x = name_x + name_width + 18
        time_y = name_y + 6

        # 文案起始
        text_x = PADDING
        text_y = TEXT_START_Y

        # 包装文案
        wrapped_lines = _wrap_text(text or "", font_text, TEXT_MAX_WIDTH - 10)

        # 计算文案区域高度
        text_block_height = 0
        for line in wrapped_lines:
            bbox = font_text.getbbox(line)
            lh = bbox[3] - bbox[1]
            text_block_height += lh + LINE_SPACING
        text_block_height = max(text_block_height - LINE_SPACING, 0)

        # 计算图片区域
        img_y = text_y + text_block_height + IMG_OFFSET_Y

        # 准备图片网格
        placements, grid_height = await _prepare_images_grid(image_urls, session)

        # 计算总画布高度
        content_bottom = img_y + grid_height if grid_height > 0 else text_y + text_block_height
        total_height = content_bottom + PADDING
        total_height = max(total_height, 280)

        # 创建画布
        canvas = Image.new("RGB", (CANVAS_WIDTH, total_height), COLOR_BG)
        draw = ImageDraw.Draw(canvas)

        # 1. 绘制头像
        avatar = None
        if avatar_file:
            avatar = _create_circular_avatar(source_path=avatar_file)
        if not avatar:
            avatar = _create_circular_avatar(source_path=avatar_source_path)
        if avatar:
            avatar_x = AVATAR_X - AVATAR_BORDER
            avatar_y = AVATAR_Y - AVATAR_BORDER
            canvas.paste(avatar, (avatar_x, avatar_y), avatar)
        else:
            placeholder = _make_avatar_placeholder()
            canvas.paste(placeholder, (AVATAR_X - AVATAR_BORDER, AVATAR_Y - AVATAR_BORDER), placeholder)

        # 2. 绘制UP主名称
        draw.text((name_x, name_y), author or "UP主", font=font_name, fill=COLOR_NAME)

        # 3. 绘制时间（带小圆点前缀）
        if pub_time:
            # 小圆点
            dot_x = time_x
            dot_y = time_y + FONT_META_SIZE // 2
            draw.ellipse((dot_x, dot_y - 3, dot_x + 6, dot_y + 3), fill=COLOR_DOT)
            # 时间文字
            draw.text((dot_x + 12, time_y), pub_time, font=font_meta, fill=COLOR_TIME)

        # 4. 绘制文案
        current_y = text_y
        for line in wrapped_lines:
            if line:
                draw.text((text_x, current_y), line, font=font_text, fill=COLOR_TEXT)
            bbox = font_text.getbbox(line)
            lh = bbox[3] - bbox[1]
            current_y += lh + LINE_SPACING

        # 5. 绘制图片（带圆角）
        for img, px, py in placements:
            _draw_rounded_image(canvas, img, px, img_y + py)

        # 转为PNG字节
        buf = io.BytesIO()
        canvas.save(buf, format="PNG", quality=95)
        return buf.getvalue()

    except Exception as e:
        logger.error(f"合成动态图片失败: {e}")
        import traceback
        traceback.print_exc()
        return None


def get_default_avatar_path() -> str:
    """获取默认头像路径"""
    return os.path.join(os.path.dirname(__file__), "assets", "avatar_source.png")
