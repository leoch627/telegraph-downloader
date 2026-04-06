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
- `--log-level`: 日志级别（`DEBUG/INFO/WARNING/ERROR`）。

## 输出说明

- ZIP 文件保存到 `downloads/`（可通过参数修改）。
- 文件名格式：`文章标题_帖子ID_评论ID.zip`。
- 已处理链接记录在 SQLite 里，重复运行会自动跳过已处理链接。

## 注意事项

- 评论读取依赖频道已绑定讨论组。
- 首次登录需要人机交互（短信/验证码）。
- 请遵守频道内容和版权规定，仅在授权范围内下载。
