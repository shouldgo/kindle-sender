#!/usr/bin/env python3
"""Instapaper → Kindle EPUB sender."""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import html
import mimetypes
import os
import secrets
import subprocess
import tempfile
import time
import urllib.parse
import uuid
from datetime import datetime
from pathlib import Path

import io

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from ebooklib import epub
from PIL import Image as PilImage, ImageDraw, ImageFont

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

INSTAPAPER_BASE = "https://www.instapaper.com/api/2"
LIST_PAGE_SIZE = 500  # API maximum per page


@dataclasses.dataclass
class Config:
    token: str
    kindle_email: str
    articles_per_send: int
    mail_from: str


@dataclasses.dataclass
class Article:
    id: int
    title: str
    url: str
    time: int
    html_content: str


def _parse_int_env(key: str, default: int) -> int:
    val = os.environ.get(key, str(default))
    try:
        return int(val)
    except ValueError:
        print(f"Warning: {key}={val!r} is not a valid integer, using {default}.")
        return default


def load_config() -> Config:
    load_dotenv()
    required = ["INSTAPAPER_TOKEN", "KINDLE_EMAIL"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        print(f"Missing required .env keys: {', '.join(missing)}")
        raise SystemExit(2)
    return Config(
        token=os.environ["INSTAPAPER_TOKEN"],
        kindle_email=os.environ["KINDLE_EMAIL"],
        articles_per_send=_parse_int_env("ARTICLES_PER_SEND", 10),
        mail_from=os.environ.get("MAIL_FROM", ""),
    )


# ---------------------------------------------------------------------------
# Instapaper client
# ---------------------------------------------------------------------------

class InstapaperClient:
    def __init__(self, cfg: Config) -> None:
        self._http = requests.Session()
        self._http.headers["Authorization"] = f"Bearer {cfg.token}"

    def _get(self, path: str, params: dict | None = None) -> requests.Response:
        resp = self._http.get(f"{INSTAPAPER_BASE}/{path}", params=params, timeout=30)
        if resp.status_code == 401:
            print("Instapaper token invalid — regenerate it at instapaper.com/developers/applications.")
            raise SystemExit(1)
        return resp

    def list_unread(self) -> list[dict]:
        # "home" is the unread list; page until we've seen `total` bookmarks.
        bookmarks: list[dict] = []
        while True:
            resp = self._get("bookmarks", {"section": "home", "limit": LIST_PAGE_SIZE, "offset": len(bookmarks)})
            resp.raise_for_status()
            data = resp.json()
            page = data["bookmarks"]
            bookmarks.extend(page)
            if not page or len(bookmarks) >= data["total"]:
                return bookmarks

    def get_text(self, bookmark_id: int) -> str:
        resp = self._get(f"bookmarks/{bookmark_id}/parse")
        if not resp.ok:
            raise RuntimeError(f"HTTP {resp.status_code}")
        return resp.json()["content"]["body"] or ""


# ---------------------------------------------------------------------------
# EPUB builder
# ---------------------------------------------------------------------------

def random_suffix(n: int = 6) -> str:
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    return "".join(secrets.choice(alphabet) for _ in range(n))


def domain_of(url: str) -> str:
    try:
        host = urllib.parse.urlparse(url).hostname or url
    except Exception:
        return url
    return host[4:] if host.startswith("www.") else host


IMAGE_TIMEOUT = 10
IMAGE_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
IMAGE_MIME_TO_EXT = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/svg+xml": "svg",
    "image/avif": "avif",
    "image/bmp": "bmp",
    "image/tiff": "tiff",
}


def _first_srcset_url(srcset: str) -> str | None:
    if not srcset:
        return None
    first = srcset.split(",", 1)[0].strip()
    if not first:
        return None
    return first.split()[0]


def _pick_ext(url: str, content_type: str | None) -> str | None:
    if content_type:
        ct = content_type.split(";", 1)[0].strip().lower()
        if not ct.startswith("image/"):
            return None
        if ct in IMAGE_MIME_TO_EXT:
            return IMAGE_MIME_TO_EXT[ct]
        guessed = mimetypes.guess_extension(ct)
        if guessed:
            return guessed.lstrip(".")
    path = urllib.parse.urlparse(url).path
    ext = Path(path).suffix.lower().lstrip(".")
    if ext in {"jpg", "jpeg", "png", "gif", "webp", "svg", "avif", "bmp", "tiff"}:
        return "jpg" if ext == "jpeg" else ext
    return None


