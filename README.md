# TG 频道视频搬运工具

监控一个或多个 Telegram 源频道，当出现**新视频**时，自动把视频搬运（复制或转发）到你的目标频道。
提供 **Web 管理界面**，也支持纯命令行运行。

基于 [Telethon](https://docs.telethon.dev/)（MTProto），使用**个人账号**登录，因此可以监控任何你账号能看到的频道，无需成为对方频道的管理员。

---

## 功能

- 🖥️ **Web 管理面板**：浏览器里配置频道、登录账号、一键启停、查看状态与实时日志。
- 🔒 **面板访问密码**：可用环境变量 `WEB_PASSWORD` 或在面板内设置，基于 Cookie 会话。
- 👥 **多账号**：可添加任意多个 Telegram 账号，每个账号独立登录、独立配置源/目标频道、独立启停与日志。
- 监听多个源频道的新消息，只搬运**视频**（自动忽略图片、文字、视频留言等）。
- 两种搬运模式：
  - `copy`（默认）：重新发送视频，**不带「转发自」标签**，看起来像原创。
  - `forward`：直接转发，保留来源标签。
- 可选搬运启动时的「最近历史视频」（回填）。
- 自动处理 Telegram 限流（FloodWait）并重试，可配置发送间隔。
- 简单去重，避免同一条消息被重复搬运。

---

## 一、准备工作

### 1. 获取 API_ID / API_HASH

1. 打开 https://my.telegram.org 并用你的手机号登录。
2. 进入 **API development tools**，创建一个应用。
3. 记下 **api_id**（数字）和 **api_hash**（字符串）。

### 2. 确认频道权限

- **源频道**：你的账号需要能看到它（公开频道直接搜，私有频道需先加入）。
- **目标频道**：你需要有发消息的权限（通常是你自己建的频道；如果是频道，请把账号设为管理员或拥有者）。

---

## 二、安装

需要 Python 3.10+。

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

---

## 三、方式一：Web 管理界面（推荐）

```bash
python web.py
```

然后浏览器打开 **http://127.0.0.1:8000** ，按页面操作：

0. （可选但推荐）**设置面板密码**：启动前设环境变量 `WEB_PASSWORD=你的密码`，或进入面板后点右上角「设置密码」。设置后访问面板需先输入密码。
1. 点左侧「＋ 添加账号」创建一个账号（可添加多个）。选中某账号后：
2. 在「配置」卡片填入 **API ID / API Hash**、**源频道**（每行一个）、**目标频道**、搬运模式等，点「保存配置」。
3. 在「账号登录」卡片填手机号 → 点「发送验证码」→ 输入验证码（如开了两步验证再填密码）→ 点「登录」。
4. 点「▶ 启动监控」。之后该账号的源频道一发新视频就会自动搬运。每个账号互相独立。
5. 「运行日志」实时刷新；随时可点「■ 停止」或在左侧切换其它账号。

> 想让局域网/服务器其它设备访问，可设环境变量：`WEB_HOST=0.0.0.0 WEB_PORT=8000 python web.py`。
> 暴露到公网时请自行加反向代理与鉴权，因为面板未内置密码保护。

Web 界面保存的配置会写入 `settings.json`（含密钥，已在 `.gitignore` 中排除）。

---

## 四、方式二：命令行运行

不想用网页也可以。两种配置来源（`settings.json` 优先，其次 `.env`）：

```bash
cp .env.example .env     # 然后编辑 .env 填入你的值
python forwarder.py
```

首次运行会在终端提示输入手机号、验证码完成登录，生成 `*.session` 文件，之后无需再登录。

### `.env` / 配置项说明

| 变量 | 说明 |
|------|------|
| `API_ID` / `API_HASH` | 从 my.telegram.org 获取 |
| `SESSION_NAME` | 会话文件名，默认 `forwarder` |
| `SOURCE_CHANNELS` | 源频道，多个用英文逗号分隔。支持 `@用户名`、数字ID（`-1001234567890`）、`t.me/xxx` 链接 |
| `TARGET_CHANNEL` | 目标频道 |
| `MODE` | `copy`（默认，去来源标签）或 `forward`（保留来源标签） |
| `KEEP_CAPTION` | 是否保留原文案，仅 `copy` 模式生效 |
| `BACKFILL_LIMIT` | 启动时每个源频道搬运的历史视频条数，`0` = 只监控新视频 |
| `SEND_DELAY` | 每条搬运之间的延迟秒数，建议 2~5，防限流 |

---

## 五、长期后台运行（可选）

```bash
nohup python web.py > web.log 2>&1 &
```

或写成 systemd 服务长期托管。

---

## 六、注意事项

- ⚠️ **`.env`、`settings.json`、`*.session` 都等同于你的账号凭证，切勿上传到 GitHub 或分享给他人**（已在 `.gitignore` 中排除）。
- Web 面板默认无登录鉴权，请勿在不可信网络中直接暴露公网。
- 搬运他人内容请遵守版权及 Telegram 服务条款，自行承担相应责任。
- 大量、高频搬运可能触发 Telegram 限流甚至风控，请适当调大 `SEND_DELAY`。
- `copy` 模式会以你的账号重新发送媒体；超大视频可能需要先下载再上传，耗时较长。

---

## 文件结构

```
.
├── web.py             # Web 管理服务（FastAPI）
├── forwarder.py       # 核心搬运服务 ForwarderService（也可命令行直接运行）
├── config.py          # 设置存储（settings.json 优先，.env 回退）
├── static/
│   └── index.html     # Web 管理面板（单页）
├── requirements.txt   # 依赖
├── .env.example       # 命令行配置示例
└── .gitignore
```
