#!/usr/bin/env python3
"""
IKEA 3D model (GLB) batch downloader.

Usage examples
--------------
  # Interactive mode (asks for country, categories, variants)
  python ikea-glb-downloader.py

  # List the categories available on the French site
  python ikea-glb-downloader.py --country fr --lang fr --list-categories

  # Download two categories (by ID), with all colour variants
  python ikea-glb-downloader.py --country fr --lang fr --categories fu002,li001 --variants

  # Download the whole catalogue, main variant only
  python ikea-glb-downloader.py --country fi --lang en --categories all --no-variants

  # Still works with a raw category URL, as the original script did
  python ikea-glb-downloader.py --url https://www.ikea.com/fi/en/cat/sofas-fu003/
"""

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional

import requests
from bs4 import BeautifulSoup
from tqdm import tqdm
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException
from webdriver_manager.chrome import ChromeDriverManager

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Suppress webdriver-manager logging
logging.getLogger("WDM").setLevel(logging.NOTSET)
os.environ["WDM_LOG_LEVEL"] = "0"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# CSS selectors used on the IKEA site. Kept in one place so they are easy to
# fix if IKEA changes its front-end.
SEL_PLP_WRAPPER = ".plp-fragment-wrapper"
SEL_PLP_PRODUCT_LINK = ".plp-fragment-wrapper a.plp-product__image-link"
SEL_STYLE_PICKER = ".js-product-style-picker"
SEL_STYLE_LINK = ".pip-product-styles__link"
ID_XR_MODEL_SCRIPT = "pip-xr-viewer-model"

# Category IDs look like "fu002", "st001" (two letters + 3 digits) or are
# purely numeric ("700640", "19053"). The "products" index itself is excluded.
CATEGORY_ID_RE = re.compile(r"^(?:[a-z]{2}\d{3}|\d+)$")

SCROLL_PAUSE = 2.0  # seconds between scrolls when loading a listing page


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Category:
    id: str
    name: str
    url: str
    parent_id: Optional[str] = None
    children: List["Category"] = field(default_factory=list)

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "url": self.url,
            "parent_id": self.parent_id,
            "children": [c.to_dict() for c in self.children],
        }

    @staticmethod
    def from_dict(d) -> "Category":
        cat = Category(d["id"], d["name"], d["url"], d.get("parent_id"))
        cat.children = [Category.from_dict(c) for c in d.get("children", [])]
        return cat


def base_url(country: str, lang: str) -> str:
    return f"https://www.ikea.com/{country.lower()}/{lang.lower()}"


def category_id_from_url(url: str) -> Optional[str]:
    """'https://www.ikea.com/fr/fr/cat/tables-chaises-fu002/' -> 'fu002'."""
    m = re.search(r"/cat/([^/?#]+)/?(?:[?#].*)?$", url)
    if not m:
        return None
    slug = m.group(1)
    cat_id = slug.rsplit("-", 1)[-1]
    return cat_id if CATEGORY_ID_RE.match(cat_id) else None


# ---------------------------------------------------------------------------
# Browser handling (one shared driver for the whole run)
# ---------------------------------------------------------------------------

class Browser:
    """Lazily-created, reusable headless Chrome driver."""

    def __init__(self, headless: bool = True):
        self.headless = headless
        self._driver = None

    @property
    def driver(self):
        if self._driver is None:
            self._driver = self._create()
        return self._driver

    def _create(self):
        options = webdriver.ChromeOptions()
        if self.headless:
            options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--log-level=3")
        options.add_argument(f"--user-agent={USER_AGENT}")
        options.add_experimental_option("excludeSwitches", ["enable-logging"])

        try:
            service = Service(ChromeDriverManager(log_level=0).install())
        except TypeError:
            service = Service(ChromeDriverManager().install())

        original_stderr = sys.stderr
        sys.stderr = open(os.devnull, "w")
        try:
            driver = webdriver.Chrome(service=service, options=options)
        finally:
            sys.stderr.close()
            sys.stderr = original_stderr
        return driver

    def get(self, url: str):
        """Load a URL, recreating the driver once if it has died."""
        try:
            self.driver.get(url)
        except WebDriverException:
            self.close()
            self.driver.get(url)

    def close(self):
        if self._driver is not None:
            try:
                self._driver.quit()
            except Exception:
                pass
            self._driver = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# ---------------------------------------------------------------------------
