"""
NVAR Market Statistics Scraper + GHL Sender
============================================
Runs daily via GitHub Actions. Checks nvar.com for a new monthly
"Market Statistics" post. If found (and not already sent), scrapes
the key data, generates a ≤15-word text via Claude API, and fires
a GHL webhook to trigger an SMS to your smart list.
"""

import os
import re
import json
import requests
from datetime import datetime
from pathlib import Path

# ── Config (set these as GitHub Secrets) ──────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GHL_WEBHOOK_URL = os.environ.get("GHL_WEBHOOK_URL", "")
NVAR_NEWS_URL = "https://www.nvar.com/news/"
LAST_SENT_FILE = Path(__file__).parent / "last_sent.txt"


def get_last_sent():
    """Read the last sent post URL from file."""
    if LAST_SENT_FILE.exists():
        return LAST_SENT_FILE.read_text().strip()
    return ""


def save_last_sent(url):
    """Save the last sent post URL to file."""
    LAST_SENT_FILE.write_text(url)


def fetch_page(url):
    """Fetch a webpage and return its text content."""
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; NVARScraper/1.0)"
    }
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.text


def find_latest_market_stats_post(html):
    """
    Find the most recent 'Market Statistics: <Month> <Year>' post link
    on the NVAR news page. Returns (url, title) or (None, None).
    """
    # Pattern matches links like: /news/2026-02-09/market-statistics-january-2026/
    # with titles like "Market Statistics: January 2026"
    pattern = r'href="(https?://www\.nvar\.com/news/\d{4}-\d{2}-\d{2}/market-statistics-[a-z]+-\d{4}/?)"\s*'
    urls = re.findall(pattern, html, re.IGNORECASE)

    if not urls:
        # Try relative URLs
        pattern = r'href="(/news/\d{4}-\d{2}-\d{2}/market-statistics-[a-z]+-\d{4}/?)"\s*'
        rel_urls = re.findall(pattern, html, re.IGNORECASE)
        urls = [f"https://www.nvar.com{u}" for u in rel_urls]

    if not urls:
        # Broader fallback: look for "Market Statistics:" text near links
        pattern = r'href="([^"]*market-statistics[^"]*)"'
        urls = re.findall(pattern, html, re.IGNORECASE)
        urls = [
            u if u.startswith("http") else f"https://www.nvar.com{u}"
            for u in urls
            # Exclude "national-market-statistics" and "year-end" posts
            if "national-market" not in u.lower()
            and "year-end" not in u.lower()
            and "comparison" not in u.lower()
        ]

    if urls:
        return urls[0]
    return None


def search_for_press_release(month_name, year):
    """
    Fallback: search PR Newswire for the NVAR press release
    in case the NVAR site is slow to update.
    """
    search_url = (
        f"https://www.prnewswire.com/news-releases/"
        f"?keyword=NVAR+market+statistics+{month_name}+{year}"
    )
    try:
        html = fetch_page(search_url)
        pattern = r'href="([^"]*northern-virginia[^"]*market[^"]*)"'
        matches = re.findall(pattern, html, re.IGNORECASE)
        if matches:
            url = matches[0]
            if not url.startswith("http"):
                url = f"https://www.prnewswire.com{url}"
            return url
    except Exception:
        pass
    return None


