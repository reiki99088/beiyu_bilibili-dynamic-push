# Bilibili 动态 / 直播监控插件

> 适配 **MaiBot** 的 Bilibili UP 主动态与直播监控插件，支持动态合成图片推送、视频动态下载发送、多群订阅、合并转发防刷屏，以及防风控的随机轮询机制。
>
> 作者: 北遇 (Beiyu) | 原作者: 白狐whitefox

---

## 注意事项

不要使用独立的 Napcat_Adapter！请去插件市场安装 Napcat_Adapter 适配器。

---

## 功能特性

### 动态推送
- 实时监控 UP 主发布的 **图文动态、视频投稿、专栏文章** 等。
- **合成图片推送**：将 UP 主头像、名称、发布时间、动态文案和图片合成为一张精美图片发送，包含圆形头像边框、圆角图片网格布局。
- **原始图片附送**：非视频动态在发送合成图片后，额外发送动态中的原始图片。
- 支持 **转发动态**，自动显示原作者及原始内容。
- **置顶识别**：可识别并推送新增置顶动态，旧置顶不会重复推送。
- **过期过滤**：超过 `max_dynamic_age` 的旧动态不再推送（避免重启后推送积压消息）。
- **开奖过滤**：可自动丢弃开奖类动态。

### 视频动态处理
- 自动识别视频类型动态（含视频投稿）。
- 下载视频 DASH 流（视频+音频），使用 **ffmpeg** 合成为 MP4 文件。
- 视频以本地文件路径方式发送，不受 WebSocket 帧大小限制。
- 视频处理在后台异步执行，不阻塞命令响应。
- 发送完成后自动清理临时文件。

### 直播通知
- **开播提醒**：包含封面、标题、跳转链接和开播时间。
- **下播提醒**：自动统计本次直播时长。

### 粉丝里程碑
- 自动记录粉丝数突破 1 万 / 10 万 / 100 万 等里程碑并推送。
- 支持 `/bili fans <UID>` 手动查询当前粉丝数与下一个里程碑差距。

### 防风控机制
- 支持 **轮询抖动**，模拟真人查询行为，降低被风控概率。
- 内置 **凭证自动刷新**（每 6 小时检测一次，需配置 `ac_time_value`）。

### 在线管理
- 支持通过群聊指令进行 **订阅增删查**，管理员限定。

---

## 安装依赖

```bash
pip install bilibili-api-python aiohttp Pillow
```

> 视频动态处理需要系统安装 **ffmpeg** 并加入 PATH。

---

## 配置说明

插件首次运行会自动生成配置模板。推荐通过 **WebUI** 直接编辑；如需手改 `config.toml`，参考下面的字段说明。

```toml
# 1. 插件开关
[plugin]
enabled = true                 # 插件总开关

# 2. Cookie 凭证
#    获取方式：浏览器登录 B 站 → F12 → Application → Cookies → bilibili.com
[credential]
sessdata      = ""             # 必填：SESSDATA
bili_jct      = ""             # 必填：bili_jct（32 位 hex）
buvid3        = ""             # 选填：部分接口需要，填上更稳
dedeuserid    = ""             # 选填：你的 B 站 UID
ac_time_value = ""             # 选填：配置后插件可自动刷新 Cookie，避免过期

# 3. 运行参数
[settings]
poll_interval   = 120          # 轮询基准间隔（秒），建议 ≥ 60
poll_jitter     = 10           # 轮询抖动秒数，实际间隔 = base ± jitter
admin_qqs       = ["114514"]   # 管理员 QQ 号列表
max_images      = 3            # 单条动态最大同消息图片数，超过则合并转发
ignore_lottery  = true         # 自动丢弃开奖类动态
max_dynamic_age = 3600         # 动态最大有效时长（秒），过老的不再推送
auto_like       = false        # 开启后自动点赞新动态（谨慎使用！这玩意儿太容易风控了）
skip_forward    = true         # 过滤转发类型动态

# 4. 订阅列表（每行一组：UID => 群号1, 群号2）
#    支持的分隔符：=>  ->  :  ：  |
#    群号之间支持：半角逗号 / 全角逗号 / 空格
[subscriptions]
users = [
    "114514 => 1919810, 123456",
    "36081646 => 12345678",
]
```

