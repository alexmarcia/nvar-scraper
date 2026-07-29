"""
NVAR Market Statistics Scraper + GHL Direct Sender
====================================================
Runs daily via GitHub Actions at 10 AM ET.

WHAT IT DOES:
  1. Checks nvar.com for new regional "Market Statistics" posts
  2. Scrapes key stats (median price, inventory, DOM, etc.)
  3. Pulls contacts from GHL, checks their tags
  4. Sends personalized SMS and/or email based on tags

TAG SYSTEM:
  Audience:    seller | buyer | (neither = general)
  Delivery:    email-only | text-only | (neither = both)
  Frequency:   quarterly | (neither = every month)
  Alerts:      brightalerts -> gets nearby sold/listed texts (separate system)

QUARTERLY MONTHS: January, April, July, October
"""

import os
import re
import json
import time
import requests
from datetime import datetime
from pathlib import Path

# -- Config --------------------------------------------------------
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GHL_API_KEY = os.environ.get("GHL_API_KEY", "")
GHL_LOCATION_ID = os.environ.get("GHL_LOCATION_ID", "")
NVAR_NEWS_URL = "https://www.nvar.com/news/"
LAST_SENT_FILE = Path(__file__).parent / "last_sent.txt"

CLAUDE_MODEL = "claude-sonnet-5"

QUARTERLY_MONTHS = [1, 4, 7, 10]

TAG_PROMPTS = {
    "seller": (
        "The recipient is a homeowner who may consider SELLING. "
        "Pick stats that matter to sellers: inventory shifts, days on market, "
        "buyer demand, pricing trends. The insight should help them understand "
        "what this means for their home's position in the market."
    ),
    "buyer": (
        "The recipient is looking to BUY a home. "
        "Pick stats that matter to buyers: price movement, inventory growth, "
        "more choices, any cooling or balancing signals. The insight should "
        "help them see where the opportunity is right now."
    ),
}
TAG_PRIORITY = ["seller", "buyer"]
DEFAULT_PROMPT = (
    "The recipient is a general real estate contact. "
    "Give them a balanced update with whatever stats feel most interesting."
)


# -- Helpers -------------------------------------------------------

def get_last_sent():
    if LAST_SENT_FILE.exists():
        return LAST_SENT_FILE.read_text().strip()
    return ""

def save_last_sent(url):
    LAST_SENT_FILE.write_text(url)

def fetch_page(url):
    headers = {"User-Agent": "Mozilla/5.0 (compatible; NVARScraper/1.0)"}
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.text

def is_quarterly_month():
    return datetime.now().month in QUARTERLY_MONTHS


# -- Find Latest Post ----------------------------------------------

def find_latest_market_stats_post(html):
    EXCLUDE_PATTERNS = [
        "national", "comparison", "mid-year", "midyear", "forecast",
        "summit", "condo", "navigating", "recognized", "shop",
        "checklist", "book-of-lists", "legislative", "general-assembly",
        "forms-changes", "new-member",
    ]

    def is_valid_url(url):
        url_lower = url.lower()
        return (
            "market-statistics" in url_lower
            and not any(exc in url_lower for exc in EXCLUDE_PATTERNS)
        )

    pattern = r'href="(https?://www\.nvar\.com/news/\d{4}-\d{2}-\d{2}/market-statistics-[a-z]+-\d{4}/?)"'
    urls = [u for u in re.findall(pattern, html, re.IGNORECASE) if is_valid_url(u)]

    if not urls:
        pattern = r'href="(/news/\d{4}-\d{2}-\d{2}/market-statistics-[a-z]+-\d{4}/?)"'
        rel_urls = re.findall(pattern, html, re.IGNORECASE)
        urls = [f"https://www.nvar.com{u}" for u in rel_urls if is_valid_url(u)]

    if not urls:
        pattern = r'href="([^"]*market-statistics[^"]*)"'
        all_urls = re.findall(pattern, html, re.IGNORECASE)
        urls = [
            u if u.startswith("http") else f"https://www.nvar.com{u}"
            for u in all_urls if is_valid_url(u)
        ]

    if urls:
        print(f"  Found: {urls[0]}")
    return urls[0] if urls else None