# Category discovery
# ---------------------------------------------------------------------------

def parse_category_index(html: str, base: str) -> List[Category]:
    """
    Parse IKEA's "all products" page into a list of top-level categories,
    each with its sub-categories.

    Page structure (same on every country site):
        <h2>Storage furniture</h2>
          <a href=".../cat/storage-furniture-st001/">Shop all</a>   <- parent
          <a href=".../cat/wardrobes-19053/">Wardrobes</a>          <- child
          ...
        <h2>Next category</h2>
    """
    soup = BeautifulSoup(html, "html.parser")
    groups: List[Category] = []
    current: Optional[Category] = None
    pending_name: Optional[str] = None

    for el in soup.find_all(["h2", "a"]):
        if el.name == "h2":
            pending_name = el.get_text(" ", strip=True)
            current = None
            continue

        href = el.get("href") or ""
        if "/cat/" not in href:
            continue
        if href.startswith("/"):
            href = "https://www.ikea.com" + href
        cat_id = category_id_from_url(href)
        if not cat_id:
            continue
        text = el.get_text(" ", strip=True)

        if pending_name is not None and current is None:
            # First /cat/ link after an <h2> is the "Shop all" link of that group
            current = Category(cat_id, pending_name, href)
            groups.append(current)
            continue

        if current is None:
            # Link before the first heading (e.g. "New lower price") – ignore
            continue

        if cat_id == current.id:
            continue
        if any(c.id == cat_id for c in current.children):
            continue
        current.children.append(Category(cat_id, text or cat_id, href, current.id))

    # Links that are themselves a top-level category (e.g. the image link
    # that precedes the next <h2>) should not be listed as children.
    top_ids = {g.id for g in groups}
    for g in groups:
        g.children = [c for c in g.children if c.id not in top_ids]

    return groups


def fetch_category_index(browser: Browser, country: str, lang: str) -> List[Category]:
    """Download and parse the category index. Tries plain HTTP first, falls
    back to Selenium if the page needs JavaScript."""
    base = base_url(country, lang)
    # The slug before the ID is ignored by IKEA, so this works for any country.
    index_url = f"{base}/cat/x-products/"

    html = ""
    try:
        resp = requests.get(index_url, headers={"User-Agent": USER_AGENT}, timeout=30)
        resp.raise_for_status()
        html = resp.text
    except requests.RequestException as e:
        print(f"HTTP fetch of category index failed ({e}); trying with the browser.")

    cats = parse_category_index(html, base) if html else []
    if not cats:
        browser.get(index_url)
        WebDriverWait(browser.driver, 30).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "a[href*='/cat/']"))
        )
        cats = parse_category_index(browser.driver.page_source, base)

    if not cats:
        raise RuntimeError(
            f"No categories found at {index_url}. Check the country/language "
            f"codes (e.g. fr/fr, fi/en, de/de) or whether IKEA changed its page."
        )
    return cats


def load_categories(browser: Browser, country: str, lang: str, refresh: bool = False) -> List[Category]:
    cache_file = f"categories-{country}-{lang}.json"
    if not refresh and os.path.exists(cache_file):
        with open(cache_file, encoding="utf-8") as f:
            data = json.load(f)
        return [Category.from_dict(d) for d in data]

    cats = fetch_category_index(browser, country, lang)
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump([c.to_dict() for c in cats], f, ensure_ascii=False, indent=2)
    return cats


def flatten(cats: List[Category]) -> List[Category]:
    out = []
    for c in cats:
        out.append(c)
        out.extend(c.children)
    return out


def print_categories(cats: List[Category], show_children: bool = True):
    n = 0
    for top in cats:
        n += 1
        print(f"{n:3d}. [{top.id}] {top.name}")
        if show_children:
            for child in top.children:
                n += 1
                print(f"{n:3d}.     [{child.id}] {child.name}")