def extract_stats_from_article(html):
    """
    Extract key market statistics from the article/press release HTML.
    Returns a dict with the parsed numbers.
    """
    stats = {}

    # Closed sales
    m = re.search(
        r'(\d[\d,]+)\s+homes?\s+closed',
        html, re.IGNORECASE
    )
    if m:
        stats["closed_sales"] = m.group(1).replace(",", "")

    # Closed sales YoY change
    m = re.search(
        r'closed.*?(down|up)\s+([\d.]+)%',
        html, re.IGNORECASE
    )
    if m:
        direction = "-" if m.group(1).lower() == "down" else "+"
        stats["closed_sales_change"] = f"{direction}{m.group(2)}%"

    # Median sold price
    m = re.search(
        r'median\s+sold\s+price[^$]*\$([\d,]+)',
        html, re.IGNORECASE
    )
    if m:
        stats["median_price"] = m.group(1).replace(",", "")

    # Median price YoY change
    m = re.search(
        r'median\s+sold\s+price[^%]*?(down|up|decrease|increase)\s+(?:of\s+)?([\d.]+)%',
        html, re.IGNORECASE
    )
    if not m:
        m = re.search(
            r'\$[\d,]+[^%]*?median[^%]*?(down|up|decrease|increase)\s+(?:of\s+)?([\d.]+)%',
            html, re.IGNORECASE
        )
    if not m:
        # Handle "a 1.5% decrease" format (number before direction)
        m = re.search(
            r'median\s+sold\s+price[^%]*?([\d.]+)%\s+(decrease|increase|decline|gain)',
            html, re.IGNORECASE
        )
        if m:
            direction = "-" if m.group(2).lower() in ("decrease", "decline") else "+"
            stats["median_price_change"] = f"{direction}{m.group(1)}%"
            m = None  # Skip the normal handler below
    if m:
        direction = "-" if m.group(1).lower() in ("down", "decrease") else "+"
        stats["median_price_change"] = f"{direction}{m.group(2)}%"

    # Average days on market
    m = re.search(
        r'(?:average\s+)?days\s+on\s+market[^0-9]*?(\d+)\s+days',
        html, re.IGNORECASE
    )
    if not m:
        m = re.search(
            r'(?:increased|decreased|was)[^0-9]*?to\s+(\d+)\s+days',
            html, re.IGNORECASE
        )
    if not m:
        m = re.search(
            r'(\d+)\s+days\s+in\s+\w+\s+\d{4}',
            html, re.IGNORECASE
        )
    if m:
        stats["days_on_market"] = m.group(1)

    # Days on market YoY change
    m = re.search(
        r'days\s+on\s+market[^%]*?(down|up)\s+([\d.]+)%',
        html, re.IGNORECASE
    )
    if m:
        direction = "-" if m.group(1).lower() == "down" else "+"
        stats["dom_change"] = f"{direction}{m.group(2)}%"

    # Active listings
    m = re.search(
        r'active\s+listings[^0-9]*([\d,]+)\s+units',
        html, re.IGNORECASE
    )
    if not m:
        m = re.search(
            r'active\s+listings\s+(?:rose|fell|increased|decreased)[^0-9]*([\d,]+)\s+units',
            html, re.IGNORECASE
        )
    if not m:
        m = re.search(
            r'to\s+([\d,]+)\s+units.*?active\s+listing',
            html, re.IGNORECASE
        )
    if not m:
        # "rose 21.1% year over year to 1,526 units"
        m = re.search(
            r'listings\s+rose[^0-9]*[\d.]+%[^0-9]*([\d,]+)\s+units',
            html, re.IGNORECASE
        )
    if m:
        stats["active_listings"] = m.group(1).replace(",", "")

    # Active listings YoY change
    m = re.search(
        r'active\s+listings[^%]*?(down|up)\s+([\d.]+)%',
        html, re.IGNORECASE
    )
    if m:
        direction = "-" if m.group(1).lower() == "down" else "+"
        stats["active_listings_change"] = f"{direction}{m.group(2)}%"

    # New pending sales
    m = re.search(
        r'pending\s+sales.*?was\s+([\d,]+)\s+units',
        html, re.IGNORECASE
    )
    if not m:
        m = re.search(
            r'(?:new\s+)?pending\s+sales[^0-9]*([\d,]+)\s+units',
            html, re.IGNORECASE
        )
    if not m:
        m = re.search(
            r'([\d,]+)\s+(?:new\s+)?pending\s+sales',
            html, re.IGNORECASE
        )
    if m:
        stats["pending_sales"] = m.group(1).replace(",", "")

    # Pending sales YoY change
    m = re.search(
        r'pending\s+sales[^%]*?(down|up)\s+([\d.]+)%',
        html, re.IGNORECASE
    )
    if m:
        direction = "-" if m.group(1).lower() == "down" else "+"
        stats["pending_sales_change"] = f"{direction}{m.group(2)}%"

    # Months of supply
    m = re.search(
        r'months\s+of\s+supply.*?was\s+([\d.]+\d)',
        html, re.IGNORECASE
    )
    if not m:
        m = re.search(
            r'months\s+of\s+supply.*?to\s+([\d.]+\d)',
            html, re.IGNORECASE
        )
    if not m:
        m = re.search(
            r'([\d.]+\d)\s+months\s+of\s+supply',
            html, re.IGNORECASE
        )
    if m:
        stats["months_supply"] = m.group(1)

    # Extract the month/year from the page
    m = re.search(
        r'Market\s+Statistics:?\s+(\w+)\s+(\d{4})',
        html, re.IGNORECASE
    )
    if m:
        stats["report_month"] = m.group(1)
        stats["report_year"] = m.group(2)

    return stats


def generate_message_ai(stats):
    """
    Use Claude API to generate a concise ≤15-word text message
    from the scraped stats.
    """
    if not ANTHROPIC_API_KEY:
        print("⚠️  No ANTHROPIC_API_KEY set, falling back to template.")
        return generate_message_template(stats)

    month = stats.get("report_month", "")
    year = stats.get("report_year", "")
    median = stats.get("median_price", "")
    median_chg = stats.get("median_price_change", "")
    inv_chg = stats.get("active_listings_change", "")
    dom = stats.get("days_on_market", "")
    pending_chg = stats.get("pending_sales_change", "")

    prompt = f"""You are a real estate marketing assistant. Generate a text message 
for a real estate agent's client list based on the latest Northern Virginia (NoVA) 
housing market data.

RULES:
- MUST be 15 words or fewer (this is a hard limit)
- Casual, confident tone — like a knowledgeable friend texting
- Include 1-2 key stats (pick the most compelling)
- End with a soft call to action (e.g., "let's chat", "reach out", "thinking of moving?")
- Do NOT use hashtags or emojis
- Do NOT include "NVAR" — just say "NoVA" or "Northern Virginia"

DATA for {month} {year}:
- Median sold price: ${median} ({median_chg} YoY)
- Active listings change: {inv_chg} YoY
- Avg days on market: {dom} days
- Pending sales change: {pending_chg} YoY

Generate ONLY the text message, nothing else."""

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 100,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        message = data["content"][0]["text"].strip().strip('"')
        
        # Verify word count
        word_count = len(message.split())
        if word_count > 18:  # small buffer for flexibility
            print(f"⚠️  AI message too long ({word_count} words), falling back to template.")
            return generate_message_template(stats)
        
        print(f"✅ AI-generated message ({word_count} words): {message}")
        return message

    except Exception as e:
        print(f"⚠️  Claude API error: {e}. Falling back to template.")
        return generate_message_template(stats)


