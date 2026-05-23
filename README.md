# Telegraph Downloader

从 Telegram 评论或直接的 telegra.ph 链接抓取页面内图片并打包为 ZIP 的小工具。

## 要求

- Python 3.8+
- 依赖见 `requirements.txt`（建议在 virtualenv 中安装）

安装依赖：

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 配置

推荐使用 `.env` 或环境变量配置：仓库包含一个示例文件 [`.env.example`](.env.example).

支持的环境变量（或对应的 CLI 参数）：

- `TELEGRAM_API_ID` — Telegram API ID（仅在扫描频道时需要）
- `TELEGRAM_API_HASH` — Telegram API hash（仅在扫描频道时需要）
- `TELEGRAM_CHANNEL` — 要扫描的频道名或 invite handle
- `TG_PROXY_URL` — 可选，Telegram 连接代理（例如 `socks5://127.0.0.1:1080`）
- `WEB_PROXY_URL` — 可选，用于 HTTP 请求的代理

示例 `.env`（不要把真实密钥提交到仓库）：

```ini
TELEGRAM_API_ID=
TELEGRAM_API_HASH=
TELEGRAM_CHANNEL=
# 可选代理
TG_PROXY_URL=
WEB_PROXY_URL=
```

仓库已将 `.env` 列入 `.gitignore`，请不要将包含密钥的 `.env` 推上远程。

## 用法

在本仓库根目录运行：

- 直接下载单个 Telegraph 页面（跳过 Telegram）：

```bash
python telegraph_downloader.py --link "https://telegra.ph/Example-Title-01-01" \
  --output-dir downloads --timeout 20 --workers 6
```

- 扫描频道评论并下载链接（需要 API ID/Hash 与 channel）：

```bash
python telegraph_downloader.py --api-id 12345 --api-hash abcd1234 \
  --channel my_channel --limit 50 --timeout 20 --workers 6
```

可选参数包括 `--tg-proxy` 与 `--web-proxy`，分别用于 Telegram 连接与网页抓取。

## 特性与实现细节

- 支持解析消息中的显式 URL、实体内联链接与按钮 URL。
- 对非 telegra.ph 链接会尝试跟随重定向（HEAD/GET），以捕获短链（例如 t.me 按钮）最终跳转到 telegra.ph/graph.org 的情况。
- 下载时支持断点续传：临时文件保存在输出目录的 `.partial/<link_key>/`，下载完成后打包为 ZIP 并清理临时目录。
- 已使用 SQLite 记录处理过的链接（默认 `state/processed.sqlite3`），避免重复下载。

## 调试与测试

- 若要手动测试某个短链是否被解析为 telegra.ph，可以使用 `--link` 模式配合该短链：

```bash
python telegraph_downloader.py --link "https://t.me/some_short_link" 
```

（脚本会尝试跟随重定向并在最终目标为 telegra.ph 时下载图片）

## 推送到仓库

创建、提交并推送 README：

```bash
git add README.md
git commit -m "Add README"
git push
```

---

如果你希望我把当前 shell 环境里存在的变量写入一个本地 `.env` 文件（注意这会把真实值写入磁盘），回复“创建实际 .env”；或者我可以只在仓库中创建一个空 ` .env` 模板。也可以让我把 README 做成英文版或补充更多运行示例。

# Telegraph Comment Image Downloader

从 Telegram 频道评论中提取 `telegra.ph` 链接，解析文章图片并打包为 ZIP。

## 功能

- 扫描频道最近 N 条帖子（默认 50）。
- 只处理有评论的帖子，提取评论中的 `telegra.ph` 链接。
- 下载文章中的全部图片并打包为 ZIP。
- 用 SQLite 记录已处理链接，支持增量运行（避免重复下载）。
- 支持 Telegram 连接代理（`--tg-proxy`）和网页请求代理（`--web-proxy`）。

## 安装

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 获取 Telegram API 凭证

1. 打开 `https://my.telegram.org`。
2. 登录账号并创建应用。
3. 获取 `api_id` 和 `api_hash`。

