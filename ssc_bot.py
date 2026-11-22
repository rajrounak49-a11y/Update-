#!/usr/bin/env python3
"""
SSC -> Telegram bot   (ONE file: bot + auto-install + settings + GitHub workflow)

Watches the SSC website and posts every NEW notice / result / admit card /
answer key to your Telegram channel (PDF attached when possible).

QUICK START
  1. Fill in BOT_TOKEN and CHAT_ID in the SETTINGS block below.
  2. python ssc_bot.py --test      -> sends a test message to your channel
  3. python ssc_bot.py --loop      -> runs forever, checks every 15 minutes

OTHER COMMANDS
  python ssc_bot.py                    check once (for cron / GitHub Actions)
  python ssc_bot.py --post-existing    on the very first run, also post what is
                                       already on the site
  python ssc_bot.py --print-workflow   print the GitHub Actions file (free 24/7)

Missing libraries (requests, beautifulsoup4, playwright) install automatically.

SECURITY: if you upload this file to a PUBLIC GitHub repo, do NOT type your
token into it. Leave the placeholders and use GitHub Secrets instead.
"""
import os
import subprocess
import sys


def _ensure(pip_name, module_name):
    """Import a library, installing it first if it is missing."""
    try:
        __import__(module_name)
    except ImportError:
        print(f"Installing {pip_name} ...", flush=True)
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", pip_name])
        except subprocess.CalledProcessError:
            sys.exit(f"Could not install {pip_name}. Please run:  pip install {pip_name}")


_ensure("requests", "requests")
_ensure("beautifulsoup4", "bs4")

import argparse  # noqa: E402
import html  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import time  # noqa: E402
from datetime import datetime  # noqa: E402
from pathlib import Path  # noqa: E402
from urllib.parse import urldefrag, urljoin, urlparse  # noqa: E402

import requests  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402

# =============================== SETTINGS ===================================
# Environment variables win if set; otherwise the text after "or" is used.
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or "PASTE_YOUR_BOT_TOKEN_HERE"
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID") or "@your_channel_username"

# Line added at the end of every post. Use "" to turn it off.
FOOTER = os.environ.get("FOOTER", "🚀 Join @TheKhunkharsOfficial For Latest SSC Updates")

# Pages to watch (comma separated if you set it as an environment variable).
SOURCES = [u.strip() for u in os.environ.get("SSC_URLS", "https://ssc.gov.in/").split(",") if u.strip()]

CHECK_INTERVAL_MIN = int(os.environ.get("CHECK_INTERVAL_MIN", "15"))  # used with --loop
SEND_PDFS = os.environ.get("SEND_PDFS", "1") == "1"  # 0 = send links only
STATE_FILE = Path(os.environ.get("STATE_FILE", "seen.json"))  # remembers what was posted
VERIFY_SSL = os.environ.get("VERIFY_SSL", "1") == "1"
MAX_NEW_PER_RUN = int(os.environ.get("MAX_NEW_PER_RUN", "25"))  # safety against spam

# Only links whose text matches this (or that point to a PDF) count as updates.
DEFAULT_KEYWORDS = (
    r"notice|notification|result|answer\s*key|admit|intimation|exam|schedule|recruit|"
    r"vacanc|corrigend|revised|cut[- ]?off|marks|scorecard|advertisement|"
    r"\b(?:cgl|chsl|mts|gd|je|cpo|steno\w*|jht|aso|ldce|phase)\b"
)
KEYWORDS = re.compile(os.environ.get("KEYWORDS_REGEX", DEFAULT_KEYWORDS), re.I)
# ============================================================================