def _resolve_image_url(article_url: str, src: str) -> str | None:
    src = src.strip()
    if not src or src.startswith("data:"):
        return None
    try:
        return urllib.parse.urljoin(article_url, src)
    except Exception:
        return None


def _download_image(url: str) -> tuple[bytes, str] | None:
    try:
        resp = requests.get(
            url,
            timeout=IMAGE_TIMEOUT,
            headers={"User-Agent": IMAGE_UA, "Accept": "image/*,*/*;q=0.8"},
        )
    except Exception as e:
        print(f"  [img-skip] {url}: {e}")
        return None
    if not resp.ok:
        print(f"  [img-skip] {url}: HTTP {resp.status_code}")
        return None
    ct = resp.headers.get("Content-Type")
    ext = _pick_ext(url, ct)
    if ext is None:
        print(f"  [img-skip] {url}: not an image (Content-Type={ct!r})")
        return None
    media_type = ct.split(";", 1)[0].strip().lower() if ct and ct.lower().startswith("image/") else f"image/{ext}"
    result = _normalize_image(resp.content, media_type)
    if result is None:
        print(f"  [img-skip] {url}: unsupported format ({media_type})")
        return None
    return result


_PASSTHROUGH_TYPES = {"image/jpeg", "image/png", "image/gif"}
_CONVERT_TO_JPEG = {"image/webp", "image/avif", "image/bmp", "image/tiff"}


def _normalize_image(content: bytes, media_type: str) -> tuple[bytes, str] | None:
    mt = media_type.split(";", 1)[0].strip().lower()
    if mt in _PASSTHROUGH_TYPES:
        return content, mt
    if mt == "image/svg+xml":
        return None
    if mt in _CONVERT_TO_JPEG:
        try:
            img = PilImage.open(io.BytesIO(content)).convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=85)
            return buf.getvalue(), "image/jpeg"
        except Exception as e:
            print(f"  [img-skip] format conversion failed ({mt}): {e}")
            return None
    return content, mt


def _embed_images(
    article_url: str,
    html_content: str,
    image_map: dict[str, dict],
) -> tuple[str, int, int]:
    """Walk <img> and <picture> tags; download and embed each image; rewrite src to local path."""
    if not html_content:
        return html_content, 0, 0

    soup = BeautifulSoup(html_content, "html.parser")
    embedded = 0
    skipped = 0

    for picture in soup.find_all("picture"):
        chosen_url = None
        for source in picture.find_all("source"):
            url = _first_srcset_url(source.get("srcset", ""))
            if url:
                chosen_url = url
                break
        inner_img = picture.find("img")
        if chosen_url is None and inner_img is not None:
            chosen_url = inner_img.get("src") or _first_srcset_url(inner_img.get("srcset", ""))
        if chosen_url:
            new_img = soup.new_tag("img", src=chosen_url)
            if inner_img is not None and inner_img.get("alt"):
                new_img["alt"] = inner_img["alt"]
            picture.replace_with(new_img)
        else:
            picture.decompose()

    for img in soup.find_all("img"):
        src = img.get("src") or _first_srcset_url(img.get("srcset", "") or "")
        abs_url = _resolve_image_url(article_url, src or "")
        if abs_url is None:
            img.decompose()
            skipped += 1
            continue

        if abs_url not in image_map:
            result = _download_image(abs_url)
            if result is None:
                img.decompose()
                skipped += 1
                continue
            content, media_type = result
            digest = hashlib.sha1(abs_url.encode("utf-8")).hexdigest()[:12]
            ext = IMAGE_MIME_TO_EXT.get(media_type, media_type.split("/", 1)[-1])
            ext = ext.lower().replace("+xml", "")
            file_name = f"images/{digest}.{ext}"
            image_map[abs_url] = {
                "uid": f"img_{digest}",
                "file_name": file_name,
                "media_type": media_type,
                "content": content,
            }

        entry = image_map[abs_url]
        img["src"] = entry["file_name"]
        for attr in ("srcset", "style", "width", "height", "sizes", "loading", "decoding"):
            if img.has_attr(attr):
                del img[attr]
        embedded += 1

    return str(soup), embedded, skipped


_COVER_W, _COVER_H = 1600, 2133  # 3:4 aspect ratio, matches Kindle Oasis 10th gen
_COVER_FONT = "/System/Library/Fonts/Helvetica.ttc"


