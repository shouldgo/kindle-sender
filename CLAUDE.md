# kindle-sender

Replicates Instapaper's paywalled "Send to Kindle" feature. Fetches saved unread articles from Instapaper, builds a single EPUB digest, and delivers it to a Kindle via macOS Mail.app.

## What it does

1. Authenticates with Instapaper API v2 using a personal access token (bearer token)
2. Fetches the whole unread list (paginated), shows the count, asks how many and latest vs oldest, then fetches the chosen articles' parsed HTML
3. Downloads and embeds all images into the EPUB
4. Builds a single EPUB digest (each chapter headed by title and an author · domain · N min read line) and emails it to the Kindle via Mail.app

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
- `INSTAPAPER_TOKEN` — instapaper.com/developers/applications → your app → Generate access token. An existing API v1 `oauth_token` (e.g. from the old `.instapaper_token` cache) works as-is; don't regenerate unless it's lost, since regenerating revokes the old one immediately.
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

- **Local sort.** The API has no sort parameter. The script pages through the whole unread list (`section=home`, 500 per page, until `total`) and sorts by save time (`time`) locally.
- **No dedup across runs.** Running twice sends the same articles twice. Archive manually in Instapaper after reading.
- **Mail.app must be running.** Script exits once Mail.app queues the message — stuck Outbox is silent.
- **Image quality.** Images render as grayscale halftones on e-ink. Non-JPEG/PNG formats (webp, avif, bmp, tiff) are converted to JPEG via Pillow before embedding; SVGs are skipped silently. Some CDN-hosted images are skipped (logged as `[img-skip]`). If images still don't appear on Kindle after conversion, the next step would be pre-converting the EPUB to AZW3 locally via Calibre before sending.
- **No retry on transient errors.** If Instapaper returns a 5xx or a network error during a run, the affected article is skipped and logged. Rerun the script to retry.
- **Personal use only.** Since 2026-09-30, `/api/2/bookmarks/{id}/parse` works without an Instaparser key only when the authenticated user is the account that registered the app. The app must stay linked to your account (it shows under Your Apps); otherwise parse returns 403.

## Troubleshooting

- **"Send failed:" with no message + macOS "Python quit unexpectedly" (fixed).** On macOS 27, `subprocess` launching `osascript` via `fork()` segfaulted in the child (`crashed on child side of fork pre-exec` in `~/Library/Logs/DiagnosticReports/Python-*.ips`), because Network.framework threads from the earlier HTTP/image downloads don't survive fork. Fix: `send_email` calls `/usr/bin/osascript` by absolute path with `close_fds=False`, which makes Python use `posix_spawn` (no fork). Don't revert either detail — both are required for the spawn path. A crash of this kind now reports `osascript killed by signal N`.
