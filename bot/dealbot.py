"""
PedalDeals bot — finds cycling deals from legal data sources.

Data sources (all legal):
  1. Affiliate network product feeds (Awin, TradeTracker) — shops provide these
  2. RSS/Atom feeds from shop sale pages
  3. Manual deals you add yourself

Run:  python dealbot.py
Does:  reads sources -> filters deals -> tracks price history -> writes js/deals.js
"""

import csv
import io
import json
import os
import sys
import zipfile
from datetime import date, datetime

# optional deps — graceful fallback
try:
    import requests
except ImportError:
    requests = None
    print("warning: 'requests' not installed, can only use manual source")

try:
    import feedparser
except ImportError:
    feedparser = None
    print("warning: 'feedparser' not installed, RSS feeds disabled")

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None
    print("warning: 'beautifulsoup4' not installed, scrapers disabled")


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def load_config():
    path = os.path.join(SCRIPT_DIR, "config.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_price_history(config):
    path = os.path.join(SCRIPT_DIR, config["price_history_file"])
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_price_history(config, history):
    path = os.path.join(SCRIPT_DIR, config["price_history_file"])
    with open(path, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2, ensure_ascii=False)


def guess_category(title, config):
    title_lower = title.lower()
    for cat, keywords in config["category_keywords"].items():
        for kw in keywords:
            if kw in title_lower:
                return cat
    return "accessories"


def make_deal_key(title, store):
    return (title.strip().lower() + "|" + store.strip().lower())


# ---------------------------------------------------------------------------
# Source: Manual deals (always works)
# ---------------------------------------------------------------------------
def fetch_manual(source_config):
    path = os.path.join(SCRIPT_DIR, source_config["file"])
    if not os.path.exists(path):
        print(f"  manual file not found: {path}")
        return []
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    deals = []
    for item in raw:
        deals.append({
            "title": item["title"],
            "category": item.get("category", ""),
            "price_now": float(item["price_now"]),
            "price_was": float(item["price_was"]),
            "store": item["store"],
            "url": item.get("url", "#"),
            "img": item.get("img", ""),
            "pick": item.get("pick", False),
        })
    return deals


# ---------------------------------------------------------------------------
# Source: Awin product feeds (CSV, possibly zipped)
# ---------------------------------------------------------------------------
def fetch_awin_feed(source_config, config):
    if not requests:
        print("  skipping awin feed (requests not installed)")
        return []
    url = source_config["url"]
    print(f"  downloading awin feed: {source_config['name']}")
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()

    # awin feeds can be zipped
    if url.endswith("/") or "compression/zip" in url:
        try:
            zf = zipfile.ZipFile(io.BytesIO(resp.content))
            csv_name = zf.namelist()[0]
            csv_text = zf.read(csv_name).decode("utf-8")
        except zipfile.BadZipFile:
            csv_text = resp.text
    else:
        csv_text = resp.text

    reader = csv.DictReader(io.StringIO(csv_text))
    deals = []
    for row in reader:
        try:
            sale_price = float(row.get("search_price", 0))
            rrp = float(row.get("rrp_price", 0))
        except (ValueError, TypeError):
            continue

        if rrp <= 0 or sale_price <= 0 or sale_price >= rrp:
            continue

        title = row.get("product_name", "").strip()
        if not title:
            continue

        category = guess_category(title, config)
        store = row.get("merchant_name", source_config["name"]).strip()
        link = row.get("aw_deep_link", "#")

        deals.append({
            "title": title,
            "category": category,
            "price_now": sale_price,
            "price_was": rrp,
            "store": store,
            "url": link,
            "pick": False,
        })

    return deals


# ---------------------------------------------------------------------------
# Source: RSS/Atom feeds
# ---------------------------------------------------------------------------
def fetch_rss_feed(source_config, config):
    if not feedparser or not requests:
        print("  skipping RSS (missing deps)")
        return []
    url = source_config["url"]
    if not url:
        return []
    print(f"  fetching RSS: {source_config['name']}")
    feed = feedparser.parse(url)
    deals = []
    for entry in feed.entries:
        title = entry.get("title", "").strip()
        link = entry.get("link", "#")
        if not title:
            continue
        # RSS feeds rarely include structured pricing — these would need
        # the price extracted from the description or a follow-up request.
        # For now we just log what's found. You'd customize this per feed.
        print(f"    found: {title}")
        deals.append({
            "title": title,
            "category": guess_category(title, config),
            "price_now": 0,
            "price_was": 0,
            "store": source_config["name"],
            "url": link,
            "pick": False,
        })
    return deals


# ---------------------------------------------------------------------------
# Source: Bike-Discount scraper (Shopware 6 AJAX widget)
# ---------------------------------------------------------------------------
def fetch_bike_discount(source_config, config):
    if not requests or not BeautifulSoup:
        print("  skipping bike-discount (missing deps)")
        return []

    base = "https://www.bike-discount.de/en/widgets/cms/navigation"
    nav_id = source_config.get("nav_id", "018c7ec55ee371dabd7b73d1d4e9003b")
    url = f"{base}/{nav_id}"
    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.bike-discount.de/en/bike/sale/close-outs",
    }
    max_pages = source_config.get("max_pages", 5)
    deals = []

    for page in range(1, max_pages + 1):
        print(f"  page {page}...")
        params = {"p": page, "limit": 24, "order": "topseller",
                  "no-aggregations": 1}
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=30)
            resp.raise_for_status()
        except Exception as e:
            print(f"  error on page {page}: {e}")
            break

        soup = BeautifulSoup(resp.text, "html.parser")
        cards = soup.select(".product-box")
        if not cards:
            break

        for card in cards:
            title_el = card.select_one(".product-name")
            if not title_el:
                continue
            title = title_el.get_text(strip=True)

            link_el = card.select_one("a.product-image-link") or card.select_one("a")
            link = link_el["href"] if link_el and link_el.get("href") else "#"
            if link.startswith("/"):
                link = "https://www.bike-discount.de" + link

            price_now_el = card.select_one(".product-price")
            list_price_el = card.select_one(".list-price-price")

            if not price_now_el or not list_price_el:
                continue

            try:
                price_now = float(
                    price_now_el.get_text(strip=True)
                    .replace("EUR", "").replace("€", "")
                    .replace(".", "").replace(",", ".").strip()
                )
                price_was = float(
                    list_price_el.get_text(strip=True)
                    .replace("EUR", "").replace("€", "")
                    .replace(".", "").replace(",", ".").strip()
                )
            except (ValueError, AttributeError):
                continue

            if price_was <= 0 or price_now <= 0 or price_now >= price_was:
                continue

            deals.append({
                "title": title,
                "category": guess_category(title, config),
                "price_now": price_now,
                "price_was": price_was,
                "store": "Bike-Discount",
                "url": link,
                "pick": False,
            })

    return deals


