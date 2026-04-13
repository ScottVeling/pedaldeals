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


import re

def guess_category(title, config):
    title_lower = title.lower()
    # check categories in priority order: clothing first (most specific),
    # then tools, parts, accessories, bikes last (too many false positives)
    priority = ["clothing", "tools", "parts", "accessories", "bikes"]
    for cat in priority:
        keywords = config["category_keywords"].get(cat, [])
        for kw in keywords:
            # use word boundary matching to avoid substring false positives
            if re.search(r'\b' + re.escape(kw) + r'\b', title_lower):
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
# Source: Rose Bikes scraper
# ---------------------------------------------------------------------------
def fetch_rose_bikes(source_config, config):
    if not requests or not BeautifulSoup:
        print("  skipping rose bikes (missing deps)")
        return []

    base_url = source_config.get("url", "https://www.rosebikes.com/sale")
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
        cards = soup.select(".catalog-product-tile")
        if not cards:
            break

        for card in cards:
            title_el = card.select_one(".catalog-product-tile__title")
            if not title_el:
                continue
            title = " ".join(title_el.get_text().split())

            link_el = card.select_one(".catalog-product-tile__link")
            link = link_el["href"] if link_el and link_el.get("href") else "#"
            if link.startswith("/"):
                link = "https://www.rosebikes.com" + link

            img_el = card.select_one("img")
            img = img_el.get("src", "") if img_el else ""

            old_el = card.select_one(".product-tile-price__old-value")
            cur_el = card.select_one(".product-tile-price__current-value")
            if not old_el or not cur_el:
                continue

            try:
                price_was = float(
                    old_el.get_text(strip=True)
                    .replace("€", "").replace(",", "").strip()
                )
                price_now = float(
                    cur_el.get_text(strip=True)
                    .replace("€", "").replace(",", "").strip()
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
                "store": "Rose Bikes",
                "url": link,
                "img": img,
                "pick": False,
            })

    return deals


# ---------------------------------------------------------------------------
# Source: Bikester.nl scraper (Shopify)
# ---------------------------------------------------------------------------
def parse_bikester_price(el):
    """Parse Bikester price like '€2.59900' or '€15999' into float.
    The last 2 digits are cents, dots are thousands separators."""
    if not el:
        return 0
    text = el.get_text(strip=True).replace("€", "").replace(" ", "")
    # remove thousands separator dots
    text = text.replace(".", "")
    if not text:
        return 0
    try:
        # last 2 digits are cents
        val = int(text)
        return val / 100.0
    except ValueError:
        return 0


def fetch_bikester(source_config, config):
    if not requests or not BeautifulSoup:
        print("  skipping bikester (missing deps)")
        return []

    base_url = source_config.get("url", "https://www.bikester.nl/sale/")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    }
    max_pages = source_config.get("max_pages", 5)
    deals = []

    for page in range(1, max_pages + 1):
        print(f"  page {page}...")
        page_url = f"{base_url}?page={page}" if page > 1 else base_url
        try:
            resp = requests.get(page_url, headers=headers, timeout=30,
                                allow_redirects=True)
            resp.raise_for_status()
        except Exception as e:
            print(f"  error on page {page}: {e}")
            break

        soup = BeautifulSoup(resp.text, "html.parser")
        cards = soup.select("product-card")
        if not cards:
            break

        for card in cards:
            link_el = card.select_one("a.js-prod-link")
            if not link_el:
                continue
            title = link_el.get("aria-label", "").strip()
            if not title:
                continue

            link = link_el.get("href", "#")
            if link.startswith("/"):
                link = "https://www.bikester.nl" + link

            img_el = card.select_one("img")
            img = ""
            if img_el:
                img = img_el.get("src", "")
                if img.startswith("//"):
                    img = "https:" + img

            cur_el = card.select_one(".price__current .js-value")
            was_el = card.select_one(".price__was .js-value")

            price_now = parse_bikester_price(cur_el)
            price_was = parse_bikester_price(was_el)

            if price_was <= 0 or price_now <= 0 or price_now >= price_was:
                continue

            deals.append({
                "title": title,
                "category": guess_category(title, config),
                "price_now": price_now,
                "price_was": price_was,
                "store": "Bikester",
                "url": link,
                "img": img,
                "pick": False,
            })

    return deals


