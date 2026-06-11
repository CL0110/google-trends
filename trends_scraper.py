#!/usr/bin/env python3
"""
Google Trends daily scraper -> JSON + Obsidian-friendly Markdown note.

Source: Google's official "Trending Now" RSS feed
        https://trends.google.com/trending/rss?geo=US
We deliberately do NOT use pytrends: its trending_searches endpoint has been
broken/flaky for a long time (Google changed the backend) and it gets rate-
blocked easily. The RSS feed needs no API key, is stable, and includes traffic
estimates + related news links, which makes for much richer Obsidian notes.

Outputs (into --output-dir, default ./data/trends/):
  trends_YYYY-MM-DD.json   raw, machine-readable (for GitHub Actions / scripts)
  trends_YYYY-MM-DD.md     pretty Obsidian daily note (Dataview frontmatter + table)
  trends.log               plain rolling debug log (errors, retries, timings)

Exit codes:  0 = success,  1 = failure  (so GitHub Actions / CI can branch on it)

------------------------------------------------------------------------------
Schedule daily at 09:00 UTC with GitHub Actions
------------------------------------------------------------------------------
Save as .github/workflows/trends.yml :

    name: Scrape Google Trends
    on:
      schedule:
        - cron: "0 9 * * *"     # 09:00 UTC every day
      workflow_dispatch:          # ...and a manual "Run" button
    jobs:
      scrape:
        runs-on: ubuntu-latest
        steps:
          - uses: actions/checkout@v4
          - uses: actions/setup-python@v5
            with: { python-version: "3.12" }
          - run: python trends_scraper.py --output-dir ./data/trends/
          - name: Commit results
            run: |
              git config user.name  "trends-bot"
              git config user.email "bot@users.noreply.github.com"
              git add data/trends/
              git diff --staged --quiet || git commit -m "trends: $(date -u +%F)"
              git push

(Note: GitHub-hosted runners can't write into your local Obsidian vault. Run the
 script locally / via Task Scheduler with --output-dir pointed at your vault, OR
 let the Action commit JSON+MD to the repo and sync that folder into Obsidian.)
------------------------------------------------------------------------------
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from zoneinfo import ZoneInfo

# --- configuration ----------------------------------------------------------
FEED_URL = "https://trends.google.com/trending/rss?geo={geo}"
TIMEZONE = ZoneInfo("America/New_York")  # US East Coast (EST/EDT, auto DST)
TOP_N = 20                 # how many trending searches to keep
RATE_LIMIT_SECONDS = 2.0   # min seconds between outbound requests
MAX_RETRIES = 5            # attempts before giving up
BACKOFF_BASE = 2.0         # exponential backoff: BACKOFF_BASE ** attempt seconds
REQUEST_TIMEOUT = 30       # per-request timeout (seconds)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
# RSS namespace used by Google Trends for the extra fields (traffic, news, etc.)
HT_NS = "https://trends.google.com/trending/rss"

log = logging.getLogger("trends")


# --- networking with rate limiting + exponential backoff --------------------
_last_request_at = 0.0


def _rate_limit() -> None:
    """Block until at least RATE_LIMIT_SECONDS have passed since the last call."""
    global _last_request_at
    wait = RATE_LIMIT_SECONDS - (time.monotonic() - _last_request_at)
    if wait > 0:
        time.sleep(wait)
    _last_request_at = time.monotonic()


def fetch_feed(url: str) -> bytes:
    """Fetch a URL with rate limiting and exponential-backoff retries.

    Raises the last exception if all retries are exhausted.
    """
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES):
        _rate_limit()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                body = resp.read()
            log.info("Fetched %s (%d bytes) on attempt %d", url, len(body), attempt + 1)
            return body
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            last_exc = exc
            # 429 = rate limited; treat like any transient error and back off.
            status = getattr(exc, "code", "n/a")
            backoff = BACKOFF_BASE ** attempt
            log.warning(
                "Attempt %d/%d failed (status=%s): %s — retrying in %.1fs",
                attempt + 1, MAX_RETRIES, status, exc, backoff,
            )
            if attempt < MAX_RETRIES - 1:
                time.sleep(backoff)
    log.error("All %d attempts failed for %s", MAX_RETRIES, url)
    raise last_exc  # type: ignore[misc]


# --- parsing ----------------------------------------------------------------
def _text(el: ET.Element | None) -> str:
    return (el.text or "").strip() if el is not None else ""


def parse_trends(xml_bytes: bytes, top_n: int = TOP_N) -> list[dict]:
    """Parse the Trending Now RSS feed into a list of trend dicts."""
    root = ET.fromstring(xml_bytes)
    trends: list[dict] = []
    for rank, item in enumerate(root.iterfind(".//item"), start=1):
        if rank > top_n:
            break
        news = []
        for ni in item.iterfind(f"{{{HT_NS}}}news_item"):
            news.append({
                "title": _text(ni.find(f"{{{HT_NS}}}news_item_title")),
                "url": _text(ni.find(f"{{{HT_NS}}}news_item_url")),
                "source": _text(ni.find(f"{{{HT_NS}}}news_item_source")),
            })
        trends.append({
            "rank": rank,
            "title": _text(item.find("title")),
            "approx_traffic": _text(item.find(f"{{{HT_NS}}}approx_traffic")),
            "picture": _text(item.find(f"{{{HT_NS}}}picture")),
            "pub_date": _text(item.find("pubDate")),
            "news": news,
        })
    if not trends:
        raise ValueError("Feed parsed but contained no <item> entries")
    log.info("Parsed %d trending searches", len(trends))
    return trends


# --- output writers ---------------------------------------------------------
def write_json(trends: list[dict], path: Path, fetched: str, geo: str) -> None:
    payload = {
        "geo": geo,
        "fetched_et": fetched,  # US East Coast time
        "count": len(trends),
        "trends": trends,
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Wrote JSON -> %s", path)


def write_markdown(trends: list[dict], path: Path, date: str,
                   fetched: str, geo: str) -> None:
    """Write an Obsidian daily note: Dataview frontmatter + table + details."""
    lines: list[str] = []
    # YAML frontmatter -> queryable with Dataview
    lines += [
        "---",
        f"date: {date}",
        "source: google-trends",
        f"geo: {geo}",
        "status: success",
        f"fetched: {len(trends)}",
        f"fetched_et: {fetched}",
        "tags: [google-trends, daily]",
        "---",
        "",
        f"# \U0001F4C8 {geo} Trending Searches — {date}",
        "",
        f"*{len(trends)} trends · fetched {fetched}*",
        "",
        "| # | Search term | Traffic |",
        "|---|-------------|---------|",
    ]
    for t in trends:
        term = t["title"].replace("|", "\\|")
        traffic = t["approx_traffic"] or "—"
        # [[wikilink]] so trending terms build a backlink graph over time
        lines.append(f"| {t['rank']} | [[{term}]] | {traffic} |")

    lines += ["", "---", "", "## Details", ""]
    for t in trends:
        lines.append(f"### {t['rank']}. {t['title']}")
        if t["approx_traffic"]:
            lines.append(f"**Traffic:** {t['approx_traffic']}")
        if t["news"]:
            lines.append("")
            for n in t["news"]:
                src = f" — *{n['source']}*" if n["source"] else ""
                if n["url"]:
                    lines.append(f"- [{n['title']}]({n['url']}){src}")
                else:
                    lines.append(f"- {n['title']}{src}")
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
    log.info("Wrote Markdown note -> %s", path)


# --- main -------------------------------------------------------------------
def configure_logging(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler(output_dir / "trends.log", encoding="utf-8"),
            logging.StreamHandler(sys.stderr),
        ],
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scrape daily Google Trends -> JSON + Obsidian note")
    parser.add_argument("--output-dir", default="./data/trends/",
                        help="Where to write files (point this at your Obsidian vault folder)")
    parser.add_argument("--geo", default="US", help="Region code (default: US)")
    parser.add_argument("--top", type=int, default=TOP_N, help="How many trends to keep (default: 20)")
    args = parser.parse_args(argv)

    output_dir = Path(args.output_dir).expanduser()
    configure_logging(output_dir)

    # US East Coast time drives the filenames/timestamps (EST/EDT, auto DST).
    now = dt.datetime.now(TIMEZONE)
    date = now.strftime("%Y-%m-%d")
    fetched = now.strftime("%Y-%m-%d %H:%M %Z")  # e.g. "2026-06-05 15:31 EDT"

    log.info("=== Run start: geo=%s top=%d out=%s ===", args.geo, args.top, output_dir)
    try:
        xml_bytes = fetch_feed(FEED_URL.format(geo=args.geo))
        trends = parse_trends(xml_bytes, top_n=args.top)

        write_json(trends, output_dir / f"trends_{date}.json", fetched, args.geo)
        write_markdown(trends, output_dir / f"trends_{date}.md", date, fetched, args.geo)

        log.info("=== Run OK: %d trends saved ===", len(trends))
        return 0
    except Exception as exc:  # noqa: BLE001 - top-level guard for clean exit code
        log.exception("Run FAILED: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