# -- Extract Stats -------------------------------------------------

def extract_stats_from_article(html):
    stats = {}

    m = re.search(r'(\d[\d,]+)\s+homes?\s+closed', html, re.IGNORECASE)
    if m:
        stats["closed_sales"] = m.group(1).replace(",", "")

    m = re.search(r'closed.*?(down|up)\s+([\d.]+)%', html, re.IGNORECASE)
    if m:
        direction = "-" if m.group(1).lower() == "down" else "+"
        stats["closed_sales_change"] = f"{direction}{m.group(2)}%"

    m = re.search(r'median\s+sold\s+price[^$]*\$([\d,]+)', html, re.IGNORECASE)
    if m:
        stats["median_price"] = m.group(1).replace(",", "")

    m = re.search(
        r'median\s+sold\s+price[^%]*?(down|up|decrease|increase)\s+(?:of\s+)?([\d.]+)%',
        html, re.IGNORECASE
    )
    if m:
        direction = "-" if m.group(1).lower() in ("down", "decrease") else "+"
        stats["median_price_change"] = f"{direction}{m.group(2)}%"
    else:
        m = re.search(
            r'median\s+sold\s+price[^%]*?([\d.]+)%\s+(decrease|increase|decline|gain)',
            html, re.IGNORECASE
        )
        if m:
            direction = "-" if m.group(2).lower() in ("decrease", "decline") else "+"
            stats["median_price_change"] = f"{direction}{m.group(1)}%"

    m = re.search(r'(?:average\s+)?days\s+on\s+market[^0-9]*?(\d+)\s+days', html, re.IGNORECASE)
    if not m:
        m = re.search(r'(?:increased|decreased|was)[^0-9]*?to\s+(\d+)\s+days', html, re.IGNORECASE)
    if not m:
        m = re.search(r'(\d+)\s+days\s+in\s+\w+\s+\d{4}', html, re.IGNORECASE)
    if m:
        stats["days_on_market"] = m.group(1)

    m = re.search(r'days\s+on\s+market[^%]*?(down|up)\s+([\d.]+)%', html, re.IGNORECASE)
    if m:
        direction = "-" if m.group(1).lower() == "down" else "+"
        stats["dom_change"] = f"{direction}{m.group(2)}%"

    m = re.search(r'active\s+listings[^0-9]*([\d,]+)\s+units', html, re.IGNORECASE)
    if not m:
        m = re.search(r'listings\s+rose[^0-9]*[\d.]+%[^0-9]*([\d,]+)\s+units', html, re.IGNORECASE)
    if m:
        stats["active_listings"] = m.group(1).replace(",", "")

    m = re.search(r'active\s+listings[^%]*?(down|up)\s+([\d.]+)%', html, re.IGNORECASE)
    if m:
        direction = "-" if m.group(1).lower() == "down" else "+"
        stats["active_listings_change"] = f"{direction}{m.group(2)}%"

    m = re.search(r'pending\s+sales.*?was\s+([\d,]+)\s+units', html, re.IGNORECASE)
    if not m:
        m = re.search(r'(?:new\s+)?pending\s+sales[^0-9]*([\d,]+)\s+units', html, re.IGNORECASE)
    if m:
        stats["pending_sales"] = m.group(1).replace(",", "")

    m = re.search(r'pending\s+sales[^%]*?(down|up)\s+([\d.]+)%', html, re.IGNORECASE)
    if m:
        direction = "-" if m.group(1).lower() == "down" else "+"
        stats["pending_sales_change"] = f"{direction}{m.group(2)}%"

    m = re.search(r'months\s+of\s+supply.*?was\s+([\d.]+\d)', html, re.IGNORECASE)
    if not m:
        m = re.search(r'months\s+of\s+supply.*?to\s+([\d.]+\d)', html, re.IGNORECASE)
    if m:
        stats["months_supply"] = m.group(1)

    m = re.search(r'Market\s+Statistics:?\s+(\w+)\s+(\d{4})', html, re.IGNORECASE)
    if m:
        stats["report_month"] = m.group(1)
        stats["report_year"] = m.group(2)

    return stats