# ---------------------------------------------------------------------------
# Source: Lordgun scraper (GBP, calculates original from discount %)
# ---------------------------------------------------------------------------
def fetch_lordgun(source_config, config):
    if not requests or not BeautifulSoup:
        print("  skipping lordgun (missing deps)")
        return []

    base_url = source_config.get("url", "https://www.lordgunbicycles.co.uk/offers")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    }
    max_pages = source_config.get("max_pages", 3)
    gbp_to_eur = source_config.get("gbp_to_eur", 1.16)
    deals = []

    for page in range(1, max_pages + 1):
        print(f"  page {page}...")
        page_url = f"{base_url}?page={page}" if page > 1 else base_url
        try:
            resp = requests.get(page_url, headers=headers, timeout=30,
                                allow_redirects=True)
            resp.raise_for_status()
        except Exception as e:
            print(f"  error on page {page}: {e}")
            break

        soup = BeautifulSoup(resp.text, "html.parser")
        cards = soup.select("article.product")
        if not cards:
            break

        for card in cards:
            brand_el = card.select_one(".product__brand")
            title_el = card.select_one(".article__title")
            if not title_el:
                continue
            brand = brand_el.get_text(strip=True) if brand_el else ""
            name = title_el.get_text(strip=True)
            title = f"{brand} {name}".strip() if brand else name

            link_el = card.select_one("a.article__link")
            link = link_el["href"] if link_el and link_el.get("href") else "#"
            if link.startswith("/"):
                link = "https://www.lordgunbicycles.co.uk" + link

            img_el = card.select_one("img.article__image")
            img = ""
            if img_el:
                img = img_el.get("data-src", "") or img_el.get("src", "")

            price_el = card.select_one(".product__price")
            disc_el = card.select_one(".product__price--discount")
            if not price_el or not disc_el:
                continue

            try:
                price_text = (price_el.get_text(strip=True)
                              .lower().replace("from", "").replace("£", "")
                              .replace(",", "").strip())
                price_gbp = float(price_text)
                disc_text = disc_el.get_text(strip=True).replace("%", "").replace("-", "").strip()
                disc_pct = float(disc_text)
            except (ValueError, AttributeError):
                continue

            if disc_pct <= 0 or price_gbp <= 0:
                continue

            price_now = round(price_gbp * gbp_to_eur, 2)
            price_was = round(price_now / (1 - disc_pct / 100), 2)

            deals.append({
                "title": title,
                "category": guess_category(title, config),
                "price_now": price_now,
                "price_was": price_was,
                "store": "Lordgun",
                "url": link,
                "img": img,
                "pick": False,
            })

    return deals


