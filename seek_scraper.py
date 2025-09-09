# seek_scraper.py
from playwright.sync_api import sync_playwright
import pandas as pd
import time
import re
import os
from urllib.parse import urljoin
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
# Core scraper
# ----------------------------
def scrape_jobs(
    keyword: str = "Senior Insight Analyst",
    min_salary: int = 150000,
    listing_date: int = 1,
    headless: bool | None = None,
    csv_path: str = "seek_jobs_24h.csv",
):
    """
    Scrape Seek listings (default last 24h) and write a CSV.
    Returns list[dict] with results.
    """
    headless, slow_mo = resolve_headless(headless)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, slow_mo=slow_mo)
        page = browser.new_page()
        page.set_viewport_size({"width": 1280, "height": 2000})

        url = build_seek_url(keyword, min_salary=min_salary, listing_date=listing_date)
        print(f"🔍 Opening: {url}")
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_selector('[data-automation="jobTitle"]', timeout=30000)

        job_cards = page.locator('[data-automation="normalJob"]')
        count = job_cards.count()
        if count == 0:
            print("No jobs found.")
            pd.DataFrame([]).to_csv(csv_path, index=False)
            browser.close()
            return []

        results = []
        for i in range(count):
            card = job_cards.nth(i)
            try:
                card.scroll_into_view_if_needed()
            except Exception:
                pass
            time.sleep(0.15 if slow_mo == 0 else 0.0)  # light politeness delay only if not slow-mo

            # Title & company
            title = card.locator('[data-automation="jobTitle"]').first.inner_text().strip()
            company = card.locator('[data-automation="jobCompany"]').first.inner_text().strip()

            # Posted label (Seek sometimes renders via ::before)
            posted_el = card.locator('[data-automation="jobListingDate"]').first
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

            # Compute local posted datetime to the hour
            now_local = datetime.now().astimezone()
            posted_dt_local = (now_local - timedelta(hours=hours_old)).replace(
                minute=0, second=0, microsecond=0
            )
            posted_dt_local_str = posted_dt_local.strftime("%Y-%m-%d %H:00 %Z")

            # Robustly find the detail link
            href = None
            overlay = card.locator('a[data-automation="job-list-item-link-overlay"]').first
            if overlay.count() > 0:
                href = overlay.get_attribute("href")
            if not href:
                link2 = card.locator('a:has([data-automation="jobTitle"])').first
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
            job_id = extract_job_id_from_url(detail_url)

            # Open detail in a new tab
            detail_page = browser.new_page()
            try:
                print(f"➡️  ({i+1}/{count}) {title} | {company} | {posted_text} → {posted_dt_local_str} | {detail_url}")
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

                results.append({
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

        browser.close()

        # Always write CSV (even if empty)
        df = pd.DataFrame(results)
        df.to_csv(csv_path, index=False)
        print(f"✅ Scraped {len(results)} job(s). CSV written: {csv_path}")
        return results


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