# ---------------------------------------------------------------------------
# Source: Mantel scraper
# ---------------------------------------------------------------------------
def fetch_mantel(source_config, config):
    if not requests or not BeautifulSoup:
        print("  skipping mantel (missing deps)")
        return []

    base_url = source_config.get("url", "https://www.mantel.com/en/sale")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    max_pages = source_config.get("max_pages", 5)
    deals = []

    for page in range(1, max_pages + 1):
        print(f"  page {page}...")
        page_url = f"{base_url}?p={page}"
        try:
            resp = requests.get(page_url, headers=headers, timeout=30)
            resp.raise_for_status()
        except Exception as e:
            print(f"  error on page {page}: {e}")
            break

        soup = BeautifulSoup(resp.text, "html.parser")
        cards = soup.select("ma-product-card, .product-card")
        if not cards:
            break

        for card in cards:
            title_el = (card.select_one(".js-product-card-title-url")
                        or card.select_one(".product-card-title a"))
            if not title_el:
                continue
            title = " ".join(title_el.get_text().split())

            link = title_el.get("href", "#")
            if link.startswith("//"):
                link = "https:" + link
            elif link.startswith("/"):
                link = "https://www.mantel.com" + link

            img_el = card.select_one("img")
            img = ""
            if img_el:
                img = img_el.get("src", "")
                if img.startswith("//"):
                    img = "https:" + img

            rrp_el = card.select_one(".product-card-price-recommended span")
            now_el = card.select_one(".product-card-price-current")

            if not rrp_el or not now_el:
                continue

            try:
                price_text = now_el.get_text(strip=True)
                # handle "From 54,95" format
                price_text = price_text.lower().replace("from", "").strip()
                price_now = float(
                    price_text.replace("€", "").replace(".", "").replace(",", ".").strip()
                )
                price_was = float(
                    rrp_el.get_text(strip=True)
                    .replace("€", "").replace(".", "").replace(",", ".").strip()
                )
            except (ValueError, AttributeError):
                continue

            if price_was <= 0 or price_now <= 0 or price_now >= price_was:
                continue

            deals.append({
                "title": title,
                "category": guess_category(title, config),
                "price_now": price_now,
                "price_was": price_was,
                "store": "Mantel",
                "url": link,
                "img": img,
                "pick": False,
            })

    return deals


