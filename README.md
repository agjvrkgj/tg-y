# TG 频道视频搬运工具

监控一个或多个 Telegram 源频道，当出现**新视频**时，自动把视频搬运（复制或转发）到你的目标频道。

基于 [Telethon](https://docs.telethon.dev/)（MTProto），使用**个人账号**登录，因此可以监控任何你账号能看到的频道，无需成为对方频道的管理员。

---

## 功能

- 监听多个源频道的新消息，只搬运**视频**（自动忽略图片、文字、视频留言等）。
- 两种搬运模式：
  - `copy`（默认）：重新发送视频，**不带「转发自」标签**，看起来像原创。
  - `forward`：直接转发，保留来源标签。
- 可选搬运启动时的「最近历史视频」（`BACKFILL_LIMIT`）。
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
# 1. 安装依赖（建议用虚拟环境）
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 2. 准备配置
cp .env.example .env
# 然后编辑 .env 填入你的值
```

### `.env` 配置说明

| 变量 | 说明 |
|------|------|
| `API_ID` / `API_HASH` | 上一步从 my.telegram.org 获取 |
| `SESSION_NAME` | 会话文件名，默认 `forwarder` |
| `SOURCE_CHANNELS` | 源频道，多个用英文逗号分隔。支持 `@用户名`、数字ID（`-1001234567890`）、`t.me/xxx` 链接 |
| `TARGET_CHANNEL` | 目标频道 |
| `MODE` | `copy`（默认，去来源标签）或 `forward`（保留来源标签） |
| `KEEP_CAPTION` | 是否保留原文案，仅 `copy` 模式生效 |
| `BACKFILL_LIMIT` | 启动时每个源频道搬运的历史视频条数，`0` = 只监控新视频 |
| `SEND_DELAY` | 每条搬运之间的延迟秒数，建议 2~5，防限流 |

---

## 三、运行

```bash
python forwarder.py
```

- **首次运行**会提示输入手机号、Telegram 发来的验证码（如开了两步验证还需输入密码）。
  登录成功后会生成 `forwarder.session` 文件，之后运行不再需要登录。
- 看到「开始监控，等待新视频…」后即生效。源频道一发新视频就会自动搬运。
- 按 `Ctrl+C` 退出。

### 长期后台运行（可选）

用 `nohup` 或 `screen`/`tmux`：

```bash
nohup python forwarder.py > forwarder.log 2>&1 &
```

或写成 systemd 服务长期托管。

---

## 四、注意事项

- ⚠️ **`.env` 和 `*.session` 等同于你的账号凭证，切勿上传到 GitHub 或分享给他人**（本项目已在 `.gitignore` 中排除）。
- 搬运他人内容请遵守版权及 Telegram 服务条款，自行承担相应责任。
- 大量、高频搬运可能触发 Telegram 限流甚至风控，请适当调大 `SEND_DELAY`。
- `copy` 模式会以你的账号重新发送媒体；超大视频可能需要先下载再上传，耗时较长。

---

## 文件结构

```
.
├── forwarder.py      # 主程序：监听 + 搬运逻辑
├── config.py         # 读取并校验 .env 配置
├── requirements.txt  # 依赖
├── .env.example      # 配置示例（复制为 .env 使用）
└── .gitignore
```