# ---------------------------------------------------------------------------
# Source: Canyon Outlet scraper
# ---------------------------------------------------------------------------
def fetch_canyon(source_config, config):
    if not requests or not BeautifulSoup:
        print("  skipping canyon (missing deps)")
        return []

    urls = source_config.get("urls", [
        "https://www.canyon.com/en-nl/outlet-bikes/road-bikes/",
        "https://www.canyon.com/en-nl/outlet-bikes/gravel-bikes/",
        "https://www.canyon.com/en-nl/outlet-bikes/mountain-bikes/",
    ])
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    }
    deals = []

    for url in urls:
        cat_name = url.rstrip("/").split("/")[-1]
        print(f"  {cat_name}...")
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
        except Exception as e:
            print(f"  error: {e}")
            continue

        soup = BeautifulSoup(resp.text, "html.parser")
        cards = soup.select(".productTileDefault--bike")

        for card in cards:
            name_el = card.select_one("a.productTileDefault__productName")
            if not name_el:
                continue
            title = name_el.get("title", "").strip() or name_el.get_text(strip=True)

            link = name_el.get("href", "#")
            if link.startswith("/"):
                link = "https://www.canyon.com" + link

            sale_el = card.select_one(".productTile__priceSale")
            orig_el = card.select_one(".productTile__priceOriginal")
            if not sale_el or not orig_el:
                continue

            img_el = card.select_one("img.productTileDefault__image")
            img = ""
            if img_el:
                srcset = img_el.get("srcset", "")
                if srcset:
                    img = srcset.split(",")[0].strip().split(" ")[0]
                else:
                    img = img_el.get("src", "")

            try:
                sale_text = sale_el.get_text(strip=True).replace("€", "").replace(".", "").replace(",", ".").strip()
                orig_text = orig_el.get_text(strip=True).replace("€", "").replace(".", "").replace(",", ".").strip()
                price_now = float(sale_text)
                price_was = float(orig_text)
            except (ValueError, AttributeError):
                continue

            if price_was <= 0 or price_now <= 0 or price_now >= price_was:
                continue

            deals.append({
                "title": "Canyon " + title,
                "category": guess_category(title, config),
                "price_now": price_now,
                "price_was": price_was,
                "store": "Canyon",
                "url": link,
                "img": img,
                "pick": False,
            })

    return deals


# ---------------------------------------------------------------------------
# Source: 12GOBiking.nl scraper (GraphQL API)
# ---------------------------------------------------------------------------
def fetch_12gobiking(source_config, config):
    if not requests:
        print("  skipping 12gobiking (requests not installed)")
        return []

    categories = source_config.get("categories", {
        "2896": "road bikes",
        "3406": "gravel bikes",
        "2897": "mountain bikes",
    })
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Content-Type": "application/json",
    }
    page_size = source_config.get("page_size", 20)
    deals = []

    for cat_id, cat_name in categories.items():
        print(f"  {cat_name}...")
        query = """{
          products(filter: {category_id: {eq: "%s"}}, pageSize: %d, sort: {price: ASC}) {
            items {
              name
              url_key
              price_range {
                minimum_price {
                  regular_price { value }
                  final_price { value }
                }
              }
              image { url }
            }
          }
        }""" % (cat_id, page_size)

        try:
            resp = requests.post(
                "https://www.12gobiking.nl/graphql",
                json={"query": query},
                headers=headers,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"  error: {e}")
            continue

        items = data.get("data", {}).get("products", {}).get("items", [])
        for item in items:
            title = item.get("name", "").strip()
            if not title:
                continue

            url_key = item.get("url_key", "")
            link = f"https://www.12gobiking.nl/{url_key}" if url_key else "#"

            img = item.get("image", {}).get("url", "")

            prices = item.get("price_range", {}).get("minimum_price", {})
            price_was = prices.get("regular_price", {}).get("value", 0)
            price_now = prices.get("final_price", {}).get("value", 0)

            if price_was <= 0 or price_now <= 0 or price_now >= price_was:
                continue

            deals.append({
                "title": title,
                "category": guess_category(title, config),
                "price_now": price_now,
                "price_was": price_was,
                "store": "12GOBiking",
                "url": link,
                "img": img,
                "pick": False,
            })

    return deals


