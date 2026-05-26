#!/usr/bin/env python3
"""Instapaper → Kindle EPUB sender."""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import html
import json
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
from requests_oauthlib import OAuth1Session

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TOKEN_PATH = Path(".instapaper_token")
INSTAPAPER_BASE = "https://www.instapaper.com/api/1"


@dataclasses.dataclass
class Config:
    consumer_key: str
    consumer_secret: str
    username: str
    password: str
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
    required = [
        "INSTAPAPER_CONSUMER_KEY",
        "INSTAPAPER_CONSUMER_SECRET",
        "INSTAPAPER_USERNAME",
        "INSTAPAPER_PASSWORD",
        "KINDLE_EMAIL",
    ]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        print(f"Missing required .env keys: {', '.join(missing)}")
        raise SystemExit(2)
    return Config(
        consumer_key=os.environ["INSTAPAPER_CONSUMER_KEY"],
        consumer_secret=os.environ["INSTAPAPER_CONSUMER_SECRET"],
        username=os.environ["INSTAPAPER_USERNAME"],
        password=os.environ["INSTAPAPER_PASSWORD"],
        kindle_email=os.environ["KINDLE_EMAIL"],
        articles_per_send=_parse_int_env("ARTICLES_PER_SEND", 10),
        mail_from=os.environ.get("MAIL_FROM", ""),
    )


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def load_token() -> tuple[str, str] | None:
    if not TOKEN_PATH.exists():
        return None
    try:
        data = json.loads(TOKEN_PATH.read_text())
        return data["oauth_token"], data["oauth_token_secret"]
    except Exception:
        return None


def save_token(token: str, secret: str) -> None:
    tmp = TOKEN_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps({"oauth_token": token, "oauth_token_secret": secret}))
    os.replace(tmp, TOKEN_PATH)


# ---------------------------------------------------------------------------
# Instapaper client
# ---------------------------------------------------------------------------

class InstapaperClient:
    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._token: tuple[str, str] | None = load_token()

    def _xauth(self) -> tuple[str, str]:
        session = OAuth1Session(
            client_key=self._cfg.consumer_key,
            client_secret=self._cfg.consumer_secret,
        )
        resp = session.post(
            f"{INSTAPAPER_BASE}/oauth/access_token",
            data={
                "x_auth_username": self._cfg.username,
                "x_auth_password": self._cfg.password,
                "x_auth_mode": "client_auth",
            },
            timeout=30,
        )
        if resp.status_code == 401:
            print("Instapaper auth failed — check username/password and consumer key/secret.")
            raise SystemExit(1)
        resp.raise_for_status()
        parsed = urllib.parse.parse_qs(resp.text)
        token = parsed["oauth_token"][0]
        secret = parsed["oauth_token_secret"][0]
        save_token(token, secret)
        print("Authenticated with Instapaper, token cached.")
        return token, secret

    def _session(self) -> OAuth1Session:
        if self._token is None:
            self._token = self._xauth()
        token, secret = self._token
        return OAuth1Session(
            client_key=self._cfg.consumer_key,
            client_secret=self._cfg.consumer_secret,
            resource_owner_key=token,
            resource_owner_secret=secret,
        )

    def _request(self, endpoint: str, data: dict) -> object:
        # Makes a signed POST, retries once after re-authing on 401.
        for attempt in range(2):
            resp = self._session().post(f"{INSTAPAPER_BASE}/{endpoint}", data=data, timeout=30)
            if resp.status_code == 401 and attempt == 0:
                TOKEN_PATH.unlink(missing_ok=True)
                self._token = None
                continue
            return resp
        return resp  # second 401 falls through to caller

    def list_unread(self, limit: int = 10) -> list[dict]:
        resp = self._request("bookmarks/list", {"limit": limit, "folder_id": "unread"})
        resp.raise_for_status()
        return [x for x in resp.json() if x.get("type") == "bookmark"]

    def get_text(self, bookmark_id: int) -> str:
        resp = self._request("bookmarks/get_text", {"bookmark_id": bookmark_id})
        if not resp.ok:
            raise RuntimeError(f"HTTP {resp.status_code}")
        return resp.text


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


_COVER_W, _COVER_H = 1600, 2560
_COVER_FONT = "/System/Library/Fonts/Helvetica.ttc"


def _render_cover(heading: str, subheading: str) -> bytes:
    img = PilImage.new("RGB", (_COVER_W, _COVER_H), color=(0, 0, 0))
    draw = ImageDraw.Draw(img)
    h_font = ImageFont.truetype(_COVER_FONT, 140)
    s_font = ImageFont.truetype(_COVER_FONT, 90)
    hw = draw.textlength(heading, font=h_font)
    sw = draw.textlength(subheading, font=s_font)
    draw.text(((_COVER_W - hw) / 2, _COVER_H * 0.38), heading, fill="white", font=h_font)
    draw.text(((_COVER_W - sw) / 2, _COVER_H * 0.50), subheading, fill="white", font=s_font)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def build_epub(title: str, articles: list[Article]) -> bytes:
    book = epub.EpubBook()
    book.set_identifier(f"instapaper-{uuid.uuid4()}")
    book.set_title(title)
    book.set_language("en")
    book.add_author("Instapaper")
    book.set_cover("cover.jpg", _render_cover("Instapaper Digest", title))

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
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())


# ---------------------------------------------------------------------------
# Interactive count prompt
# ---------------------------------------------------------------------------

def prompt_article_count(default: int) -> int:
    while True:
        raw = input(f"How many articles? [{default}]: ").strip()
        if not raw:
            return default
        try:
            n = int(raw)
            if 1 <= n <= 500:
                return n
            print("Enter a number between 1 and 500.")
        except ValueError:
            print("Please enter a valid number.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Send Instapaper articles to Kindle.")
    parser.add_argument("--dry-run", action="store_true", help="Build EPUB but do not send email.")
    parser.add_argument("--count", type=int, default=None, help="Number of articles (skips prompt).")
    args = parser.parse_args()

    cfg = load_config()

    if args.count is not None:
        count = args.count
    else:
        count = prompt_article_count(cfg.articles_per_send)

    client = InstapaperClient(cfg)

    print(f"Fetching {count} unread bookmarks…")
    bookmarks = client.list_unread(limit=count)

    if not bookmarks:
        print("No unread articles.")
        return 0

    print(f"{len(bookmarks)} article(s) to fetch.")
    articles: list[Article] = []
    for b in bookmarks:
        bid = b["bookmark_id"]
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
