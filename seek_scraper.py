# seek_scraper.py
from playwright.sync_api import sync_playwright
import pandas as pd
import time
import re
import os
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse
from datetime import datetime, timedelta

# ----------------------------
# Env helpers
# ----------------------------
def env_str(name: str, default: str) -> str:
    v = os.getenv(name)
    return v if (v is not None and str(v).strip() != "") else default

def env_int(name: str, default: int) -> int:
    try:
        return int(env_str(name, str(default)))
    except Exception:
        return default

def env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "y")


# ----------------------------
# URL & parsing helpers
# ----------------------------
def build_seek_url(keyword: str, min_salary: int = 150000, listing_date: int = 1) -> str:
    """
    Build Seek search URL.
    listing_date (Seek convention):
      0 = any time, 1 = last 24 hours, 3 = last 3 days, etc.
    """
    base = "https://www.seek.com.au"
    keyword_slug = re.sub(r"\s+", "-", keyword.strip()).lower()
    query = f"{keyword_slug}-jobs/in-All-Australia"
    filters = f"?salaryrange={min_salary}-999999&daterange={listing_date}"
    return f"{base}/{query}{filters}"

def set_query_param(url: str, key: str, value: str) -> str:
    u = urlparse(url)
    q = parse_qs(u.query)
    q[key] = [value]
    new_query = urlencode(q, doseq=True)
    return urlunparse((u.scheme, u.netloc, u.path, u.params, new_query, u.fragment))

def extract_job_id_from_url(url: str):
    m = re.search(r"/job/(\d+)", url or "")
    return m.group(1) if m else None

def posted_text_to_hours(txt: str) -> float:
    """
    Convert Seek's posted-time text to hours.
    Handles: 'Just posted', 'Today', '10h ago', '1d ago',
             'Posted 12 hours ago', 'Posted 1 day ago', '30+ days ago'
    """
    if not txt:
        return float("inf")
    t = txt.strip().strip('"').lower()

    if "just" in t or "today" in t:
        return 0.0

    m = re.search(r"(\d+)\s*(h|hour)", t)
    if m:
        return float(m.group(1))

    m = re.search(r"(\d+)\s*(d|day)", t)
    if m:
        return float(m.group(1)) * 24.0

    m = re.search(r"posted\s+(\d+)\s+hour", t)
    if m:
        return float(m.group(1))
    m = re.search(r"posted\s+(\d+)\s+day", t)
    if m:
        return float(m.group(1)) * 24.0

    if "30+" in t or "month" in t or "week" in t:
        return float("inf")

    return float("inf")


# ----------------------------
# Headless mode resolver
# ----------------------------
def resolve_headless(headless_param: bool | None) -> tuple[bool, float]:
    """
    Decide headless mode:
      - If HEADLESS env is set, use it.
      - Else if running in CI and no DISPLAY, default headless True.
      - Else if DISPLAY is present (e.g., Xvfb), default headed (False).
      - Else default headless True.
    Also returns slow_mo (seconds) from env SLOW_MO (only applied when headed).
    """
    # explicit env override wins
    if os.getenv("HEADLESS") not in (None, ""):
        headless = env_bool("HEADLESS", True)
    elif headless_param is not None:
        headless = headless_param
    else:
        in_ci = env_bool("CI", False)
        has_display = os.getenv("DISPLAY") not in (None, "")
        if in_ci and not has_display:
            headless = True
        elif has_display:
            headless = False
        else:
            headless = True

    slow_mo = float(env_str("SLOW_MO", "0"))
    # never slow-mo in headless/CI to keep runs fast and stable
    if headless or env_bool("CI", False):
        slow_mo = 0.0
    return headless, slow_mo


# ----------------------------
# Pagination helpers
# ----------------------------
# These selectors are present in Seek pagination; we’ll read the biggest page number.
PAGINATION_LINK_SELECTORS = [
    "a[data-automation^='page-']",
    "a[data-testid^='pagination-page-']",
    "nav[aria-label='Pagination'] a",
]