def resolve_selection(selection: str, cats: List[Category]) -> List[Category]:
    """
    Turn a user selection into categories.

    Accepts: 'all', category IDs ('fu002'), list numbers ('3'), ranges ('3-7'),
    comma/space separated. Selecting a top-level category downloads it as a
    whole (its listing page already includes all sub-categories).
    """
    selection = selection.strip().lower()
    all_cats = flatten(cats)
    if selection in ("all", "*", ""):
        return list(cats)  # top-level pages cover everything

    by_id = {c.id: c for c in all_cats}
    chosen: Dict[str, Category] = {}
    for token in re.split(r"[,\s]+", selection):
        if not token:
            continue
        m = re.match(r"^(\d+)-(\d+)$", token)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            for i in range(lo, hi + 1):
                if 1 <= i <= len(all_cats):
                    chosen[all_cats[i - 1].id] = all_cats[i - 1]
            continue
        if token.isdigit() and token not in by_id:
            i = int(token)
            if 1 <= i <= len(all_cats):
                chosen[all_cats[i - 1].id] = all_cats[i - 1]
            else:
                print(f"Warning: no category number {i}")
            continue
        if token in by_id:
            chosen[token] = by_id[token]
        else:
            print(f"Warning: unknown category '{token}'")

    # Drop sub-categories whose parent is also selected
    parents = {c.id for c in chosen.values() if c.parent_id is None}
    return [c for c in chosen.values() if c.parent_id not in parents]


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------

def get_product_links(browser: Browser, url: str) -> List[str]:
    """Return the product URLs on one listing page (scrolls to load them all)."""
    driver = browser.driver
    try:
        browser.get(url)
        WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, SEL_PLP_WRAPPER))
        )

        last_height = driver.execute_script("return document.body.scrollHeight")
        while True:
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(SCROLL_PAUSE)
            new_height = driver.execute_script("return document.body.scrollHeight")
            if new_height == last_height:
                break
            last_height = new_height

        WebDriverWait(driver, 30).until(
            EC.presence_of_all_elements_located((By.CSS_SELECTOR, SEL_PLP_PRODUCT_LINK))
        )
        links = [
            a.get_attribute("href")
            for a in driver.find_elements(By.CSS_SELECTOR, SEL_PLP_PRODUCT_LINK)
        ]
        links = [l.split("?")[0] for l in links if l]
        return list(dict.fromkeys(links))  # dedupe, keep order
    except TimeoutException:
        print(f"Timeout while loading products on page: {url}")
        return []
    except Exception as e:
        print(f"Error while getting product links from {url}: {e}")
        return []


def iter_category_products(browser: Browser, category_url: str, max_pages: Optional[int] = None):
    """Walk all pages of a category, yielding product URLs. Stops when a page
    yields no *new* URLs (IKEA may serve the last page again for out-of-range
    page numbers), or after max_pages."""
    seen = set()
    page = 1
    while True:
        if max_pages and page > max_pages:
            break
        sep = "&" if "?" in category_url else "?"
        page_url = f"{category_url}{sep}page={page}"
        print(f"\nFetching product links from page {page}: {page_url}")
        links = get_product_links(browser, page_url)
        new = [l for l in links if l not in seen]
        print(f"Found {len(links)} products on this page ({len(new)} new)")
        if not new:
            break
        for l in new:
            seen.add(l)
            yield l
        page += 1


def get_color_variant_links(browser: Browser, url: str) -> List[str]:
    driver = browser.driver
    browser.get(url)
    variants = [url]
    try:
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, SEL_STYLE_PICKER))
        )
        picker = driver.find_element(By.CSS_SELECTOR, SEL_STYLE_PICKER)
        for a in picker.find_elements(By.CSS_SELECTOR, SEL_STYLE_LINK):
            href = a.get_attribute("href")
            if href:
                variants.append(href.split("?")[0])
    except TimeoutException:
        pass  # product has no variants
    return list(dict.fromkeys(variants))


def get_product_details(browser: Browser, url: str):
    """Return (name, color, glb_url) for a product page; glb_url may be None."""
    driver = browser.driver
    name, color = "Unknown", "Unknown"
    try:
        browser.get(url)
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "title")))

        title = (driver.title or "").strip()
        if title:
            name_color = title.split(" - IKEA")[0]
            m = re.match(r"(.*?),\s*(.*)", name_color)
            if m:
                name, color = m.groups()
            else:
                name, color = name_color, "Default"

        try:
            script = WebDriverWait(driver, 5).until(
                EC.presence_of_element_located((By.ID, ID_XR_MODEL_SCRIPT))
            )
            data = json.loads(script.get_attribute("innerHTML"))
            return name, color, data.get("url")
        except TimeoutException:
            print(f"GLB model script not found for {url}")
        except json.JSONDecodeError:
            print(f"Error decoding model JSON for {url}")
    except TimeoutException:
        print(f"Timeout while loading page: {url}")
    except Exception as e:
        print(f"Error processing {url}: {e}")
    return name, color, None


