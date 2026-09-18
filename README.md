# kindle-sender

Replicates Instapaper's paywalled "Send to Kindle" feature. Fetches saved unread articles from Instapaper, builds a single EPUB digest, and delivers it to a Kindle via macOS Mail.app.

## What it does

1. Authenticates with Instapaper via xAuth (OAuth 1.0a)
2. Fetches unread bookmarks and their parsed HTML via Instapaper's API
3. Downloads and embeds all images into the EPUB
4. Builds a single EPUB digest and emails it to the Kindle via Mail.app

After reading on Kindle, archive articles manually in Instapaper.

## Files

- `send.py` — the entire script, single file
- `.env` — secrets and config (gitignored)
- `.env.example` — template with all required keys
- `requirements.txt` — Python dependencies

## Requirements

- macOS (Mail.app is required for sending)
- Python 3.10+

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in:

- `INSTAPAPER_CONSUMER_KEY` / `INSTAPAPER_CONSUMER_SECRET` — register an "Owner Only" app at [instapaper.com/main/request_oauth_consumer_token](https://www.instapaper.com/main/request_oauth_consumer_token)
- `INSTAPAPER_USERNAME` / `INSTAPAPER_PASSWORD` — your Instapaper login
- `KINDLE_EMAIL` — your `@kindle.com` address from [amazon.com/myk](https://www.amazon.com/myk)
- `MAIL_FROM` — the address Mail.app sends from (required if you have multiple accounts — Amazon silently drops mail from unapproved addresses)

Add `MAIL_FROM` to Amazon's approved senders list at amazon.com/myk → Preferences → Personal Document Settings.

## Usage

```bash
python3 send.py                          # prompts: count (default 10), then latest/oldest (default oldest)
python3 send.py --count 5 --order latest # skip prompts
python3 send.py --dry-run  # build EPUB locally, don't send
```

Dry run writes `Instapaper-ReadLater-YYYY-MM-DD-XXXXXX.epub` to the project directory.

## Known limitations

- **No dedup across runs.** Running twice sends the same articles twice. Archive manually in Instapaper after reading.
- **Mail.app must be running.** The script queues the message via Mail.app and exits — a stuck Outbox is silent.
- **Image quality.** Images render as grayscale halftones on e-ink. Non-JPEG/PNG formats (webp, avif, bmp, tiff) are converted to JPEG via Pillow; SVGs are skipped. Some CDN-hosted images are skipped (logged as `[img-skip]`). If images still don't appear on Kindle, try pre-converting the EPUB to AZW3 locally via Calibre before sending.
- **No retry on transient errors.** If Instapaper returns a 5xx or a network error during a run, the affected article is skipped and logged. Rerun the script to retry.
- **macOS only.** Delivery depends on Mail.app; there is no cross-platform alternative in this script.

## Legal / ToS notes

- Instapaper's `bookmarks/get_text` endpoint is explicitly **personal use only** per their API docs. Run this script only against your own Instapaper account.
- Do not commit article content (EPUBs, extracted HTML) to this repo — the `*.epub` gitignore enforces this for generated files.
- The MIT license covers this source code. Use of the Instapaper API is separately subject to [Instapaper's API Terms of Use](https://www.instapaper.com/developers/api-terms).
- You are responsible for configuring your own Amazon approved-sender list at amazon.com/myk.