# -- GHL API -------------------------------------------------------

def ghl_get_all_contacts():
    if not GHL_API_KEY or not GHL_LOCATION_ID:
        print("No GHL_API_KEY or GHL_LOCATION_ID set.")
        return []

    contacts = []
    url = "https://services.leadconnectorhq.com/contacts/"
    headers = {
        "Authorization": f"Bearer {GHL_API_KEY}",
        "Version": "2021-07-28",
    }
    params = {"locationId": GHL_LOCATION_ID, "limit": 100}

    page = 1
    while True:
        print(f"  Fetching contacts page {page}...")
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()

            batch = data.get("contacts", [])
            if not batch:
                break

            for c in batch:
                tags = c.get("tags", [])
                phone = c.get("phone", "")
                email = (c.get("email") or "").strip()
                first_name = (
                    c.get("firstName", "") or c.get("firstNameLowerCase", "") or ""
                ).strip().title()

                if phone or email:
                    contacts.append({
                        "id": c.get("id"),
                        "name": first_name,
                        "phone": phone,
                        "email": email,
                        "tags": [t.lower().strip() for t in tags] if tags else [],
                    })

            meta = data.get("meta", {})
            next_page = meta.get("nextPageUrl") or meta.get("nextPage")
            if next_page and batch:
                if isinstance(next_page, str) and next_page.startswith("http"):
                    url = next_page
                    params = {}
                else:
                    params["startAfterId"] = batch[-1].get("id", "")
                page += 1
            else:
                break

        except Exception as e:
            print(f"GHL API error: {e}")
            break

    print(f"  Total contacts: {len(contacts)}")
    return contacts


def ghl_send_sms(contact_id, message):
    url = "https://services.leadconnectorhq.com/conversations/messages"
    headers = {
        "Authorization": f"Bearer {GHL_API_KEY}",
        "Version": "2021-04-15",
        "Content-Type": "application/json",
    }
    payload = {"type": "SMS", "contactId": contact_id, "message": message}
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=30)
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"    SMS failed: {e}")
        return False


def ghl_send_email(contact_id, subject, body_html):
    url = "https://services.leadconnectorhq.com/conversations/messages"
    headers = {
        "Authorization": f"Bearer {GHL_API_KEY}",
        "Version": "2021-04-15",
        "Content-Type": "application/json",
    }
    payload = {
        "type": "Email",
        "contactId": contact_id,
        "subject": subject,
        "html": body_html,
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=30)
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"    Email failed: {e}")
        return False


# -- Tag Helpers ---------------------------------------------------

def get_audience_tag(tags):
    for tag in TAG_PRIORITY:
        if tag in tags:
            return tag
    return "general"

def get_delivery_preference(tags):
    if "email-only" in tags:
        return "email-only"
    if "text-only" in tags:
        return "text-only"
    return "both"

def is_quarterly_contact(tags):
    return "quarterly" in tags

def should_send_this_month(tags):
    if is_quarterly_contact(tags):
        return is_quarterly_month()
    return True


# -- Claude API Helper ---------------------------------------------