def download_glb(url: str, filename: str):
    with requests.get(url, stream=True, headers={"User-Agent": USER_AGENT}, timeout=60) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        with open(filename, "wb") as f, tqdm(
            desc=os.path.basename(filename), total=total,
            unit="iB", unit_scale=True, unit_divisor=1024,
        ) as bar:
            for chunk in r.iter_content(chunk_size=64 * 1024):
                bar.update(f.write(chunk))


def safe_filename(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "", name).strip()


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

class ProductDB:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.c = self.conn.cursor()
        self.c.execute(
            """CREATE TABLE IF NOT EXISTS products
               (url TEXT PRIMARY KEY, name TEXT, color TEXT, glb_url TEXT,
                downloaded INTEGER, category TEXT, country TEXT)"""
        )
        # Older databases from the original script lack the last two columns
        cols = {row[1] for row in self.c.execute("PRAGMA table_info(products)")}
        for col in ("category", "country"):
            if col not in cols:
                self.c.execute(f"ALTER TABLE products ADD COLUMN {col} TEXT")
        self.conn.commit()

    def seen(self, url: str) -> bool:
        self.c.execute("SELECT 1 FROM products WHERE url=?", (url,))
        return self.c.fetchone() is not None

    def add(self, url, name, color, glb_url, downloaded, category, country):
        self.c.execute(
            "INSERT OR REPLACE INTO products VALUES (?, ?, ?, ?, ?, ?, ?)",
            (url, name, color, glb_url, downloaded, category, country),
        )
        self.conn.commit()

    def close(self):
        self.conn.close()


# ---------------------------------------------------------------------------
# Processing
# ---------------------------------------------------------------------------

def process_product(browser: Browser, db: ProductDB, url: str, download_variants: bool,
                    out_dir: str, category_id: str, country: str) -> int:
    """Process one product (and its variants). Returns number of GLBs downloaded."""
    print(f"\nProcessing product: {url}")
    if db.seen(url):
        print("Skipping already processed product")
        return 0

    downloaded_count = 0
    try:
        variant_urls = get_color_variant_links(browser, url) if download_variants else [url]
        if download_variants:
            print(f"Found {len(variant_urls)} colour variant(s)")

        for variant_url in variant_urls:
            if db.seen(variant_url):
                print(f"Skipping already processed variant: {variant_url}")
                continue

            name, color, glb_url = get_product_details(browser, variant_url)
            print(f"  {name} - {color}")

            downloaded = 0
            if glb_url:
                filename = safe_filename(f"{name} - {color}.glb")
                full_path = os.path.join(out_dir, filename)
                try:
                    download_glb(glb_url, full_path)
                    downloaded = 1
                    downloaded_count += 1
                except requests.RequestException as e:
                    print(f"  Download failed: {e}")
            else:
                print(f"  No GLB file for {name} - {color}")

            db.add(variant_url, name, color, glb_url, downloaded, category_id, country)
    except Exception as e:
        print(f"Error processing product {url}: {e}")
    return downloaded_count


def run_download(browser: Browser, db: ProductDB, targets, download_variants: bool,
                 out_dir: str, country: str, max_pages: Optional[int]):
    """targets: list of (category_id, label, url)."""
    os.makedirs(out_dir, exist_ok=True)
    total_products = total_files = 0
    for cat_id, label, url in targets:
        print(f"\n{'=' * 70}\nCategory: {label} [{cat_id}]\n{url}\n{'=' * 70}")
        for i, link in enumerate(iter_category_products(browser, url, max_pages), 1):
            print(f"\n[{label}] item {i}")
            total_files += process_product(browser, db, link, download_variants, out_dir, cat_id, country)
            total_products += 1
    print(f"\nDone. Products processed: {total_products}, GLB files downloaded: {total_files}")