# ---------------------------------------------------------------------------
# Source: Planet X scraper (Shopify JSON API, GBP)
# ---------------------------------------------------------------------------
def fetch_planetx(source_config, config):
    if not requests:
        print("  skipping planetx (requests not installed)")
        return []

    urls = source_config.get("urls", [
        "https://www.planetx.co.uk/collections/bikes-on-sale/products.json?limit=50",
    ])
    gbp_to_eur = source_config.get("gbp_to_eur", 1.16)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    deals = []

    for url in urls:
        print(f"  fetching...")
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"  error: {e}")
            continue

        for product in data.get("products", []):
            title = product.get("title", "").strip()
            if not title:
                continue

            handle = product.get("handle", "")
            link = f"https://www.planetx.co.uk/products/{handle}" if handle else "#"

            images = product.get("images", [])
            img = images[0].get("src", "") if images else ""

            # use first variant with compare_at_price
            for variant in product.get("variants", []):
                compare = variant.get("compare_at_price")
                price = variant.get("price")
                if compare and price:
                    try:
                        price_gbp = float(price)
                        was_gbp = float(compare)
                    except (ValueError, TypeError):
                        continue
                    if was_gbp > price_gbp > 0:
                        deals.append({
                            "title": title,
                            "category": guess_category(title, config),
                            "price_now": round(price_gbp * gbp_to_eur, 2),
                            "price_was": round(was_gbp * gbp_to_eur, 2),
                            "store": "Planet X",
                            "url": link,
                            "img": img,
                            "pick": False,
                        })
                        break  # only first variant

    return deals


# ---------------------------------------------------------------------------
# Source: Hollandbikeshop scraper
# ---------------------------------------------------------------------------
def fetch_hollandbikeshop(source_config, config):
    if not requests or not BeautifulSoup:
        print("  skipping hollandbikeshop (missing deps)")
        return []

    urls = source_config.get("urls", [
        "https://hollandbikeshop.com/fietsgereedschap-fietsonderhoud/fietsgereedschap/",
        "https://hollandbikeshop.com/fietsgereedschap-fietsonderhoud/fiets-schoonmaakmiddelen/",
        "https://hollandbikeshop.com/fietsgereedschap-fietsonderhoud/smeermiddel/",
    ])
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    }
    deals = []

    for url in urls:
        print(f"  {url.split('/')[-2]}...")
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
        except Exception as e:
            print(f"  error: {e}")
            continue

        soup = BeautifulSoup(resp.text, "html.parser")
        cards = soup.select("a.product-card")

        for card in cards:
            price_el = card.select_one(".product-card__price")
            small_el = card.select_one(".product-card__price small") if price_el else None
            if not small_el:
                continue  # not on sale

            title = card.get("title", "").strip()
            if not title:
                continue

            link = card.get("href", "#")
            if link.startswith("/"):
                link = "https://hollandbikeshop.com" + link

            img_el = card.select_one("img")
            img = ""
            if img_el:
                img = img_el.get("src", "")
                if img.startswith("/"):
                    img = "https://hollandbikeshop.com" + img

            try:
                was_text = small_el.get_text(strip=True)
                was_text = was_text.replace("€", "").replace("EUR", "").replace(".", "").replace(",", ".").strip()
                price_was = float(was_text)

                full_text = price_el.get_text(strip=True)
                now_text = full_text.replace(small_el.get_text(strip=True), "").strip()
                now_text = now_text.replace("€", "").replace("EUR", "").replace(".", "").replace(",", ".").strip()
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
                "store": "Hollandbikeshop",
                "url": link,
                "img": img,
                "pick": False,
            })

    return deals