def call_claude(prompt, max_tokens=1000):
    """Generic Claude API call. Returns response text or None on failure."""
    if not ANTHROPIC_API_KEY:
        return None

    resp = None
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": CLAUDE_MODEL,
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        # Concatenate all text blocks (adaptive-thinking models may return multiple blocks)
        text_parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
        result = "".join(text_parts).strip().strip('"')
        if data.get("stop_reason") == "max_tokens":
            print(f"    WARNING: Claude output was TRUNCATED at {max_tokens} tokens. Using fallback instead.")
            return None
        return result if result else None
    except Exception as e:
        print(f"    Claude API error: {e}")
        if resp is not None:
            try:
                print(f"    Details: {resp.text[:500]}")
            except Exception:
                pass
        return None


# -- SMS Generation (AI) ------------------------------------------

def generate_message_for_tag(tag, stats, first_name="", contact_index=0):
    month = stats.get("report_month", "")
    year = stats.get("report_year", "")
    median = stats.get("median_price", "")
    median_chg = stats.get("median_price_change", "")
    inv_chg = stats.get("active_listings_change", "")
    dom = stats.get("days_on_market", "")
    dom_chg = stats.get("dom_change", "")
    pending_chg = stats.get("pending_sales_change", "")
    closed_chg = stats.get("closed_sales_change", "")
    supply = stats.get("months_supply", "")

    try:
        median_str = f"${int(median) // 1000}K"
    except (ValueError, TypeError):
        median_str = f"${median}"

    tag_prompt = TAG_PROMPTS.get(tag, DEFAULT_PROMPT)
    name = first_name if first_name else ""

    prompt = f"""You are Alex Marcia, a realtor in Northern Virginia. Write a monthly 
market update text to a client. 

ALEX'S VOICE — warm, genuine, uses natural punctuation, says "no worries" and "happy to", 
never salesy, always honest, gives counter-strategies not hype.

TEXT TO: {name}
THEIR SITUATION: {tag_prompt}

{month} {year} DATA:
- Median price: {median_str} ({median_chg} vs last year)
- Inventory: {inv_chg} vs last year
- Avg days on market: {dom} days ({dom_chg} vs last year)
- Pending sales: {pending_chg} vs last year
- Closed sales: {closed_chg} vs last year
- Months of supply: {supply}

FORMAT — CRITICAL, FOLLOW EXACTLY:
- Two short paragraphs separated by a blank line
- Paragraph 1: Greeting + 2-3 stats in one flowing sentence
- Paragraph 2: One honest insight + warm CTA
- TOTAL: 3-4 sentences. That's it. SHORT.

RULES:
- HONEST and UNBIASED — never "great time to buy/sell"
- Sellers: opportunity AND what to watch for
- Buyers: challenges AND where the upside is
- NEVER make value judgments on prices (affordable, luxury, solid price point, great deal)
- Only make data-backed statements. Never assume or speculate beyond what the numbers show.
- Never repeat openers, insights, or CTAs (message #{contact_index + 1} of 36+)
- Vary which stats you pick each time
- No hyphens or dashes. Use commas and periods.
- Mix Hey/hey randomly.
- Never offer to meet in person, grab coffee, or commit Alex to in-person time. CTAs should be text, call, or chat based only.

WRITE ONLY THE TEXT MESSAGE. Nothing else."""

    message = call_claude(prompt)
    if message:
        return message
    return generate_template_message(tag, stats, first_name)


def generate_template_message(tag, stats, first_name=""):
    month = stats.get("report_month", "This month")
    median = stats.get("median_price", "")
    inv_chg = stats.get("active_listings_change", "")
    dom = stats.get("days_on_market", "")

    try:
        median_str = f"${int(median) // 1000}K"
    except (ValueError, TypeError):
        median_str = f"${median}"

    name = first_name if first_name else ""

    if tag == "seller":
        return f"Hey {name}! Wanted to share the {month} numbers with you. Inventory is up {inv_chg} from last year, homes are averaging about {dom} days on market, and median is at {median_str}.\n\nBuyers are being more selective, so preparation and pricing matter more than ever. If you ever want to talk through what that looks like for your situation im here."
    elif tag == "buyer":
        return f"Hey {name}! {month} market data just came out. Median is at {median_str}, there's {inv_chg} more inventory than last year, and homes are taking about {dom} days to sell now.\n\nThe pace is much more manageable than before. Happy to talk through what this means for your search whenever you have a chance."
    else:
        return f"Hey {name}! Quick {month} update for you. Median sitting at {median_str}, inventory up {inv_chg} from last year, and homes averaging {dom} days on market.\n\nThe market is finding more of a balance which is healthy for everyone. If any of this sparks questions don't hesitate to reach out."