# ---------------------------------------------------------------------------
# CLI / interactive
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Batch-download IKEA 3D models (GLB). Run without arguments for interactive mode.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--country", "-c", help="IKEA country code, e.g. fr, fi, de, us")
    p.add_argument("--lang", "-l", help="Site language code, e.g. fr, en, de (defaults to the country code)")
    p.add_argument("--list-categories", action="store_true", help="Print the category list and exit")
    p.add_argument("--categories", help="Category IDs or list numbers to download, comma separated, or 'all'")
    p.add_argument("--url", action="append", help="Download a raw category URL (can be repeated)")
    v = p.add_mutually_exclusive_group()
    v.add_argument("--variants", dest="variants", action="store_true", default=None,
                   help="Download every colour variant of each product")
    v.add_argument("--no-variants", dest="variants", action="store_false",
                   help="Download only the main variant")
    p.add_argument("--output", "-o", default="downloaded-files", help="Output directory (default: downloaded-files)")
    p.add_argument("--db", default="ikea_products.db", help="SQLite database path (default: ikea_products.db)")
    p.add_argument("--max-pages", type=int, default=None, help="Max listing pages per category (for testing)")
    p.add_argument("--refresh-categories", action="store_true", help="Ignore the cached category list")
    p.add_argument("--no-headless", action="store_true", help="Show the browser window")
    return p


def ask(prompt: str, default: Optional[str] = None) -> str:
    suffix = f" [{default}]" if default else ""
    try:
        value = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        value = ""
    return value or (default or "")


def ask_yes_no(prompt: str, default: bool = False) -> bool:
    d = "Y/n" if default else "y/N"
    v = ask(f"{prompt} ({d})").lower()
    if not v:
        return default
    return v in ("y", "yes", "o", "oui")


def main(argv=None):
    args = build_parser().parse_args(argv)
    interactive = sys.stdin.isatty()

    # --- raw URL mode (original behaviour) ---------------------------------
    if args.url:
        variants = args.variants
        if variants is None:
            variants = ask_yes_no("Download all colour variants?") if interactive else False
        m = re.search(r"ikea\.com/([a-z]{2})/", args.url[0])
        country = args.country or (m.group(1) if m else "?")
        targets = [(category_id_from_url(u) or "url", u, u) for u in args.url]
        with Browser(headless=not args.no_headless) as browser:
            db = ProductDB(args.db)
            try:
                run_download(browser, db, targets, variants, args.output, country, args.max_pages)
            finally:
                db.close()
        return

    # --- country / language -----------------------------------------------
    country = args.country
    lang = args.lang
    if not country:
        if not interactive:
            sys.exit("Error: --country is required in non-interactive mode (or use --url).")
        country = ask("IKEA country code (e.g. fr, fi, de, us)", "fr")
        if not lang:
            lang = ask("Site language code", country)
    country = country.lower()
    lang = (lang or country).lower()

    with Browser(headless=not args.no_headless) as browser:
        print(f"Loading categories for ikea.com/{country}/{lang} ...")
        cats = load_categories(browser, country, lang, refresh=args.refresh_categories)
        all_cats = flatten(cats)
        print(f"{len(cats)} top-level categories, {len(all_cats) - len(cats)} sub-categories.\n")

        if args.list_categories:
            print_categories(cats)
            return

        selection = args.categories
        if selection is None:
            if not interactive:
                sys.exit("Error: --categories is required in non-interactive mode (use 'all' for everything).")
            print_categories(cats)
            print("\nEnter category numbers or IDs (comma separated, ranges like 3-7 allowed), or 'all'.")
            selection = ask("Categories to download", "all")

        chosen = resolve_selection(selection, cats)
        if not chosen:
            sys.exit("Nothing selected.")

        variants = args.variants
        if variants is None:
            variants = ask_yes_no("Download all colour variants?") if interactive else False

        print("\nWill download:")
        for c in chosen:
            print(f"  [{c.id}] {c.name}")
        print(f"Variants: {'all colours' if variants else 'main only'}  |  Output: {args.output}")
        if interactive and args.categories is None and not ask_yes_no("Proceed?", True):
            return

        targets = [(c.id, c.name, c.url) for c in chosen]
        db = ProductDB(args.db)
        try:
            run_download(browser, db, targets, variants, args.output, country, args.max_pages)
        finally:
            db.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.")