> 如果觉得手动配置 `users` 麻烦，可以留空，进群后用 `/bili add <UID>` 指令进行订阅。
>
> `SESSDATA` 中常见的 `%2C`、`%2A` 等是 URL 编码，**直接粘贴即可**，插件会自动解码。

### Cookie 字段说明

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `sessdata` | 是 | 登录态核心 Cookie，缺失将无法访问受限接口 |
| `bili_jct` | 是 | CSRF 校验值，写操作必需 |
| `buvid3` | 否 | 部分风控接口需要 |
| `dedeuserid` | 否 | 即你的 B 站 UID |
| `ac_time_value` | 否 | 自动刷新 Cookie 所需的凭据，强烈建议填写 |

---

## 指令列表

> 所有指令仅限 **管理员（`admin_qqs`）** 在 **群聊** 中使用。

| 指令 | 说明 |
| --- | --- |
| `/bili help` | 查看帮助信息 |
| `/bili start` | 启动监控 |
| `/bili stop` | 停止监控 |
| `/bili status` | 查看运行状态及订阅数 |
| `/bili add <UID>` | 在当前群订阅指定 UP 主 |
| `/bili remove <UID>` | 在当前群移除指定订阅（仅可移除指令添加的） |
| `/bili list` | 列出当前群所有订阅 |
| `/bili info <UID>` | 查询该 UP 主当前直播状态（极容易风控，不建议使用） |
| `/bili <UID>` | 推送一条测试动态到当前群（容易风控，请降低使用频率） |
| `/bili fans <UID>` | 查询该 UP 主当前粉丝数量（较容易风控，请注意使用频率） |

---

## 文件结构

```
beiyu_bilibili-dynamic-push/
├── __init__.py              # 包入口
├── plugin.py                # 插件主类与命令注册
├── config.py                # 配置模型（pydantic / WebUI schema）
├── monitor.py               # 核心监控逻辑（动态 / 直播 / 粉丝 / 视频处理）
├── commands.py              # /bili 系列指令实现
├── subscription.py          # 订阅管理器（静态 + 动态）
├── utils.py                 # 工具函数（图片转码、历史持久化等）
├── image_composer.py        # 动态合成图片生成器（头像+文案+图片网格）
├── _manifest.json           # 插件清单文件
├── requirements.txt         # Python 依赖
├── assets/                  # 静态资源
│   ├── bilibili.png         # 插件图标
│   ├── avatar_source.png    # 默认头像源图
│   └── avatar_cache/        # UP 主头像缓存目录（自动生成）
├── config.toml              # [自动生成] 配置文件
├── history.json             # [自动生成] 动态/直播/粉丝历史记录，用于去重
└── subscriptions.json       # [自动生成] 指令添加的订阅信息
```

> `history.json` 与 `subscriptions.json` 由插件自动维护，**请勿手动修改**。

---

## 注意事项

1. **风控建议**：尽管已加入抖动机制，仍建议 `poll_interval >= 60`，避免 API 被封禁。
2. **凭证维护**：插件内置每 6 小时自动刷新凭证（需要 `ac_time_value`）；若 Cookie 彻底失效，需手动更新配置。
3. **首次订阅**：新订阅 UID 时，第一次轮询会以当前最新动态作为基准，**不推送历史动态**；粉丝里程碑同理。
4. **请勿手动修改** `history.json` 和 `subscriptions.json`，否则可能导致重复推送或订阅异常。
5. **固定订阅 vs 动态订阅**：
   - 配置文件中的订阅为「固定订阅」，**不可通过指令移除**。
   - 通过 `/bili add` 添加的为「动态订阅」，可通过 `/bili remove` 移除。
6. **视频动态**：需要系统安装 ffmpeg，否则视频合成功能不可用。视频在后台异步处理，不会阻塞命令响应。
7. **图片发送**：合成图片和原始图片均通过本地文件路径发送，不受 WebSocket 帧大小限制。

---

## License

[MIT](./LICENSE)