# -- Email Generation (sent to Alex for review) --------------------

def generate_email_insight(tag, stats):
    """Use Claude to generate the 'What this means' and 'My advice' sections."""
    month = stats.get("report_month", "")
    year = stats.get("report_year", "")
    median = stats.get("median_price", "")
    median_chg = stats.get("median_price_change", "")
    inv = stats.get("active_listings", "")
    inv_chg = stats.get("active_listings_change", "")
    dom = stats.get("days_on_market", "")
    dom_chg = stats.get("dom_change", "")
    pending = stats.get("pending_sales", "")
    pending_chg = stats.get("pending_sales_change", "")
    closed = stats.get("closed_sales", "")
    closed_chg = stats.get("closed_sales_change", "")
    supply = stats.get("months_supply", "")

    try:
        median_fmt = f"${int(median):,}"
    except (ValueError, TypeError):
        median_fmt = f"${median}"

    if tag == "seller":
        audience_context = (
            "This email is going to SELLERS — homeowners who may be thinking about selling. "
            "Frame the insight around what these numbers mean for their home's value and market position. "
            "The advice should be actionable and specific to sellers."
        )
    elif tag == "buyer":
        audience_context = (
            "This email is going to BUYERS — people actively looking to buy a home. "
            "Frame the insight around what these numbers mean for their search, negotiating power, and opportunity. "
            "The advice should be actionable and specific to buyers."
        )
    else:
        audience_context = (
            "This email is going to GENERAL contacts who could be buyers OR sellers. "
            "Write TWO sections: one paragraph for people thinking about selling and one for people thinking about buying. "
            "Use headers 'If you're thinking about selling:' and 'If you're thinking about buying:' for each. "
            "Each section should have its own insight and advice based on the data."
        )

    prompt = f"""You are Alex Marcia, a realtor in Northern Virginia. Write the insight and advice 
sections for a monthly market update email.

{audience_context}

{month} {year} DATA:
- Median sold price: {median_fmt} ({median_chg} vs last year)
- Active listings: {inv} ({inv_chg} vs last year)
- Days on market: {dom} ({dom_chg} vs last year)
- Pending sales: {pending} ({pending_chg} vs last year)
- Closed sales: {closed} ({closed_chg} vs last year)
- Months of supply: {supply}

WRITE TWO SECTIONS:

1. "What this means:" — 2-3 sentences of honest, data-backed analysis. Reference specific numbers. 
   Explain what the data actually tells us, not generic filler. Every statement should connect to a number above.

2. "My advice:" — 2-3 sentences of practical, specific guidance based on what the numbers show.
   Not generic "be prepared" fluff. Actual strategy tied to the current data.

RULES:
- ONLY make statements backed by the numbers above. Never speculate or assume.
- NEVER say "great time to buy/sell" or make value judgments.
- Be honest about both sides — opportunity AND risk.
- Warm, professional tone. Not salesy.
- No hyphens or dashes. Use commas and periods.
- Never offer to meet in person, grab coffee, or commit Alex to in-person time. Any call to action should be text, call, or chat based only.

WRITE ONLY the two sections. Start with "What this means:" and then "My advice:". Nothing else."""

    result = call_claude(prompt, max_tokens=3000)
    if result:
        return result

    # Fallback if Claude fails
    if tag == "seller":
        return (
            f"What this means: Supply is at {supply} months. Buyers have more to choose "
            f"from now which means they're being more selective. The homes that are "
            f"moving are the ones priced right and prepped well.\n\n"
            f"My advice: Preparation matters more than ever right now. A solid pre-listing "
            f"strategy around pricing, staging, and timing can make a real difference."
        )
    elif tag == "buyer":
        return (
            f"What this means: There's {inv_chg} more inventory than last year and "
            f"homes are sitting longer, which gives you more time to be thoughtful. "
            f"That said, supply is still only {supply} months so it's still competitive.\n\n"
            f"My advice: With more options available, being pre-approved and ready to "
            f"move quickly on the right home is your biggest advantage."
        )
    else:
        return (
            f"If you're thinking about selling: Supply is at {supply} months. Buyers have "
            f"more to choose from now which means they're being more selective. The homes "
            f"moving are the ones priced right and prepped well. A solid pre-listing strategy "
            f"around pricing, staging, and timing can make a real difference.\n\n"
            f"If you're thinking about buying: There's {inv_chg} more inventory than last year "
            f"and homes are sitting longer, which gives you more time to be thoughtful. Being "
            f"pre-approved and ready to move quickly on the right home is your biggest advantage."
        )


