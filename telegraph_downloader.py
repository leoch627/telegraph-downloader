#!/usr/bin/env python3
import argparse
import asyncio
import logging
import re
import sqlite3
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urljoin, urlparse

import requests
import socks
from bs4 import BeautifulSoup
from telethon import TelegramClient
from telethon.tl.types import MessageEntityTextUrl, MessageEntityUrl

TELEGRAPH_PATTERN = re.compile(r"https?://(?:telegra\.ph|graph\.org)/\S+", re.IGNORECASE)
TRAILING_PUNCTUATION = ".,;:!?)]}\"'"
VALID_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
ALLOWED_TELEGRAPH_HOSTS = {"telegra.ph", "graph.org", "www.telegra.ph", "www.graph.org"}


@dataclass
class AppConfig:
    api_id: int
    api_hash: str
    channel: str
    session_name: str
    output_dir: Path
    db_path: Path
    post_limit: int
    request_timeout: int
    tg_proxy_url: Optional[str]
    web_proxy_url: Optional[str]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download images from telegra.ph links found in Telegram channel comments and pack into ZIP files."
    )
    parser.add_argument("--api-id", default="", help="Telegram API ID, or env TELEGRAM_API_ID")
    parser.add_argument("--api-hash", default="", help="Telegram API hash, or env TELEGRAM_API_HASH")
    parser.add_argument("--channel", default="", help="Channel username or invite handle, or env TELEGRAM_CHANNEL")
    parser.add_argument("--session-name", default="telegraph_downloader", help="Telethon session name")
    parser.add_argument("--output-dir", default="downloads", help="Directory for generated ZIP files")
    parser.add_argument("--db-path", default="state/processed.sqlite3", help="SQLite path for incremental state")
    parser.add_argument("--limit", type=int, default=50, help="How many recent channel posts to scan")
    parser.add_argument("--timeout", type=int, default=20, help="HTTP timeout for telegra.ph/image downloads")
    parser.add_argument(
        "--tg-proxy",
        default="",
        help=(
            "Proxy URL for Telegram connection, for example "
            "socks5://127.0.0.1:1080 or http://127.0.0.1:7890"
        ),
    )
    parser.add_argument(
        "--web-proxy",
        default="",
        help="Optional proxy URL for telegra.ph/image HTTP requests",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log verbosity",
    )

    args = parser.parse_args()

    args.api_id = args.api_id or ("" if not (v := getenv("TELEGRAM_API_ID")) else v)
    args.api_hash = args.api_hash or getenv("TELEGRAM_API_HASH", "")
    args.channel = args.channel or getenv("TELEGRAM_CHANNEL", "")
    args.tg_proxy = args.tg_proxy or getenv("TG_PROXY_URL", "")
    args.web_proxy = (
        args.web_proxy
        or getenv("WEB_PROXY_URL", "")
        or getenv("HTTPS_PROXY", "")
        or getenv("HTTP_PROXY", "")
    )

    if not args.api_id:
        parser.error("Missing API ID. Set --api-id or TELEGRAM_API_ID.")
    if not args.api_hash:
        parser.error("Missing API hash. Set --api-hash or TELEGRAM_API_HASH.")
    if not args.channel:
        parser.error("Missing channel. Set --channel or TELEGRAM_CHANNEL.")

    try:
        args.api_id = int(args.api_id)
    except ValueError as exc:
        raise SystemExit("API ID must be an integer.") from exc

    if args.limit <= 0:
        parser.error("--limit must be > 0")
    if args.timeout <= 0:
        parser.error("--timeout must be > 0")

    return args


def getenv(name: str, default: str = "") -> str:
    import os

    return os.getenv(name, default).strip()


def build_telethon_proxy(proxy_url: Optional[str]):
    if not proxy_url:
        return None

    parsed = urlparse(proxy_url)
    scheme = parsed.scheme.lower()
    host = parsed.hostname

    if not host:
        raise ValueError(f"Invalid proxy URL (missing host): {proxy_url}")

    if scheme in {"socks5", "socks5h"}:
        proxy_type = socks.SOCKS5
        default_port = 1080
    elif scheme == "socks4":
        proxy_type = socks.SOCKS4
        default_port = 1080
    elif scheme in {"http", "https"}:
        proxy_type = socks.HTTP
        default_port = 8080
    else:
        raise ValueError(
            "Unsupported TG proxy scheme. Use socks5/socks4/http/https, "
            f"got: {scheme}"
        )

    port = parsed.port or default_port
    username = unquote(parsed.username) if parsed.username else None
    password = unquote(parsed.password) if parsed.password else None

    return (proxy_type, host, port, True, username, password)