# ---------------------------------------------------------------------------
# Source: Futurumshop scraper
# ---------------------------------------------------------------------------
def fetch_futurumshop(source_config, config):
    if not requests or not BeautifulSoup:
        print("  skipping futurumshop (missing deps)")
        return []

    base_url = source_config.get("url", "https://www.futurumshop.nl/sale/")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    }
    max_pages = source_config.get("max_pages", 5)
    deals = []

    for page in range(1, max_pages + 1):
        print(f"  page {page}...")
        page_url = f"{base_url}?page={page}" if page > 1 else base_url
        try:
            resp = requests.get(page_url, headers=headers, timeout=30)
            resp.raise_for_status()
        except Exception as e:
            print(f"  error on page {page}: {e}")
            break

        soup = BeautifulSoup(resp.text, "html.parser")
        cards = soup.select(".productContent")
        if not cards:
            break

        for card in cards:
            title_el = card.select_one(".productFullName")
            brand_el = card.select_one(".productTitle")
            if not title_el:
                continue

            brand = ""
            if brand_el:
                brand = brand_el.get_text(strip=True).replace(
                    title_el.get_text(strip=True), ""
                ).strip()
            full_name = title_el.get_text(strip=True)
            title = f"{brand} {full_name}".strip() if brand else full_name

            link_el = card.select_one("a[href]")
            link = link_el["href"] if link_el else "#"
            if link.startswith("//"):
                link = "https:" + link
            elif link.startswith("/"):
                link = "https://www.futurumshop.nl" + link

            # image is in a sibling element before productContent
            img = ""
            parent = card.find_parent()
            if parent:
                img_el = parent.select_one("img")
                if img_el:
                    img = img_el.get("src", "")

            former_el = card.select_one(".js_former-price")
            current_el = card.select_one(".js_current-price")
            if not former_el or not current_el:
                continue

            try:
                was_text = former_el.get_text(strip=True).replace(",", ".").strip()
                now_text = current_el.get_text(strip=True).replace(",", ".").strip()
                price_was = float(was_text)
                price_now = float(now_text)
            except (ValueError, AttributeError):
                continue

            if price_was <= 0 or price_now <= 0 or price_now >= price_was:
                continue

            deals.append({
                "title": title,
                "category": guess_category(title, config),
                "price_now": price_now,
                "price_was": price_was,
                "store": "Futurumshop",
                "url": link,
                "img": img,
                "pick": False,
            })

    return deals


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------
def fetch_all_deals(config):
    all_deals = []

    # Manual
    for src in config["feeds"].get("manual_sources", []):
        if src.get("enabled"):
            print(f"[manual] {src['name']}")
            all_deals.extend(fetch_manual(src))

    # Awin
    for src in config["feeds"].get("awin_feeds", []):
        if src.get("enabled"):
            print(f"[awin] {src['name']}")
            try:
                all_deals.extend(fetch_awin_feed(src, config))
            except Exception as e:
                print(f"  error: {e}")

    # RSS
    for src in config["feeds"].get("rss_feeds", []):
        if src.get("enabled"):
            print(f"[rss] {src['name']}")
            try:
                all_deals.extend(fetch_rss_feed(src, config))
            except Exception as e:
                print(f"  error: {e}")

    # Scrapers
    scrapers = {
        "bike-discount": fetch_bike_discount,
        "mantel": fetch_mantel,
        "futurumshop": fetch_futurumshop,
    }
    for src in config["feeds"].get("scrapers", []):
        if src.get("enabled"):
            scraper_type = src.get("type", "")
            fn = scrapers.get(scraper_type)
            if fn:
                print(f"[scraper] {src['name']}")
                try:
                    all_deals.extend(fn(src, config))
                except Exception as e:
                    print(f"  error: {e}")
            else:
                print(f"  unknown scraper type: {scraper_type}")

    return all_deals


