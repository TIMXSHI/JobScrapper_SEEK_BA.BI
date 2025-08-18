from playwright.sync_api import sync_playwright
import pandas as pd
import time, re
from urllib.parse import urljoin
from datetime import datetime, timedelta  # ✅ for hour-accurate conversion

def build_seek_url(keyword, min_salary=150000, listing_date=1):
    """
    listing_date:
      0 = any time, 1 = last 24 hours, 3 = last 3 days, etc. (Seek convention)
    We still ALSO filter by the visible posted text to be extra sure.
    """
    base = "https://www.seek.com.au"
    keyword_slug = keyword.strip().replace(" ", "-").lower()
    query = f"{keyword_slug}-jobs/in-All-Australia"
    filters = f"?salaryrange={min_salary}-999999&listingdate={listing_date}"
    return f"{base}/{query}{filters}"

def extract_job_id_from_url(url: str):
    m = re.search(r"/job/(\d+)", url or "")
    return m.group(1) if m else None

def posted_text_to_hours(txt: str) -> float:
    """
    Convert Seek's posted-time text to hours.
    Handles variants like:
      'Just posted', 'Posted today', 'Today', '10h ago', '1d ago',
      'Posted 12 hours ago', 'Posted 1 day ago', '30+ days ago'
    """
    if not txt:
        return float("inf")
    t = txt.strip().strip('"').lower()

    # common quick cases
    if "just" in t or "today" in t:
        return 0.0

    # hours
    m = re.search(r"(\d+)\s*(h|hour)", t)
    if m:
        return float(m.group(1))

    # days
    m = re.search(r"(\d+)\s*(d|day)", t)
    if m:
        return float(m.group(1)) * 24.0

    # verbose 'Posted X hours/day(s) ago'
    m = re.search(r"posted\s+(\d+)\s+hour", t)
    if m:
        return float(m.group(1))
    m = re.search(r"posted\s+(\d+)\s+day", t)
    if m:
        return float(m.group(1)) * 24.0

    # '30+ days ago' or anything older
    if "30+" in t or "month" in t or "week" in t:
        return float("inf")

    # fallback unknown
    return float("inf")

def scrape_jobs(keyword="Senior Insight Analyst", headless=False, csv_path="seek_jobs_24h.csv"):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, slow_mo=200)
        page = browser.new_page()
        page.set_viewport_size({"width": 1280, "height": 2000})

        url = build_seek_url(keyword, listing_date=1)  # ask Seek for last 24h
        print(f"🔍 Opening: {url}")
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_selector('[data-automation="jobTitle"]')

        job_cards = page.locator('[data-automation="normalJob"]')
        count = job_cards.count()
        if count == 0:
            print("No jobs found.")
            browser.close()
            return []

        results = []
        for i in range(count):
            card = job_cards.nth(i)
            card.scroll_into_view_if_needed()
            time.sleep(0.25)

            # Title & company
            title = card.locator('[data-automation="jobTitle"]').first.inner_text().strip()
            company = card.locator('[data-automation="jobCompany"]').first.inner_text().strip()

            # Posted label (Seek renders text via ::before sometimes)
            posted_el = card.locator('[data-automation="jobListingDate"]').first
            posted_text = ""
            if posted_el.count() > 0:
                # Try actual text
                posted_text = posted_el.inner_text().strip()
                # If empty, try ::before content which many listings use
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
                # Skip anything older than 24h
                continue

            # ✅ compute local posted datetime to the hour
            now_local = datetime.now().astimezone()
            posted_dt_local = (now_local - timedelta(hours=hours_old)).replace(minute=0, second=0, microsecond=0)
            posted_dt_local_str = posted_dt_local.strftime("%Y-%m-%d %H:00 %Z")

            # Find detail href robustly
            href = None
            overlay = card.locator('a[data-automation="job-list-item-link-overlay"]').first
            if overlay.count() > 0:
                href = overlay.get_attribute("href")

            if not href:
                # Fallback: anchor around the title
                link2 = card.locator('a:has([data-automation="jobTitle"])').first
                if link2.count() > 0:
                    href = link2.get_attribute("href")

            if not href:
                # Last fallback: any anchor containing /job/<id>
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

            # Open detail in a new tab to avoid losing the list
            detail_page = browser.new_page()
            try:
                # ✅ progress line shows label AND transformed datetime
                print(f"➡️  ({i+1}/{count}) {title} | {company} | {posted_text} → {posted_dt_local_str} | {detail_url}")

                detail_page.goto(detail_url, wait_until="domcontentloaded")
                detail_page.wait_for_selector('[data-automation="jobAdDetails"]', timeout=15000)

                def safe_text(pg, selector: str, default: str = ""):
                    loc = pg.locator(selector)
                    return loc.first.inner_text().strip() if loc.count() > 0 else default

                location = safe_text(detail_page, '[data-automation="job-detail-location"]')
                category = safe_text(detail_page, '[data-automation="job-detail-classifications"]')
                work_type = safe_text(detail_page, '[data-automation="job-detail-work-type"]')
                salary = safe_text(detail_page, '[data-automation="job-detail-salary"]')

                ad_loc = detail_page.locator('[data-automation="jobAdDetails"]').first
                ad_text = ad_loc.inner_text().strip()

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
                detail_page.close()

        browser.close()

        # Save CSV
        df = pd.DataFrame(results)
        print(df)
        return results

if __name__ == "__main__":
    scrape_jobs(headless=False, csv_path="seek_jobs_24h.csv")