def _render_cover(heading: str, subheading: str) -> bytes:
    img = PilImage.new("RGB", (_COVER_W, _COVER_H), color=(18, 18, 18))
    draw = ImageDraw.Draw(img)
    h_font = ImageFont.truetype(_COVER_FONT, 220)
    s_font = ImageFont.truetype(_COVER_FONT, 100)
    hb = draw.textbbox((0, 0), heading, font=h_font)
    sb = draw.textbbox((0, 0), subheading, font=s_font)
    h_h = hb[3] - hb[1]
    s_h = sb[3] - sb[1]
    block_h = h_h + 44 + s_h
    block_top = _COVER_H // 2 - block_h // 2
    draw.text(((_COVER_W - (hb[2] - hb[0])) // 2 - hb[0], block_top - hb[1]), heading, fill=(255, 255, 255), font=h_font)
    draw.text(((_COVER_W - (sb[2] - sb[0])) // 2 - sb[0], block_top + h_h + 44 - sb[1]), subheading, fill=(180, 180, 180), font=s_font)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def build_epub(title: str, articles: list[Article]) -> bytes:
    book = epub.EpubBook()
    book.set_identifier(f"instapaper-{uuid.uuid4()}")
    book.set_title(title)
    book.set_language("en")
    book.add_author("Instapaper")
    book.set_cover("cover.jpg", _render_cover("Instapaper", title))

    css = """\
.article-title {
    font-size: 1.3em;
    font-weight: bold;
    margin: 0.6em 0 0.3em;
    line-height: 1.25;
    display: block;
}
h1, h2, h3, h4, h5, h6 {
    font-size: 1.15em;
    font-weight: bold;
    margin: 0.5em 0 0.25em;
    line-height: 1.25;
}
"""
    css_item = epub.EpubItem(
        uid="style_main",
        file_name="style/main.css",
        media_type="text/css",
        content=css,
    )
    book.add_item(css_item)

    image_map: dict[str, dict] = {}
    total_embedded = 0
    total_skipped = 0
    chapters = []

    for i, art in enumerate(articles):
        ch = epub.EpubHtml(
            title=art.title,
            file_name=f"chap_{i:03d}.xhtml",
            lang="en",
        )
        rewritten_html, n_emb, n_skip = _embed_images(art.url, art.html_content or "", image_map)
        total_embedded += n_emb
        total_skipped += n_skip

        body = (
            f"<p class='article-title'><strong>{html.escape(art.title)}</strong></p>"
            f"<p><a href='{html.escape(art.url)}'>{html.escape(domain_of(art.url))}</a></p>"
            f"<hr/>{rewritten_html or '<p>(no content)</p>'}"
        )
        ch.add_link(href="../style/main.css", rel="stylesheet", type="text/css")
        ch.content = (
            f'<?xml version="1.0" encoding="utf-8"?>'
            f'<html xmlns="http://www.w3.org/1999/xhtml">'
            f'<head><title>{html.escape(art.title)}</title></head>'
            f'<body>{body}</body></html>'
        ).encode("utf-8")
        book.add_item(ch)
        chapters.append(ch)

    for entry in image_map.values():
        book.add_item(epub.EpubImage(
            uid=entry["uid"],
            file_name=entry["file_name"],
            media_type=entry["media_type"],
            content=entry["content"],
        ))

    print(f"  Embedded {total_embedded} image reference(s), {len(image_map)} unique; skipped {total_skipped}.")

    book.toc = tuple(chapters)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav", *chapters]

    with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as tf:
        tmp_path = tf.name
    try:
        # epub3_landmark and epub3_pages cause lxml to crash on empty body content
        epub.write_epub(tmp_path, book, {"epub3_landmark": False, "epub3_pages": False})
        return Path(tmp_path).read_bytes()
    finally:
        os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# Mailer — sends via macOS Mail.app, no credentials needed
# ---------------------------------------------------------------------------

def _q(s: str) -> str:
    """Escape a value for interpolation inside an AppleScript double-quoted string."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def send_email(cfg: Config, subject: str, epub_path: Path) -> None:
    sender_clause = f', sender:"{_q(cfg.mail_from)}"' if cfg.mail_from else ""
    script = f"""
tell application "Mail"
    set msg to make new outgoing message with properties {{subject:"{_q(subject)}", visible:false{sender_clause}}}
    tell msg
        make new to recipient at end of to recipients with properties {{address:"{_q(cfg.kindle_email)}"}}
        make new attachment with properties {{file name:(POSIX file "{_q(str(epub_path.resolve()))}") as alias}}
    end tell
    send msg
end tell
"""
    # Absolute path + close_fds=False makes subprocess use posix_spawn instead of fork.
    # After requests/Network.framework threads have run, fork() segfaults in the child on macOS 27.
    result = subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, text=True, close_fds=False)
    if result.returncode < 0:
        raise RuntimeError(f"osascript killed by signal {-result.returncode}")
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())


# ---------------------------------------------------------------------------
# Interactive count prompt
# ---------------------------------------------------------------------------

def prompt_article_count(default: int, max_n: int) -> int:
    while True:
        raw = input(f"You have {max_n} unread. How many articles? [{default}]: ").strip()
        if not raw:
            return default
        try:
            n = int(raw)
            if 1 <= n <= max_n:
                return n
            print(f"Enter a number between 1 and {max_n}.")
        except ValueError:
            print("Please enter a valid number.")


def prompt_order(default: str = "oldest") -> str:
    hint = "[o]/l" if default == "oldest" else "o/[l]"
    while True:
        raw = input(f"Latest or oldest? {hint}: ").strip().lower()
        if not raw:
            return default
        if raw in ("o", "oldest"):
            return "oldest"
        if raw in ("l", "latest"):
            return "latest"
        print("Enter 'o' (oldest) or 'l' (latest).")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Send Instapaper articles to Kindle.")
    parser.add_argument("--dry-run", action="store_true", help="Build EPUB but do not send email.")
    parser.add_argument("--count", type=int, default=None, help="Number of articles (skips prompt).")
    parser.add_argument("--order", choices=["latest", "oldest"], default=None, help="Which end of the unread list (skips prompt).")
    args = parser.parse_args()
    if args.count is not None and args.count < 1:
        parser.error("--count must be at least 1")

    cfg = load_config()
    client = InstapaperClient(cfg)

    unread = client.list_unread()
    if not unread:
        print("No unread articles.")
        return 0
    total = len(unread)

    if args.count is not None:
        count = args.count
        if count > total:
            print(f"Only {total} unread — sending {total}.")
            count = total
    else:
        count = prompt_article_count(min(cfg.articles_per_send, total), total)

    order = args.order or prompt_order()

    unread.sort(key=lambda b: int(b.get("time", 0)), reverse=(order == "latest"))
    bookmarks = unread[:count]

    print(f"{len(bookmarks)} article(s) to fetch.")
    articles: list[Article] = []
    for b in bookmarks:
        bid = b["id"]
        btitle = b.get("title") or f"Article {bid}"
        print(f"  Fetching: {btitle}")
        try:
            raw_html = client.get_text(bid)
            articles.append(Article(
                id=bid,
                title=btitle,
                url=b.get("url", ""),
                time=int(b.get("time", 0)),
                html_content=raw_html,
            ))
        except Exception as e:
            print(f"  [skip] {bid} {btitle!r}: {e}")
        time.sleep(0.25)

    if not articles:
        print("All fetches failed — nothing to send.")
        return 1

    articles.sort(key=lambda a: a.time)

    now = datetime.now()
    title = now.strftime("%B %-d")
    filename = now.strftime("Instapaper-ReadLater-%Y-%m-%d-") + random_suffix() + ".epub"

    print(f"Building EPUB '{title}' with {len(articles)} article(s)…")
    epub_bytes = build_epub(title, articles)
    epub_size = len(epub_bytes)
    print(f"  EPUB size: {epub_size:,} bytes")
    if epub_size > 49 * 1024 * 1024:
        print(f"Error: EPUB is {epub_size / 1024 / 1024:.1f} MB — exceeds Kindle's 50 MB email limit. Reduce article count and try again.")
        return 1
    if epub_size > 25 * 1024 * 1024:
        print(f"Warning: EPUB is {epub_size / 1024 / 1024:.1f} MB — large attachments may be slow to deliver.")

    epub_path = Path(filename)
    epub_path.write_bytes(epub_bytes)
    print(f"  Wrote {epub_path}")

    if args.dry_run:
        print("Dry run: skipping send.")
        return 0

    from_note = f" from {cfg.mail_from}" if cfg.mail_from else ""
    print(f"Sending to {cfg.kindle_email}{from_note} via Mail.app…")
    try:
        send_email(cfg, subject=title, epub_path=epub_path)
    except Exception as e:
        print(f"Send failed: {e}")
        return 1
    finally:
        epub_path.unlink(missing_ok=True)

    print(f"Done — {len(articles)} article(s) sent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
