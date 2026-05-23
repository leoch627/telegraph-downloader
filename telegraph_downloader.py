#!/usr/bin/env python3
import argparse
import asyncio
import concurrent.futures
import hashlib
import logging
import re
import shutil
import sqlite3
import zipfile
from datetime import date, datetime, timezone
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
    download_workers: int
    tg_proxy_url: Optional[str]
    web_proxy_url: Optional[str]
    target_date: Optional[date]
    direct_link: Optional[str]


@dataclass
class TelegraphLink:
    url: str
    label: Optional[str] = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download images from telegra.ph links found in Telegram channel comments, "
            "or download a single Telegraph link directly."
        )
    )
    parser.add_argument("--api-id", default="", help="Telegram API ID, or env TELEGRAM_API_ID")
    parser.add_argument("--api-hash", default="", help="Telegram API hash, or env TELEGRAM_API_HASH")
    parser.add_argument("--channel", default="", help="Channel username or invite handle, or env TELEGRAM_CHANNEL")
    parser.add_argument("--session-name", default="telegraph_downloader", help="Telethon session name")
    parser.add_argument("--output-dir", default="downloads", help="Directory for generated ZIP files")
    parser.add_argument("--db-path", default="state/processed.sqlite3", help="SQLite path for incremental state")
    parser.add_argument("--limit", type=int, default=50, help="How many recent channel posts to scan")
    parser.add_argument("--timeout", type=int, default=20, help="HTTP timeout for telegra.ph/image downloads")
    parser.add_argument("--workers", type=int, default=6, help="Parallel worker count for image downloads")
    parser.add_argument(
        "--date",
        default="",
        help="Only process links from comments on this UTC date (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--link",
        default="",
        help="Download this Telegraph link directly (skip Telegram scanning)",
    )
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

    args.link = normalize_telegraph_url(args.link) if args.link else ""
    if args.link and not is_supported_telegraph_url(args.link):
        parser.error("--link must be a valid telegra.ph or graph.org URL")

    if args.date:
        try:
            args.date = parse_target_date(args.date)
        except ValueError as exc:
            parser.error(str(exc))
    else:
        args.date = None

    if args.link:
        if args.api_id:
            try:
                args.api_id = int(args.api_id)
            except ValueError as exc:
                raise SystemExit("API ID must be an integer.") from exc
        else:
            args.api_id = 0
    else:
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
    if args.workers <= 0:
        parser.error("--workers must be > 0")

    return args


def parse_target_date(raw: str) -> date:
    value = raw.strip()
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError("--date must be in YYYY-MM-DD format") from exc


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
    compact = re.sub(r"\s+", " ", name).strip()
    # Keep readable Unicode text while removing filesystem-reserved characters.
    sanitized = re.sub(r"[<>:\"/\\|?*\x00-\x1F]", " ", compact)
    sanitized = re.sub(r"\s+", " ", sanitized).strip(" .")
    if not sanitized:
        return "telegraph"
    return sanitized[:max_len].rstrip(" .") or "telegraph"


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


def resolve_redirect_to_telegraph(session: requests.Session, url: str, timeout: int) -> Optional[str]:
    """Follow redirects for `url` and return a telegra.ph/graph.org URL if the final
    destination is a supported Telegraph host. Returns None otherwise.
    """
    try:
        # Try a HEAD first to avoid downloading the whole body. Some servers reject HEAD.
        resp = session.head(url, allow_redirects=True, timeout=timeout)
        final = resp.url
    except requests.RequestException:
        try:
            # Fall back to GET if HEAD fails.
            resp = session.get(url, allow_redirects=True, timeout=timeout, stream=True)
            final = resp.url
            # Close response to avoid holding the connection open.
            resp.close()
        except requests.RequestException:
            return None

    final_normalized = normalize_telegraph_url(final)
    if is_supported_telegraph_url(final_normalized):
        return final_normalized
    return None


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