首次运行会让你输入手机号和验证码，Telethon 会在本地生成会话文件（`.session`）。

## 运行方式

### 1) 直接命令行参数

```bash
python telegraph_downloader.py \
  --api-id 123456 \
  --api-hash "your_api_hash" \
  --channel "your_channel_username" \
  --limit 100
```

### 2) 环境变量

```bash
export TELEGRAM_API_ID="123456"
export TELEGRAM_API_HASH="your_api_hash"
export TELEGRAM_CHANNEL="your_channel_username"
python telegraph_downloader.py
```

## 代理配置（你这个场景重点）

### Telegram 连接走代理

```bash
python telegraph_downloader.py \
  --api-id 123456 \
  --api-hash "your_api_hash" \
  --channel "your_channel_username" \
  --tg-proxy "socks5://127.0.0.1:1080"
```

支持：`socks5://`、`socks4://`、`http://`、`https://`

带认证示例：

```bash
--tg-proxy "socks5://user:pass@127.0.0.1:1080"
```

也可以用环境变量：

```bash
export TG_PROXY_URL="socks5://127.0.0.1:1080"
```

### Telegraph 和图片下载走代理（可选）

```bash
python telegraph_downloader.py \
  --api-id 123456 \
  --api-hash "your_api_hash" \
  --channel "your_channel_username" \
  --web-proxy "http://127.0.0.1:7890"
```

也可以设置：

```bash
export WEB_PROXY_URL="http://127.0.0.1:7890"
```

如果不设置 `--web-proxy`，脚本会自动尝试读取系统环境变量 `HTTPS_PROXY` / `HTTP_PROXY`。

## 常用参数

- `--limit`: 扫描最近多少条帖子。
- `--output-dir`: ZIP 输出目录（默认 `downloads`）。
- `--db-path`: 增量状态数据库（默认 `state/processed.sqlite3`）。
- `--session-name`: Telethon 会话文件名（默认 `telegraph_downloader`）。
- `--timeout`: 网页和图片下载超时秒数（默认 20）。
- `--workers`: 图片并行下载线程数（默认 6）。
- `--log-level`: 日志级别（`DEBUG/INFO/WARNING/ERROR`）。

## 日志和并行下载示例

```bash
python telegraph_downloader.py \
  --api-id 123456 \
  --api-hash "your_api_hash" \
  --channel "your_channel_username" \
  --limit 300 \
  --workers 10 \
  --log-level INFO
```

运行时会输出：

- 抓到的每个 Telegraph 链接。
- 每个链接解析出的图片 URL。
- 每张图片的下载开始/成功/失败日志。
- ZIP 打包进度日志。

## 输出说明

- ZIP 文件保存到 `downloads/`（可通过参数修改）。
- 文件名格式：`文章标题_帖子ID_评论ID.zip`。
- 已处理链接记录在 SQLite 里，重复运行会自动跳过已处理链接。

## 持久化运行（Linux 推荐）

推荐用 `systemd --user` + `timer`，支持开机后自动定时执行、崩溃后可重启、日志可集中查看。

### 1) 先手动跑通一次

首次运行 Telethon 需要输入验证码，先在终端手动执行一次，确认会话文件已生成。

### 2) 创建环境变量文件

创建 `~/.config/telegraph-downloader.env`：

```bash
TELEGRAM_API_ID=123456
TELEGRAM_API_HASH=your_hash
TELEGRAM_CHANNEL=your_channel
```

### 3) 创建用户服务

创建 `~/.config/systemd/user/telegraph-downloader.service`：

```ini
[Unit]
Description=Telegraph Downloader Job
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=/home/yourname/dev/telegraph-downloader
EnvironmentFile=%h/.config/telegraph-downloader.env
ExecStart=/home/yourname/dev/telegraph-downloader/.venv/bin/python /home/yourname/dev/telegraph-downloader/telegraph_downloader.py --limit 300 --workers 6
```

### 4) 创建定时器

创建 `~/.config/systemd/user/telegraph-downloader.timer`：

```ini
[Unit]
Description=Run Telegraph Downloader every 10 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=10min
Persistent=true

[Install]
WantedBy=timers.target
```