def filter_deals(deals, config):
    min_pct = config["min_discount_percent"]
    seen = set()
    filtered = []
    for d in deals:
        if d["price_was"] <= 0 or d["price_now"] <= 0:
            continue
        discount = (1 - d["price_now"] / d["price_was"]) * 100
        if discount < min_pct:
            continue
        # deduplicate by title + store
        key = d["title"].strip().lower() + "|" + d["store"].strip().lower()
        if key in seen:
            continue
        seen.add(key)
        filtered.append(d)
    # sort by discount descending
    filtered.sort(key=lambda d: (1 - d["price_now"] / d["price_was"]), reverse=True)
    return filtered[:config["max_deals"]]


def update_price_history(deals, history):
    today = date.today().isoformat()
    for d in deals:
        key = make_deal_key(d["title"], d["store"])
        if key not in history:
            history[key] = {"first_seen": today, "prices": []}
        history[key]["prices"].append({
            "date": today,
            "price": d["price_now"]
        })
        # keep last 90 entries max
        history[key]["prices"] = history[key]["prices"][-90:]
    return history


def write_deals_js(deals, config):
    today = date.today().isoformat()
    pick_titles = [p.lower() for p in config.get("picks", [])]

    entries = []
    for i, d in enumerate(deals):
        is_pick = d.get("pick", False)
        if not is_pick and d["title"].lower() in pick_titles:
            is_pick = True

        entry = {
            "id": i + 1,
            "title": d["title"],
            "category": d["category"],
            "priceNow": round(d["price_now"]),
            "priceWas": round(d["price_was"]),
            "store": d["store"],
            "storeUrl": d["url"],
            "added": today,
        }
        if d.get("img"):
            entry["img"] = d["img"]
        if is_pick:
            entry["pick"] = True
        entries.append(entry)

    js_content = "var DEALS = " + json.dumps(entries, indent=2, ensure_ascii=False) + ";\n"

    out_path = os.path.join(SCRIPT_DIR, config["output_file"])
    out_path = os.path.normpath(out_path)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(js_content)

    print(f"\nwrote {len(entries)} deals to {out_path}")


def main():
    print("=== pedaldeals bot ===\n")
    config = load_config()
    history = load_price_history(config)

    # 1. Fetch from all sources
    raw_deals = fetch_all_deals(config)
    print(f"\nfound {len(raw_deals)} raw deals")

    # 2. Filter by minimum discount
    filtered = filter_deals(raw_deals, config)
    print(f"after filtering: {len(filtered)} deals (min {config['min_discount_percent']}% off)")

    # 3. Update price history
    history = update_price_history(filtered, history)
    save_price_history(config, history)

    # 4. Write deals.js
    write_deals_js(filtered, config)

    print("\ndone.")


if __name__ == "__main__":
    main()