INTERVAL_SEC = CHECK_INTERVAL_MIN * 60
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# ------------------------------- scraping ---------------------------------
def parse_links(page_html, base_url):
    """Return {absolute_url: title} for links that look like SSC updates."""
    soup = BeautifulSoup(page_html, "html.parser")
    found = {}
    for a in soup.find_all("a", href=True):
        href, _ = urldefrag(urljoin(base_url, a["href"].strip()))
        if urlparse(href).scheme not in ("http", "https"):
            continue
        if href.rstrip("/") == base_url.rstrip("/"):
            continue
        is_pdf = urlparse(href).path.lower().endswith(".pdf")
        title = " ".join(a.get_text(" ", strip=True).split()) or (a.get("title") or "").strip()
        if not is_pdf and (len(title) < 8 or not KEYWORDS.search(title)):
            continue
        found[href] = title or Path(urlparse(href).path).name
    return found


_PLAYWRIGHT_INSTALL_TRIED = False


def _install_playwright(browser_only=False):
    """One-time install of Playwright + Chromium (needed only for JavaScript pages)."""
    global _PLAYWRIGHT_INSTALL_TRIED
    if _PLAYWRIGHT_INSTALL_TRIED:
        return False
    _PLAYWRIGHT_INSTALL_TRIED = True
    log("Installing browser support (one time, may take a few minutes) ...")
    try:
        if not browser_only:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "playwright"])
        subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium"])
        return True
    except subprocess.CalledProcessError:
        return False


def browser_links(url):
    """Fallback for pages that are built with JavaScript."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        if _install_playwright():
            return browser_links(url)
        log("Page needs JavaScript. Run:  pip install playwright && playwright install chromium")
        return {}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(user_agent=HEADERS["User-Agent"], ignore_https_errors=not VERIFY_SSL)
            page.goto(url, wait_until="networkidle", timeout=60000)
            content = page.content()
            browser.close()
        return parse_links(content, url)
    except Exception as e:  # noqa: BLE001
        if "executable doesn't exist" in str(e).lower() and _install_playwright(browser_only=True):
            return browser_links(url)
        log(f"Browser fetch failed for {url}: {e}")
        return {}


def get_links(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=30, verify=VERIFY_SSL)
        r.raise_for_status()
        links = parse_links(r.text, url)
        if links:
            return links
        log(f"No links in plain HTML of {url}; trying browser mode")
    except requests.RequestException as e:
        log(f"Request failed for {url}: {e}; trying browser mode")
    return browser_links(url)


# ------------------------------- telegram ---------------------------------
def tg_call(method, data, files=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    r = None
    for _ in range(3):
        r = requests.post(url, data=data, files=files, timeout=120)
        if r.status_code == 429:  # rate limited: wait and retry
            wait = r.json().get("parameters", {}).get("retry_after", 5)
            time.sleep(wait + 1)
            continue
        break
    return r


def build_caption(title, href):
    text = (
        f"🆕 <b>SSC Update</b>\n\n{html.escape(title[:700])}\n\n"
        f'🔗 <a href="{html.escape(href, quote=True)}">Open on SSC website</a>'
    )
    if FOOTER:
        text += f"\n\n{html.escape(FOOTER)}"
    return text


def send_update(title, href):
    caption = build_caption(title, href)

    if SEND_PDFS and urlparse(href).path.lower().endswith(".pdf"):
        try:
            r = requests.get(href, headers=HEADERS, timeout=90, verify=VERIFY_SSL)
            r.raise_for_status()
            if len(r.content) <= 49 * 1024 * 1024:  # Telegram bot upload limit is 50 MB
                name = Path(urlparse(href).path).name or "ssc-update.pdf"
                resp = tg_call(
                    "sendDocument",
                    {"chat_id": CHAT_ID, "caption": caption, "parse_mode": "HTML"},
                    files={"document": (name, r.content, "application/pdf")},
                )
                if resp.ok:
                    return True
                log(f"sendDocument failed ({resp.text[:200]}); falling back to link")
        except requests.RequestException as e:
            log(f"Could not download PDF {href}: {e}; falling back to link")

    resp = tg_call("sendMessage", {"chat_id": CHAT_ID, "text": caption, "parse_mode": "HTML"})
    if not resp.ok:
        log(f"sendMessage failed: {resp.text[:200]}")
    return resp.ok


# --------------------------------- state ----------------------------------
def load_state():
    """Return dict of already-seen links, or None if this is the first run."""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text("utf-8"))
        except ValueError:
            log("State file unreadable; treating as first run")
    return None


def save_state(seen):
    STATE_FILE.write_text(json.dumps(seen, ensure_ascii=False, indent=1), "utf-8")


# ---------------------------------- main ----------------------------------
def check_once(post_existing=False):
    seen = load_state()
    first_run = seen is None
    seen = seen or {}

    current = {}
    for src in SOURCES:
        current.update(get_links(src))
    new = {h: t for h, t in current.items() if h not in seen}
    log(f"Read {len(current)} links, {len(new)} new")

    if first_run:
        if not current:
            log("Could not read anything from the site; state not saved")
            return
        if not post_existing:
            save_state(current)
            log("First run: remembered existing items, nothing posted")
            return
    elif len(new) > MAX_NEW_PER_RUN:
        # Probably a site redesign, not 25+ real notices. Don't spam the channel.
        seen.update(new)
        save_state(seen)
        tg_call("sendMessage", {
            "chat_id": CHAT_ID,
            "text": f"⚠️ {len(new)} new links appeared at once - the SSC site layout may "
                    f"have changed. Please check the website manually.",
        })
        return

    # Pages list newest first, so post oldest first to keep chat order natural.
    for href, title in reversed(list(new.items())):
        if send_update(title, href):
            seen[href] = title
            save_state(seen)
            log(f"Posted: {title[:80]}")
        time.sleep(3)  # stay under Telegram's per-chat rate limit


# Free 24/7 hosting: save the text below as  .github/workflows/ssc_bot.yml  in a
# GitHub repo that contains this file, and add two repository secrets named
# TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (Settings > Secrets and variables > Actions).
WORKFLOW_YAML = """\
name: SSC updates