def extract_telegraph_links_from_message(
    message, session: Optional[requests.Session] = None, timeout: int = 10
) -> list[TelegraphLink]:
    text = message.message or ""
    seen = set()
    result: list[TelegraphLink] = []

    def normalize_label(label: Optional[str]) -> Optional[str]:
        if not label:
            return None
        cleaned = re.sub(r"\s+", " ", label).strip()
        return cleaned or None

    def add_candidate(candidate: str, label: Optional[str] = None) -> None:
        normalized = normalize_telegraph_url(candidate)
        if not is_supported_telegraph_url(normalized):
            # If the candidate is not itself a telegra.ph URL, try following redirects
            # (useful for t.me short links or other redirectors that point to telegra.ph).
            if session:
                resolved = resolve_redirect_to_telegraph(session, candidate, timeout)
                if resolved:
                    normalized = resolved
                else:
                    return
            else:
                return
        normalized_label = normalize_label(label)
        if normalized not in seen:
            seen.add(normalized)
            result.append(TelegraphLink(url=normalized, label=normalized_label))
            return

        # Backfill label if the URL was found earlier via plain-text regex.
        if normalized_label:
            for item in result:
                if item.url == normalized and not item.label:
                    item.label = normalized_label
                    break

    for link in extract_telegraph_links(text):
        add_candidate(link)

    for entity in message.entities or []:
        if isinstance(entity, MessageEntityTextUrl) and entity.url:
            start = entity.offset
            end = start + entity.length
            entity_label = text[start:end] if 0 <= start < end <= len(text) else None
            add_candidate(entity.url, entity_label)
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
                add_candidate(button_url, getattr(button, "text", None))

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
    link: str,
    timeout: int,
    workers: int,
) -> tuple[int, int]:
    def download_one_image(
        url: str,
        index: int,
        total: int,
        target_path: Path,
        request_headers: dict,
        request_proxies: dict,
    ):
        logging.info("Downloading image %d/%d: %s", index, total, url)
        try:
            resp = requests.get(
                url,
                timeout=timeout,
                headers=request_headers,
                proxies=request_proxies,
            )
            resp.raise_for_status()
            part_path = target_path.with_name(f"{target_path.name}.part")
            target_path.parent.mkdir(parents=True, exist_ok=True)
            with open(part_path, "wb") as f:
                f.write(resp.content)
            part_path.replace(target_path)
            logging.info("Downloaded image %d/%d (%d bytes)", index, total, len(resp.content))
            return index, url, target_path
        except requests.RequestException as exc:
            logging.warning("Image download failed %d/%d: %s (%s)", index, total, url, exc)
            return None

    zip_path.parent.mkdir(parents=True, exist_ok=True)
    total = len(image_urls)
    if total == 0:
        return 0, 0

    link_key = hashlib.sha1(link.encode("utf-8")).hexdigest()[:16]
    partial_dir = zip_path.parent / ".partial" / link_key
    partial_dir.mkdir(parents=True, exist_ok=True)

    expected_files: list[tuple[int, str, Path]] = []
    missing_tasks: list[tuple[int, str, Path]] = []
    existing_count = 0

    for idx, url in enumerate(image_urls, start=1):
        suffix = detect_image_suffix(url)
        file_name = f"image_{idx:03d}{suffix}"
        target_path = partial_dir / file_name
        expected_files.append((idx, url, target_path))
        if target_path.exists() and target_path.stat().st_size > 0:
            existing_count += 1
            logging.info("Resume hit %d/%d, skip existing: %s", idx, total, target_path)
        else:
            missing_tasks.append((idx, url, target_path))

    logging.info(
        "Resume status for link: total=%d existing=%d missing=%d workers=%d",
        total,
        existing_count,
        len(missing_tasks),
        workers,
    )

    downloaded_results = []
    request_headers = dict(session.headers)
    request_proxies = dict(session.proxies)

    if missing_tasks:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [
                executor.submit(
                    download_one_image,
                    url,
                    idx,
                    total,
                    target_path,
                    request_headers,
                    request_proxies,
                )
                for idx, url, target_path in missing_tasks
            ]

            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                if result is not None:
                    downloaded_results.append(result)

    downloaded_count = existing_count + len(downloaded_results)

    if downloaded_count < total:
        logging.warning(
            "Link incomplete: downloaded=%d/%d. Partial files kept for next run: %s",
            downloaded_count,
            total,
            partial_dir,
        )
        return downloaded_count, total

    if downloaded_count == 0:
        return 0, total

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
        for output_idx, source_url, source_path in expected_files:
            if not source_path.exists() or source_path.stat().st_size == 0:
                logging.warning("Missing file before packing: %s", source_path)
                continue
            zipf.write(source_path, arcname=source_path.name)
            logging.info("Packed image %d/%d into zip: %s", output_idx, total, source_path.name)

    try:
        shutil.rmtree(partial_dir)
    except OSError as exc:
        logging.warning("Failed to cleanup partial dir %s (%s)", partial_dir, exc)

    return downloaded_count, total