def generate_email_content(tag, stats):
    """Generate formatted email content for Alex to review and send manually."""
    month = stats.get("report_month", "")
    year = stats.get("report_year", "")
    median = stats.get("median_price", "")
    median_chg = stats.get("median_price_change", "")
    inv = stats.get("active_listings", "")
    inv_chg = stats.get("active_listings_change", "")
    dom = stats.get("days_on_market", "")
    dom_chg = stats.get("dom_change", "")
    pending = stats.get("pending_sales", "")
    pending_chg = stats.get("pending_sales_change", "")
    closed = stats.get("closed_sales", "")
    closed_chg = stats.get("closed_sales_change", "")
    supply = stats.get("months_supply", "")

    try:
        median_fmt = f"${int(median):,}"
    except (ValueError, TypeError):
        median_fmt = f"${median}"
    try:
        inv_fmt = f"{int(inv):,}"
    except (ValueError, TypeError):
        inv_fmt = inv
    try:
        pending_fmt = f"{int(pending):,}"
    except (ValueError, TypeError):
        pending_fmt = pending
    try:
        closed_fmt = f"{int(closed):,}"
    except (ValueError, TypeError):
        closed_fmt = closed

    bullet_lines = []
    if median_fmt and median_chg:
        bullet_lines.append(f"Median sold price: {median_fmt} ({median_chg} from last year)")
    if inv_fmt and inv_chg:
        bullet_lines.append(f"Active listings: {inv_fmt} ({inv_chg} from last year)")
    elif inv_chg:
        bullet_lines.append(f"Active listings: {inv_chg} from last year")
    if dom and dom_chg:
        bullet_lines.append(f"Days on market: {dom} ({dom_chg} from last year)")
    if pending_fmt and pending_chg:
        bullet_lines.append(f"Pending sales: {pending_fmt} ({pending_chg} from last year)")
    if closed_fmt and closed_chg:
        bullet_lines.append(f"Closed sales: {closed_fmt} ({closed_chg} from last year)")
    if supply:
        bullet_lines.append(f"Months of supply: {supply}")

    bullets = "\n".join(f"  * {b}" for b in bullet_lines)

    # Get Claude-generated insight and advice
    insight_and_advice = generate_email_insight(tag, stats)

    return f"""{month} {year} MARKET UPDATE — COPY/PASTE INTO GHL TEMPLATE
{'=' * 60}

AUDIENCE: {tag.upper()}

STATS:
{bullets}

{insight_and_advice}

No rush on anything, just like keeping you informed. Always here if you want to talk through it.
{'=' * 60}"""


# -- Main ----------------------------------------------------------

