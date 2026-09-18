# kindle-sender

Replicates Instapaper's paywalled "Send to Kindle" feature. Fetches saved unread articles from Instapaper, builds a single EPUB digest, and delivers it to a Kindle via macOS Mail.app.

## What it does

1. Authenticates with Instapaper via xAuth (OAuth 1.0a)
2. Fetches the unread list (up to 500), shows the count, asks how many and latest vs oldest, then fetches the chosen articles' parsed HTML
3. Downloads and embeds all images into the EPUB
4. Builds a single EPUB digest and emails it to the Kindle via Mail.app

After reading on Kindle, archive articles manually in Instapaper.

## Files

- `send.py` — the entire script, single file
- `.env` — secrets and config (gitignored)
- `.env.example` — template with all required keys
- `requirements.txt` — Python dependencies

## Setup

```bash
python3 -m venv .venv
pip install -r requirements.txt
```

Fill in `.env` (copy from `.env.example`):
- `INSTAPAPER_CONSUMER_KEY` / `INSTAPAPER_CONSUMER_SECRET` — from instapaper.com/main/request_oauth_consumer_token (Owner Only app)
- `INSTAPAPER_USERNAME` / `INSTAPAPER_PASSWORD` — your Instapaper login
- `KINDLE_EMAIL` — your `@kindle.com` address from amazon.com/myk
- `MAIL_FROM` — the address Mail.app sends from (required if you have multiple accounts — Amazon silently drops mail from unapproved addresses)

Add `MAIL_FROM` to Amazon's approved senders list at amazon.com/myk → Preferences → Personal Document Settings.

## Usage

```bash
python3 send.py                          # prompts: count (default 10), then latest/oldest (default oldest)
python3 send.py --count 5 --order latest # skip prompts
python3 send.py --dry-run                # build EPUB locally, don't send
```

Dry run writes `Instapaper-ReadLater-YYYY-MM-DD-XXXXXX.epub` to the project directory.

## Known limitations

- **500-item cap.** Instapaper's `bookmarks/list` returns at most 500 items and has no sort or count endpoint. The script fetches the list once and sorts by save time (`time`) locally. With more than 500 unread, the count shows `500+` and "oldest" means the oldest of the newest 500.
- **No dedup across runs.** Running twice sends the same articles twice. Archive manually in Instapaper after reading.
- **Mail.app must be running.** Script exits once Mail.app queues the message — stuck Outbox is silent.
- **Image quality.** Images render as grayscale halftones on e-ink. Non-JPEG/PNG formats (webp, avif, bmp, tiff) are converted to JPEG via Pillow before embedding; SVGs are skipped silently. Some CDN-hosted images are skipped (logged as `[img-skip]`). If images still don't appear on Kindle after conversion, the next step would be pre-converting the EPUB to AZW3 locally via Calibre before sending.
- **No retry on transient errors.** If Instapaper returns a 5xx or a network error during a run, the affected article is skipped and logged. Rerun the script to retry.
- **Personal use only.** Instapaper's `bookmarks/get_text` is explicitly personal-use in their API docs.

## Troubleshooting

- **"Send failed:" with no message + macOS "Python quit unexpectedly" (fixed).** On macOS 27, `subprocess` launching `osascript` via `fork()` segfaulted in the child (`crashed on child side of fork pre-exec` in `~/Library/Logs/DiagnosticReports/Python-*.ips`), because Network.framework threads from the earlier HTTP/image downloads don't survive fork. Fix: `send_email` calls `/usr/bin/osascript` by absolute path with `close_fds=False`, which makes Python use `posix_spawn` (no fork). Don't revert either detail — both are required for the spawn path. A crash of this kind now reports `osascript killed by signal N`.