async def run(config: AppConfig) -> None:
    if config.api_id <= 0:
        raise ValueError("API ID must be provided for Telegram scanning mode")

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
        "links_incomplete": 0,
        "images_downloaded": 0,
    }

    try:
        logging.info("Connected to Telegram. Scanning channel: %s", config.channel)
        logging.info(
            "Runtime config: limit=%d timeout=%ds workers=%d output=%s db=%s",
            config.post_limit,
            config.request_timeout,
            config.download_workers,
            config.output_dir,
            config.db_path,
        )
        if config.target_date:
            logging.info("Date filter enabled (UTC): %s", config.target_date.isoformat())
        if config.tg_proxy_url:
            logging.info("Using Telegram proxy: %s", config.tg_proxy_url)
        if config.web_proxy_url:
            logging.info("Using web proxy for requests: %s", config.web_proxy_url)

        async for post in client.iter_messages(config.channel, limit=config.post_limit):
            stats["posts_scanned"] += 1
            logging.debug("Scanning post id=%s", post.id)

            has_comments = bool(post.replies and post.replies.replies and post.replies.replies > 0)
            if not has_comments:
                continue

            stats["posts_with_comments"] += 1
            logging.info("Post %s has comments, scanning replies", post.id)

            async for comment in client.iter_messages(config.channel, reply_to=post.id):
                stats["comments_scanned"] += 1

                if config.target_date:
                    comment_date = comment.date.astimezone(timezone.utc).date()
                    if comment_date != config.target_date:
                        continue

                text = comment.message or ""
                if not text and not comment.entities:
                    continue

                links = extract_telegraph_links_from_message(
                    comment, session=http_session, timeout=config.request_timeout
                )
                if not links:
                    continue

                logging.info(
                    "Found %d Telegraph link(s) in comment %s (post %s)",
                    len(links),
                    comment.id,
                    post.id,
                )

                for link_item in links:
                    link = link_item.url
                    stats["links_found"] += 1
                    logging.info("Captured link: %s", link)

                    if is_processed(conn, link):
                        stats["links_skipped"] += 1
                        logging.info("Skip already processed link: %s", link)
                        continue

                    try:
                        logging.info("Fetching Telegraph page: %s", link)
                        article_title, image_urls = fetch_telegraph_images(
                            session=http_session,
                            telegraph_url=link,
                            timeout=config.request_timeout,
                        )
                        logging.info("Parsed %d image(s) from link: %s", len(image_urls), link)
                        for idx, image_url in enumerate(image_urls, start=1):
                            logging.info("Image URL %d/%d: %s", idx, len(image_urls), image_url)
                    except requests.RequestException as exc:
                        logging.warning("Telegraph parse failed: %s (%s)", link, exc)
                        continue

                    if not image_urls:
                        logging.info("No images found in %s", link)
                        mark_processed(conn, link, post.id, comment.id, "")
                        continue

                    filename_title = link_item.label or article_title
                    if link_item.label:
                        logging.info("Using hyperlink label as filename title: %s", link_item.label)

                    zip_name = f"{sanitize_filename(filename_title)}_{post.id}_{comment.id}.zip"
                    zip_path = config.output_dir / zip_name
                    image_count, image_total = download_images_to_zip(
                        session=http_session,
                        image_urls=image_urls,
                        zip_path=zip_path,
                        link=link,
                        timeout=config.request_timeout,
                        workers=config.download_workers,
                    )

                    if image_count == image_total and image_total > 0:
                        stats["links_downloaded"] += 1
                        stats["images_downloaded"] += image_count
                        mark_processed(conn, link, post.id, comment.id, str(zip_path))
                        logging.info("Saved %s (%d images)", zip_path, image_count)
                    else:
                        stats["links_incomplete"] += 1
                        logging.warning(
                            "Link not fully completed yet (%d/%d): %s. It will resume on next run.",
                            image_count,
                            image_total,
                            link,
                        )

        logging.info("Done. Summary: %s", stats)
    finally:
        conn.close()
        await client.disconnect()


def run_direct_link_mode(config: AppConfig) -> None:
    if not config.direct_link:
        raise ValueError("Direct link mode requires a Telegraph link")

    conn = ensure_db(config.db_path)
    http_session = create_http_session(config.web_proxy_url)
    link = config.direct_link

    try:
        logging.info("Direct link mode: %s", link)
        if config.web_proxy_url:
            logging.info("Using web proxy for requests: %s", config.web_proxy_url)

        if is_processed(conn, link):
            logging.info("Skip already processed link: %s", link)
            return

        try:
            article_title, image_urls = fetch_telegraph_images(
                session=http_session,
                telegraph_url=link,
                timeout=config.request_timeout,
            )
            logging.info("Parsed %d image(s) from link: %s", len(image_urls), link)
        except requests.RequestException as exc:
            logging.warning("Telegraph parse failed: %s (%s)", link, exc)
            return

        if not image_urls:
            logging.info("No images found in %s", link)
            mark_processed(conn, link, 0, 0, "")
            return

        link_key = hashlib.sha1(link.encode("utf-8")).hexdigest()[:8]
        zip_name = f"{sanitize_filename(article_title)}_{link_key}.zip"
        zip_path = config.output_dir / zip_name
        image_count, image_total = download_images_to_zip(
            session=http_session,
            image_urls=image_urls,
            zip_path=zip_path,
            link=link,
            timeout=config.request_timeout,
            workers=config.download_workers,
        )

        if image_count == image_total and image_total > 0:
            mark_processed(conn, link, 0, 0, str(zip_path))
            logging.info("Saved %s (%d images)", zip_path, image_count)
        else:
            logging.warning(
                "Link not fully completed yet (%d/%d): %s. It will resume on next run.",
                image_count,
                image_total,
                link,
            )
    finally:
        conn.close()


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
        download_workers=args.workers,
        tg_proxy_url=args.tg_proxy or None,
        web_proxy_url=args.web_proxy or None,
        target_date=args.date,
        direct_link=args.link or None,
    )

    try:
        if config.direct_link:
            run_direct_link_mode(config)
        else:
            asyncio.run(run(config))
    except KeyboardInterrupt:
        logging.warning("Interrupted by user.")


if __name__ == "__main__":
    main()