def main():
    print(f"Checking NVAR for new market stats... ({datetime.now().isoformat()})")
    print(f"Quarterly month: {'YES' if is_quarterly_month() else 'NO'}")

    # Check test mode
    test_mode = os.environ.get("TEST_MODE", "false").lower() == "true"
    if test_mode:
        print("*** TEST MODE: Only sending to admin/owner contacts ***")

    try:
        html = fetch_page(NVAR_NEWS_URL)
    except Exception as e:
        print(f"Failed to fetch NVAR news page: {e}")
        return

    post_url = find_latest_market_stats_post(html)
    if not post_url:
        print("No market statistics post found. Will check again tomorrow.")
        return

    print(f"Found post: {post_url}")

    last_sent = get_last_sent()
    if post_url == last_sent:
        print("Already sent this post. Nothing new to do.")
        return

    print("New post detected! Scraping stats...")

    try:
        article_html = fetch_page(post_url)
    except Exception as e:
        print(f"Could not fetch article: {e}")
        return

    stats = extract_stats_from_article(article_html)
    if not stats.get("median_price"):
        print("Could not extract key stats. Page format may have changed.")
        print(f"Extracted: {json.dumps(stats, indent=2)}")
        return

    print(f"Stats: {json.dumps(stats, indent=2)}")

    print("\nFetching contacts from GHL...")
    contacts = ghl_get_all_contacts()
    if not contacts:
        print("No contacts found. Saving post URL.")
        save_last_sent(post_url)
        return

    # Find Alex's contact for notifications
    alex_contact = None
    for c in contacts:
        if "admin" in c["tags"] or "owner" in c["tags"]:
            alex_contact = c
            break

    print(f"\nProcessing {len(contacts)} contacts...")
    total_sms = 0
    total_skipped = 0
    total_failed = 0

    for i, contact in enumerate(contacts):
        name = contact["name"]
        tags = contact["tags"]
        has_phone = bool(contact.get("phone"))

        audience = get_audience_tag(tags)
        delivery = get_delivery_preference(tags)

        # TEST MODE: only send to admin/owner contacts
        if test_mode and "admin" not in tags and "owner" not in tags:
            print(f"  {name} — skipped (test mode)")
            total_skipped += 1
            continue

        if not should_send_this_month(tags):
            print(f"  {name} — quarterly, skipping this month")
            total_skipped += 1
            continue

        send_sms = has_phone and delivery in ("both", "text-only")

        if not send_sms:
            print(f"  {name} — no valid SMS channel")
            total_skipped += 1
            continue

        print(f"\n  -> {name} [{audience}] (delivery: {delivery})")

        message = generate_message_for_tag(audience, stats, first_name=name, contact_index=i)
        print(f"    SMS: {message[:80]}...")

        if GHL_API_KEY:
            if ghl_send_sms(contact["id"], message):
                total_sms += 1
            else:
                total_failed += 1
            time.sleep(0.5)
        else:
            print(f"    [DRY RUN]")
            total_sms += 1

    # Send notification to Alex
    if alex_contact and GHL_API_KEY:
        month = stats.get("report_month", "New")
        notify_msg = f"New NVAR stats are out for {month}! Texts have been sent to your contacts. Check your email for the formatted content to paste into your GHL template."
        ghl_send_sms(alex_contact["id"], notify_msg)
        print(f"\n  Notification sent to Alex.")

        # Send formatted email content to Alex for each audience type
        for audience in ["seller", "buyer", "general"]:
            content = generate_email_content(audience, stats)
            subject = f"{month} Market Update — {audience.upper()} version (review before sending)"
            body_html = content.replace("\n", "<br>")
            ghl_send_email(alex_contact["id"], subject, body_html)
            print(f"  Email content ({audience}) sent to Alex for review.")

    save_last_sent(post_url)
    print(f"\nDone! SMS: {total_sms} | Skipped: {total_skipped} | Failed: {total_failed}")


if __name__ == "__main__":
    main()