def max_page_from_dom(page) -> int:
    nums = []
    loc = page.locator(", ".join(PAGINATION_LINK_SELECTORS))
    try:
        n = loc.count()
    except Exception:
        n = 0
    for i in range(n):
        a = loc.nth(i)
        # data-automation="page-3" / aria-label="Go to page 3"
        labels = [
            a.get_attribute("data-automation") or "",
            a.get_attribute("aria-label") or "",
        ]
        txt = (a.inner_text() or "").strip()
        labels.append(txt)
        for s in labels:
            m = re.search(r"\bpage[-\s]?(\d+)\b", s, re.I)
            if m:
                nums.append(int(m.group(1)))
                break
            if s.isdigit():
                nums.append(int(s))
                break
    return max(nums) if nums else 1


# ----------------------------
# Core scraper (now with pagination)
# ----------------------------
def scrape_jobs(
    keyword: str = "Senior Insight Analyst",
    min_salary: int = 150000,
    listing_date: int = 1,
    headless: bool | None = None,
    csv_path: str = "seek_jobs_24h.csv",
):
    """
    Scrape Seek listings (default last 24h) ACROSS ALL PAGES and write a CSV.
    Returns list[dict] with results.
    """
    headless, slow_mo = resolve_headless(headless)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, slow_mo=slow_mo)
        page = browser.new_page()
        page.set_viewport_size({"width": 1280, "height": 2000})

        base_url = build_seek_url(keyword, min_salary=min_salary, listing_date=listing_date)
        print(f"🔍 Opening: {base_url}")
        page.goto(base_url, wait_until="domcontentloaded")
        page.wait_for_selector('[data-automation="jobTitle"], a[data-testid="job-card-title"]', timeout=30000)

        # Determine how many pages to scrape
        last_page = max_page_from_dom(page)
        print(f"📑 Detected {last_page} page(s)")

        all_results = []
        seen_links = set()

        # Helper: scrape the current page into results (your existing logic)
        def scrape_current_page():
            nonlocal all_results, seen_links
            job_cards = page.locator('[data-automation="normalJob"], [data-testid="job-card"], article[data-automation="job-card"]')
            count = job_cards.count()
            if count == 0:
                return

            for i in range(count):
                card = job_cards.nth(i)
                try:
                    card.scroll_into_view_if_needed()
                except Exception:
                    pass
                time.sleep(0.15 if slow_mo == 0 else 0.0)

                # Title & company (with fallback title selector)
                title = card.locator('[data-automation="jobTitle"], a[data-testid="job-card-title"]').first.inner_text().strip()
                company = card.locator('[data-automation="jobCompany"], [data-testid="job-card-company"]').first.inner_text().strip()

                # Posted label
                posted_el = card.locator('[data-automation="jobListingDate"], [data-testid="job-card-date"]').first
                posted_text = ""
                if posted_el.count() > 0:
                    posted_text = posted_el.inner_text().strip()
                    if not posted_text:
                        try:
                            posted_text = posted_el.evaluate(
                                "el => window.getComputedStyle(el, '::before').getPropertyValue('content')"
                            )
                            posted_text = posted_text.strip('"')
                        except Exception:
                            posted_text = ""

                hours_old = posted_text_to_hours(posted_text)
                if hours_old > 24:
                    continue

                now_local = datetime.now().astimezone()
                posted_dt_local = (now_local - timedelta(hours=hours_old)).replace(
                    minute=0, second=0, microsecond=0
                )
                posted_dt_local_str = posted_dt_local.strftime("%Y-%m-%d %H:00 %Z")

                # Detail link (robust)
                href = None
                overlay = card.locator('a[data-automation="job-list-item-link-overlay"]').first
                if overlay.count() > 0:
                    href = overlay.get_attribute("href")
                if not href:
                    link2 = card.locator('a:has([data-automation="jobTitle"]), a:has(a[data-testid="job-card-title"])').first
                    if link2.count() > 0:
                        href = link2.get_attribute("href")
                if not href:
                    anchors = card.locator("a")
                    for idx in range(anchors.count()):
                        h = anchors.nth(idx).get_attribute("href") or ""
                        if "/job/" in h:
                            href = h
                            break
                if not href:
                    print(f"⚠️ No detail link for: {title} | {company}")
                    continue

                detail_url = urljoin("https://www.seek.com.au", href)
                if detail_url in seen_links:
                    continue
                seen_links.add(detail_url)

                job_id = extract_job_id_from_url(detail_url)

                # Open detail in a new tab
                detail_page = browser.new_page()
                try:
                    print(f"➡️  {title} | {company} | {posted_text} → {posted_dt_local_str} | {detail_url}")
                    detail_page.goto(detail_url, wait_until="domcontentloaded")
                    detail_page.wait_for_selector('[data-automation="jobAdDetails"]', timeout=20000)

                    def safe_text(pg, selector: str, default: str = ""):
                        loc = pg.locator(selector)
                        return loc.first.inner_text().strip() if loc.count() > 0 else default

                    location = safe_text(detail_page, '[data-automation="job-detail-location"]')
                    category = safe_text(detail_page, '[data-automation="job-detail-classifications"]')
                    work_type = safe_text(detail_page, '[data-automation="job-detail-work-type"]')
                    salary = safe_text(detail_page, '[data-automation="job-detail-salary"]')

                    ad_loc = detail_page.locator('[data-automation="jobAdDetails"]').first
                    ad_text = ad_loc.inner_text().strip() if ad_loc.count() > 0 else ""

                    all_results.append({
                        "Job ID": job_id,
                        "Job Title": title,
                        "Company": company,
                        "Detail URL": detail_url,
                        "Posted Label": posted_text,
                        "Hours Old": round(hours_old, 2),
                        "Posted Datetime (Local)": posted_dt_local.isoformat(),
                        "Location": location,
                        "Category": category,
                        "Work Type": work_type,
                        "Salary": salary,
                        "Ad Text": ad_text
                    })
                finally:
                    try:
                        detail_page.close()
                    except Exception:
                        pass

        # Scrape first page
        scrape_current_page()

        # Scrape remaining pages if any
        if last_page > 1:
            for pageno in range(2, last_page + 1):
                page_url = set_query_param(base_url, "page", str(pageno))
                print(f"📄 Page {pageno}/{last_page} → {page_url}")
                page.goto(page_url, wait_until="domcontentloaded")
                try:
                    page.wait_for_selector('[data-automation="jobTitle"], a[data-testid="job-card-title"]', timeout=30000)
                except Exception:
                    # If a variant loads slower, give a tiny nudge
                    page.mouse.wheel(0, 1500)
                    page.wait_for_timeout(500)
                scrape_current_page()

        browser.close()

        # Always write CSV (even if empty)
        df = pd.DataFrame(all_results)
        df.to_csv(csv_path, index=False)
        print(f"✅ Scraped {len(all_results)} job(s). CSV written: {csv_path}")
        return all_results


# ----------------------------
# CLI entry
# ----------------------------
if __name__ == "__main__":
    # Allow env overrides for GitHub Actions / n8n
    kw = env_str("KEYWORD", "Senior Insight Analyst")
    min_sal = env_int("MIN_SALARY", 150000)
    ldate = env_int("LISTING_DATE", 1)
    csv_out = env_str("CSV_PATH", "seek_jobs_24h.csv")

    # HEADLESS env explicitly wins; otherwise auto (CI/no DISPLAY -> headless)
    headless_env = os.getenv("HEADLESS")
    if headless_env is not None and headless_env.strip() != "":
        headless_val = env_bool("HEADLESS", True)
    else:
        headless_val = None  # delegate to resolve_headless()

    scrape_jobs(
        keyword=kw,
        min_salary=min_sal,
        listing_date=ldate,
        headless=headless_val,
        csv_path=csv_out,
    )