def create_http_session(proxy_url: Optional[str]) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "telegraph-downloader/1.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
    )
    if proxy_url:
        session.proxies.update({"http": proxy_url, "https": proxy_url})
    return session


def ensure_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS processed_links (
            link TEXT PRIMARY KEY,
            post_id INTEGER NOT NULL,
            comment_id INTEGER NOT NULL,
            zip_path TEXT,
            processed_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()
    return conn


def is_processed(conn: sqlite3.Connection, link: str) -> bool:
    row = conn.execute("SELECT 1 FROM processed_links WHERE link = ? LIMIT 1", (link,)).fetchone()
    return row is not None


def mark_processed(
    conn: sqlite3.Connection,
    link: str,
    post_id: int,
    comment_id: int,
    zip_path: str,
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO processed_links(link, post_id, comment_id, zip_path)
        VALUES (?, ?, ?, ?)
        """,
        (link, post_id, comment_id, zip_path),
    )
    conn.commit()


def sanitize_filename(name: str, max_len: int = 80) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    if not sanitized:
        return "telegraph"
    return sanitized[:max_len]


def normalize_telegraph_url(url: str) -> str:
    trimmed = url.rstrip(TRAILING_PUNCTUATION)
    parsed = urlparse(trimmed)
    if not parsed.scheme or not parsed.netloc:
        return trimmed

    # Normalize hostname case so de-dup is stable.
    normalized_netloc = parsed.netloc.lower()
    return parsed._replace(netloc=normalized_netloc).geturl()


def is_supported_telegraph_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    return parsed.scheme in {"http", "https"} and host in ALLOWED_TELEGRAPH_HOSTS


def extract_telegraph_links(text: str) -> list[str]:
    seen = set()
    result = []

    for match in TELEGRAPH_PATTERN.findall(text):
        normalized = normalize_telegraph_url(match)
        if not is_supported_telegraph_url(normalized):
            continue
        if normalized not in seen:
            seen.add(normalized)
            result.append(normalized)

    return result


def extract_telegraph_links_from_message(message) -> list[str]:
    text = message.message or ""
    seen = set()
    result = []

    def add_candidate(candidate: str) -> None:
        normalized = normalize_telegraph_url(candidate)
        if not is_supported_telegraph_url(normalized):
            return
        if normalized not in seen:
            seen.add(normalized)
            result.append(normalized)

    for link in extract_telegraph_links(text):
        add_candidate(link)

    for entity in message.entities or []:
        if isinstance(entity, MessageEntityTextUrl) and entity.url:
            add_candidate(entity.url)
        elif isinstance(entity, MessageEntityUrl):
            start = entity.offset
            end = start + entity.length
            if 0 <= start < end <= len(text):
                add_candidate(text[start:end])

    # Some forwarded/comments use inline URL buttons instead of visible URLs in text.
    for row in getattr(getattr(message, "reply_markup", None), "rows", []) or []:
        for button in getattr(row, "buttons", []) or []:
            button_url = getattr(button, "url", None)
            if button_url:
                add_candidate(button_url)

    return result


def fetch_telegraph_images(
    session: requests.Session,
    telegraph_url: str,
    timeout: int,
) -> tuple[str, list[str]]:
    response = session.get(telegraph_url, timeout=timeout)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

    title = "telegraph"
    if h1 := soup.find("h1"):
        title_text = h1.get_text(strip=True)
        if title_text:
            title = title_text
    elif soup.title and soup.title.string:
        title = soup.title.string.strip() or title

    image_urls = []
    seen = set()
    for img in soup.find_all("img"):
        src = img.get("src")
        if not src:
            continue
        absolute = urljoin("https://telegra.ph", src)
        if absolute not in seen:
            seen.add(absolute)
            image_urls.append(absolute)

    return title, image_urls


def detect_image_suffix(url: str) -> str:
    path = urlparse(url).path
    suffix = Path(path).suffix.lower()
    if suffix in VALID_IMAGE_SUFFIXES:
        return suffix
    return ".jpg"


def download_images_to_zip(
    session: requests.Session,
    image_urls: list[str],
    zip_path: Path,
    timeout: int,
) -> int:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    downloaded_count = 0

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
        for idx, url in enumerate(image_urls, start=1):
            try:
                resp = session.get(url, timeout=timeout)
                resp.raise_for_status()
                suffix = detect_image_suffix(url)
                file_name = f"image_{idx:03d}{suffix}"
                zipf.writestr(file_name, resp.content)
                downloaded_count += 1
            except requests.RequestException as exc:
                logging.warning("Image download failed: %s (%s)", url, exc)

    if downloaded_count == 0 and zip_path.exists():
        zip_path.unlink()

    return downloaded_count


async def run(config: AppConfig) -> None:
    tg_proxy = build_telethon_proxy(config.tg_proxy_url)
    client = TelegramClient(config.session_name, config.api_id, config.api_hash, proxy=tg_proxy)
    await client.start()

    conn = ensure_db(config.db_path)
    http_session = create_http_session(config.web_proxy_url)

    stats = {
        "posts_scanned": 0,
        "posts_with_comments": 0,
        "comments_scanned": 0,
        "links_found": 0,
        "links_skipped": 0,
        "links_downloaded": 0,
        "images_downloaded": 0,
    }

    try:
        logging.info("Connected to Telegram. Scanning channel: %s", config.channel)
        if config.tg_proxy_url:
            logging.info("Using Telegram proxy: %s", config.tg_proxy_url)
        if config.web_proxy_url:
            logging.info("Using web proxy for requests: %s", config.web_proxy_url)

        async for post in client.iter_messages(config.channel, limit=config.post_limit):
            stats["posts_scanned"] += 1

            has_comments = bool(post.replies and post.replies.replies and post.replies.replies > 0)
            if not has_comments:
                continue

            stats["posts_with_comments"] += 1

            async for comment in client.iter_messages(config.channel, reply_to=post.id):
                stats["comments_scanned"] += 1
                text = comment.message or ""
                if not text and not comment.entities:
                    continue

                links = extract_telegraph_links_from_message(comment)
                if not links:
                    continue

                for link in links:
                    stats["links_found"] += 1

                    if is_processed(conn, link):
                        stats["links_skipped"] += 1
                        continue

                    try:
                        article_title, image_urls = fetch_telegraph_images(
                            session=http_session,
                            telegraph_url=link,
                            timeout=config.request_timeout,
                        )
                    except requests.RequestException as exc:
                        logging.warning("Telegraph parse failed: %s (%s)", link, exc)
                        continue

                    if not image_urls:
                        logging.info("No images found in %s", link)
                        mark_processed(conn, link, post.id, comment.id, "")
                        continue

                    zip_name = f"{sanitize_filename(article_title)}_{post.id}_{comment.id}.zip"
                    zip_path = config.output_dir / zip_name
                    image_count = download_images_to_zip(
                        session=http_session,
                        image_urls=image_urls,
                        zip_path=zip_path,
                        timeout=config.request_timeout,
                    )

                    if image_count > 0:
                        stats["links_downloaded"] += 1
                        stats["images_downloaded"] += image_count
                        mark_processed(conn, link, post.id, comment.id, str(zip_path))
                        logging.info("Saved %s (%d images)", zip_path, image_count)

        logging.info("Done. Summary: %s", stats)
    finally:
        conn.close()
        await client.disconnect()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    config = AppConfig(
        api_id=args.api_id,
        api_hash=args.api_hash,
        channel=args.channel,
        session_name=args.session_name,
        output_dir=Path(args.output_dir),
        db_path=Path(args.db_path),
        post_limit=args.limit,
        request_timeout=args.timeout,
        tg_proxy_url=args.tg_proxy or None,
        web_proxy_url=args.web_proxy or None,
    )

    try:
        asyncio.run(run(config))
    except KeyboardInterrupt:
        logging.warning("Interrupted by user.")


if __name__ == "__main__":
    main()