on:
  schedule:
    - cron: "*/15 * * * *"   # every 15 min (GitHub may delay runs a few minutes)
  workflow_dispatch:          # lets you press "Run workflow" manually

permissions:
  contents: write             # needed to save seen.json back to the repo

concurrency:
  group: ssc-bot
  cancel-in-progress: false

jobs:
  check:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install dependencies
        run: |
          pip install requests beautifulsoup4 playwright
          playwright install --with-deps chromium

      - name: Check SSC site and post new updates
        run: python ssc_bot.py
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}

      - name: Save list of posted items
        run: |
          if [ -f seen.json ]; then
            git config user.name "ssc-bot"
            git config user.email "ssc-bot@users.noreply.github.com"
            git add seen.json
            git diff --cached --quiet || (git commit -m "update seen items" && git push)
          fi
"""


def main():
    ap = argparse.ArgumentParser(description="SSC website -> Telegram updates")
    ap.add_argument("--loop", action="store_true", help="keep running")
    ap.add_argument("--test", action="store_true", help="send a test message and exit")
    ap.add_argument("--post-existing", action="store_true",
                    help="on first run, also post what is currently on the site")
    ap.add_argument("--print-workflow", action="store_true",
                    help="print the GitHub Actions workflow file and exit")
    args = ap.parse_args()

    if args.print_workflow:
        print(WORKFLOW_YAML)
        return

    if BOT_TOKEN.startswith("PASTE_") or CHAT_ID.startswith("@your_"):
        sys.exit("Open ssc_bot.py and fill in BOT_TOKEN and CHAT_ID in the SETTINGS block "
                 "(or set the TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID environment variables).")

    if args.test:
        r = tg_call("sendMessage", {"chat_id": CHAT_ID, "text": "✅ SSC update bot is connected."})
        print(r.text)
        return

    while True:
        try:
            check_once(args.post_existing)
        except Exception as e:  # noqa: BLE001 - keep the loop alive
            log(f"Error during check: {e}")
        if not args.loop:
            break
        time.sleep(INTERVAL_SEC)


if __name__ == "__main__":
    main()