def generate_message_template(stats):
    """Fallback: simple template-based message."""
    month = stats.get("report_month", "This month")
    median = stats.get("median_price", "")
    inv_chg = stats.get("active_listings_change", "")

    # Format median price
    if median:
        try:
            median_num = int(median)
            if median_num >= 1000:
                median_str = f"${median_num // 1000}K"
            else:
                median_str = f"${median_num}"
        except ValueError:
            median_str = f"${median}"
    else:
        median_str = ""

    msg = f"NoVA {month} update: {median_str} median, inventory {inv_chg}. Thinking of moving? Let's talk!"
    print(f"📝 Template message: {msg}")
    return msg


def send_to_ghl(message, stats):
    """
    Send the generated message + stats to GHL via webhook.
    GHL workflow will handle sending SMS to the smart list.
    """
    if not GHL_WEBHOOK_URL:
        print("⚠️  No GHL_WEBHOOK_URL set. Skipping send.")
        print(f"📱 Message that WOULD be sent: {message}")
        return False

    payload = {
        "message": message,
        "report_month": stats.get("report_month", ""),
        "report_year": stats.get("report_year", ""),
        "median_price": stats.get("median_price", ""),
        "median_price_change": stats.get("median_price_change", ""),
        "closed_sales": stats.get("closed_sales", ""),
        "closed_sales_change": stats.get("closed_sales_change", ""),
        "days_on_market": stats.get("days_on_market", ""),
        "active_listings": stats.get("active_listings", ""),
        "active_listings_change": stats.get("active_listings_change", ""),
        "pending_sales": stats.get("pending_sales", ""),
        "pending_sales_change": stats.get("pending_sales_change", ""),
        "months_supply": stats.get("months_supply", ""),
    }

    try:
        resp = requests.post(
            GHL_WEBHOOK_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
        print(f"✅ Successfully sent to GHL webhook. Status: {resp.status_code}")
        return True
    except Exception as e:
        print(f"❌ GHL webhook error: {e}")
        return False


def main():
    print(f"🔍 Checking NVAR for new market stats... ({datetime.now().isoformat()})")

    # Step 1: Fetch the NVAR news page
    try:
        html = fetch_page(NVAR_NEWS_URL)
    except Exception as e:
        print(f"❌ Failed to fetch NVAR news page: {e}")
        return

    # Step 2: Find the latest market stats post
    post_url = find_latest_market_stats_post(html)
    if not post_url:
        print("ℹ️  No market statistics post found. Will check again tomorrow.")
        return

    print(f"📰 Found post: {post_url}")

    # Step 3: Check if we already sent this one
    last_sent = get_last_sent()
    if post_url == last_sent:
        print("ℹ️  Already sent this post. Nothing new to do.")
        return

    print("🆕 New post detected! Scraping stats...")

    # Step 4: Fetch the full article
    try:
        article_html = fetch_page(post_url)
    except Exception as e:
        # Try PR Newswire as fallback
        print(f"⚠️  Could not fetch NVAR article ({e}), trying PR Newswire...")
        now = datetime.now()
        month_name = now.strftime("%B")
        pr_url = search_for_press_release(month_name, str(now.year))
        if pr_url:
            try:
                article_html = fetch_page(pr_url)
            except Exception as e2:
                print(f"❌ PR Newswire fallback also failed: {e2}")
                return
        else:
            print("❌ No fallback source found.")
            return

    # Step 5: Extract the stats
    stats = extract_stats_from_article(article_html)
    if not stats.get("median_price"):
        print("⚠️  Could not extract key stats. The page format may have changed.")
        print(f"   Extracted so far: {json.dumps(stats, indent=2)}")
        return

    print(f"📊 Extracted stats: {json.dumps(stats, indent=2)}")

    # Step 6: Generate the text message
    message = generate_message_ai(stats)

    # Step 7: Send to GHL
    sent = send_to_ghl(message, stats)

    # Step 8: Save the post URL so we don't send again
    if sent or not GHL_WEBHOOK_URL:
        save_last_sent(post_url)
        print("💾 Saved post URL to prevent duplicate sends.")

    print("✅ Done!")


if __name__ == "__main__":
    main()