# ---------------------------------------------------------------------------
# Source: Bike-Mailorder scraper (Shopify)
# ---------------------------------------------------------------------------
def fetch_bike_mailorder(source_config, config):
    if not requests or not BeautifulSoup:
        print("  skipping bike-mailorder (missing deps)")
        return []

    base_url = source_config.get("url",
        "https://www.bike-mailorder.com/nl-nl/collections/gereedschap-onderhoud")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    }
    max_pages = source_config.get("max_pages", 3)
    deals = []

    for page in range(1, max_pages + 1):
        print(f"  page {page}...")
        page_url = f"{base_url}?page={page}" if page > 1 else base_url
        try:
            resp = requests.get(page_url, headers=headers, timeout=30,
                                allow_redirects=True)
            resp.raise_for_status()
        except Exception as e:
            print(f"  error on page {page}: {e}")
            break

        soup = BeautifulSoup(resp.text, "html.parser")
        cards = soup.select("li.js-pagination-result")
        if not cards:
            break

        for card in cards:
            if not card.select_one(".price--on-sale"):
                continue

            link_el = card.select_one("a.js-prod-link")
            if not link_el:
                continue
            title = link_el.get("aria-label", "").strip()
            if not title:
                continue

            link = link_el.get("href", "#")
            if link.startswith("/"):
                link = "https://www.bike-mailorder.com" + link

            img_el = card.select_one("img.card__main-image")
            img = ""
            if img_el:
                img = img_el.get("src", "")
                if img.startswith("//"):
                    img = "https:" + img

            was_el = card.select_one("s.price__was")
            cur_el = card.select_one("strong.price__current")
            if not was_el or not cur_el:
                continue

            try:
                was_text = was_el.get_text(strip=True).replace("€", "").replace(".", "").replace(",", ".").strip()
                now_text = cur_el.get_text(strip=True).replace("€", "").replace(".", "").replace(",", ".").strip()
                # filter out "Niet beschikbaar" (sold out)
                if "niet" in now_text.lower() or "beschikbaar" in now_text.lower():
                    continue
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
                "store": "Bike-Mailorder",
                "url": link,
                "img": img,
                "pick": False,
            })

    return deals


# ---------------------------------------------------------------------------
# Source: Bike-Components scraper
# ---------------------------------------------------------------------------
def fetch_bike_components(source_config, config):
    if not requests or not BeautifulSoup:
        print("  skipping bike-components (missing deps)")
        return []

    urls = source_config.get("urls", [
        "https://www.bike-components.de/en/tools-maintenance/general-tools/",
        "https://www.bike-components.de/en/tools-maintenance/maintenance-products/",
    ])
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    }
    deals = []

    for url in urls:
        print(f"  {url.split('/')[-2]}...")
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
        except Exception as e:
            print(f"  error: {e}")
            continue

        soup = BeautifulSoup(resp.text, "html.parser")
        cards = soup.select(".product-item-extended")

        for card in cards:
            strike_el = card.select_one(".strike-price")
            if not strike_el:
                continue  # not on sale

            title_el = card.select_one("a.product-item")
            if not title_el:
                continue
            title = title_el.get("title", "").strip()
            if not title:
                continue

            link = title_el.get("href", "#")
            if link.startswith("/"):
                link = "https://www.bike-components.de" + link

            img_el = card.select_one(".site-product-image img")
            img = ""
            if img_el:
                img = img_el.get("src", "")
                if img.startswith("/"):
                    img = "https://www.bike-components.de" + img

            cur_el = card.select_one(".price.site-price")
            if not cur_el:
                continue

            try:
                # current price: "22.99€"
                now_text = cur_el.get_text(strip=True).replace("€", "").replace(",", "").strip()
                price_now = float(now_text)

                # strike price: "instead of26.43€"
                strike_text = strike_el.get_text(strip=True)
                was_match = re.search(r'[\d.]+', strike_text)
                if not was_match:
                    continue
                price_was = float(was_match.group())
            except (ValueError, AttributeError):
                continue

            if price_was <= 0 or price_now <= 0 or price_now >= price_was:
                continue

            deals.append({
                "title": title,
                "category": guess_category(title, config),
                "price_now": price_now,
                "price_was": price_was,
                "store": "Bike-Components",
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
        "rose-bikes": fetch_rose_bikes,
        "bikester": fetch_bikester,
        "lordgun": fetch_lordgun,
        "canyon": fetch_canyon,
        "12gobiking": fetch_12gobiking,
        "planetx": fetch_planetx,
        "hollandbikeshop": fetch_hollandbikeshop,
        "bike-mailorder": fetch_bike_mailorder,
        "bike-components": fetch_bike_components,
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