### 5) 启动 / 查看 / 停止

```bash
systemctl --user daemon-reload
systemctl --user enable --now telegraph-downloader.timer
systemctl --user list-timers telegraph-downloader.timer
journalctl --user -u telegraph-downloader.service -f
```

停止并移除：

```bash
systemctl --user disable --now telegraph-downloader.timer
```

如果是服务器无人值守场景，建议额外执行：

```bash
sudo loginctl enable-linger $USER
```

### 6) 常见持久化场景

- 按日期跑一次：在 `ExecStart` 后追加 `--date 2026-04-07`，任务完成后退出。
- 单链接抓取：在 `ExecStart` 后追加 `--link https://telegra.ph/pp-09-03-27`，抓完即退出。
- 长期巡检：不加 `--date` / `--link`，由 timer 周期执行。

## 持久化运行（macOS）

推荐用 `launchd`，支持开机自启、后台定时执行、自动写日志。

### 1) 先手动跑通一次

首次运行 Telethon 需要输入验证码，先在终端手动执行一次，确认会话文件已生成。

### 2) 创建 LaunchAgent

在 `~/Library/LaunchAgents/com.telegraph.downloader.plist` 写入（把路径和参数改成你自己的）：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.telegraph.downloader</string>

  <key>ProgramArguments</key>
  <array>
    <string>/bin/zsh</string>
    <string>-lc</string>
    <string>cd /Users/yourname/dev/telegraph-downloader &amp;&amp; source .venv/bin/activate &amp;&amp; TELEGRAM_API_ID=123456 TELEGRAM_API_HASH=your_hash TELEGRAM_CHANNEL=your_channel python telegraph_downloader.py --limit 300 --workers 6</string>
  </array>

  <key>WorkingDirectory</key>
  <string>/Users/yourname/dev/telegraph-downloader</string>

  <key>RunAtLoad</key>
  <true/>

  <key>StartInterval</key>
  <integer>600</integer>

  <key>StandardOutPath</key>
  <string>/Users/yourname/dev/telegraph-downloader/logs/launchd.out.log</string>

  <key>StandardErrorPath</key>
  <string>/Users/yourname/dev/telegraph-downloader/logs/launchd.err.log</string>
</dict>
</plist>
```

`StartInterval=600` 表示每 10 分钟跑一次。脚本有 SQLite 增量状态，不会重复下载已处理链接。

### 3) 启动 / 查看 / 停止

```bash
mkdir -p ~/Library/LaunchAgents /Users/yourname/dev/telegraph-downloader/logs
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.telegraph.downloader.plist
launchctl enable gui/$(id -u)/com.telegraph.downloader
launchctl kickstart -k gui/$(id -u)/com.telegraph.downloader
```

查看状态：

```bash
launchctl print gui/$(id -u)/com.telegraph.downloader
tail -f /Users/yourname/dev/telegraph-downloader/logs/launchd.out.log /Users/yourname/dev/telegraph-downloader/logs/launchd.err.log
```

停止并移除：

```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.telegraph.downloader.plist
```

### 4) 常见持久化场景

- 按日期跑一次：直接加 `--date 2026-04-07`，任务完成后会退出（适合一次性补档）。
- 单链接抓取：直接加 `--link https://telegra.ph/pp-09-03-27`，抓完即退出。
- 长期巡检：不加 `--date` / `--link`，配合 `StartInterval` 周期执行。

## 中断续跑（已支持）

- 下载过程中如果中断（例如断网、手动停止、机器重启），下次运行会自动续跑。
- 脚本会把每个链接的图片先保存到 `downloads/.partial/<link_hash>/`。
- 重跑时会跳过已下载完成的图片，只补缺失图片；全部下载成功后再打包 ZIP。
- ZIP 生成成功后，会自动清理对应 `.partial` 临时目录。

## 注意事项

- 评论读取依赖频道已绑定讨论组。
- 首次登录需要人机交互（短信/验证码）。
- 请遵守频道内容和版权规定，仅在授权范围内下载。
