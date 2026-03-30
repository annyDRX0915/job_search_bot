import re
import time
from urllib.parse import quote_plus

import pandas as pd
from playwright.sync_api import sync_playwright

KEYWORDS = [
    "machine learning engineer",
    "ai engineer",
    "data scientist",
    "applied scientist",
    "computer vision engineer",
    "nlp engineer",
    "ml engineer",
    "software engineer",
    "forward deployed engineer",
    "research engineer",
    "research scientist",
]

LOCATIONS = [
    "Canada",
]

# r86400 = 24h, r604800 = 7d, r2592000 = 30d
TIME_RANGE = "r86400"

# Companies that are gig/task-based or part-time platforms
COMPANY_BLOCKLIST = {
    "mercor",
    "dataannotation",
    "data annotation",
    "outlier",
    "appen",
    "scale ai",  # task annotation work
    "remotasks",
    "lionbridge",
    "telus international",
    "taskus",
    "clickworker",
}

TITLE_KEYWORDS = [
    "machine learning",
    "ml engineer",
    "data scientist",
    "data science",
    "ai engineer",
    "artificial intelligence",
    "applied scientist",
    "applied ml",
    "software engineer",
    "software developer",
    "sde",
    "swe",
    "forward deployed",
    "nlp",
    "computer vision",
    "deep learning",
    "research engineer",
    "research scientist",
    "platform engineer",
    "backend engineer",
    "full stack engineer",
    "fullstack engineer",
]

TITLE_EXCLUDE = [
    "senior",
    "sr.",
    " sr ",
    "staff",
    "principal",
    "director",
    "manager",
    "vice president",
    " vp ",
    "head of",
    "lead ",
    " lead",
    "intern",
    "internship",
    "co-op",
    "coop",
    "distinguished",
    "fellow",
]


def is_relevant(title: str) -> bool:
    if not title:
        return False
    lower = title.lower()
    return any(kw in lower for kw in TITLE_KEYWORDS)


def is_blocked_company(company: str) -> bool:
    if not company:
        return False
    return any(blocked in company.lower() for blocked in COMPANY_BLOCKLIST)


_YEARS_RE = re.compile(r"(\d+)\s*\+?\s*(?:to\s*\d+\s*)?years?", re.IGNORECASE)

PHD_PATTERNS = ["ph.d", "phd", "doctorate", "doctoral degree"]


def is_entry_level(title: str) -> bool:
    if not title:
        return False
    lower = title.lower()
    return not any(kw in lower for kw in TITLE_EXCLUDE)


def is_entry_level_description(description: str, title: str = "") -> bool:
    if not description:
        # No description fetched — fall back to title-only check
        # If title already passed is_entry_level(), allow it through
        return True
    d = description.lower()
    if any(p in d for p in PHD_PATTERNS):
        return False
    for match in _YEARS_RE.finditer(d):
        if int(match.group(1)) >= 5:  # exclude 5+ years (was > 5, missed "5 years"/"5+ years")
            return False
    # Catch senior-level language in description even if title looked fine
    SENIOR_PHRASES = [
        "5+ years", "6+ years", "7+ years", "8+ years",
        "senior engineer", "senior developer", "senior scientist",
        "lead engineer", "staff engineer", "principal engineer",
    ]
    if any(p in d for p in SENIOR_PHRASES):
        return False
    return True


def fetch_description(page, url: str) -> str:
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(2)
        for selector in [
            "div.description__text",
            "div.show-more-less-html__markup",
            "div#job-details",
        ]:
            el = page.locator(selector).first
            if el.count() > 0:
                return el.inner_text()
    except Exception:
        pass
    return ""


def build_linkedin_search_url(keyword: str, location: str, start: int = 0) -> str:
    return (
        "https://www.linkedin.com/jobs/search/"
        f"?keywords={quote_plus(keyword)}"
        f"&location={quote_plus(location)}"
        f"&f_TPR={TIME_RANGE}"
        f"&start={start}"
    )


def scroll_page(page, rounds: int = 4, pause: float = 1.2):
    """Scroll the job results list to trigger lazy loading."""
    for _ in range(rounds):
        try:
            # Try scrolling the job results list panel specifically
            list_panel = page.locator("ul.jobs-search__results-list, div.jobs-search-results-list").first
            if list_panel.count() > 0:
                list_panel.evaluate("el => el.scrollBy(0, 3000)")
            else:
                page.keyboard.press("End")
            time.sleep(pause)
        except Exception:
            break


def extract_jobs_from_page(page, keyword: str, location: str):
    jobs = []

    cards = page.locator("div.base-card").all()
    if not cards:
        cards = page.locator("li").all()

    for card in cards:
        try:
            title = None
            company = None
            job_location = None
            url = None
            posted = None

            title_locator = card.locator("h3")
            if title_locator.count() > 0:
                title = title_locator.first.inner_text().strip()

            company_locator = card.locator("h4")
            if company_locator.count() > 0:
                company = company_locator.first.inner_text().strip()

            loc_locator = card.locator(".job-search-card__location")
            if loc_locator.count() > 0:
                job_location = loc_locator.first.inner_text().strip()

            link_locator = card.locator("a")
            if link_locator.count() > 0:
                url = link_locator.first.get_attribute("href")

            time_locator = card.locator("time")
            if time_locator.count() > 0:
                posted = time_locator.first.get_attribute("datetime") or time_locator.first.inner_text().strip()

            if title:
                jobs.append({
                    "source": "linkedin",
                    "search_keyword": keyword,
                    "search_location": location,
                    "company": company,
                    "title": title,
                    "location": job_location,
                    "url": url,
                    "posted_at": posted,
                })

        except Exception:
            continue

    return jobs


def dedupe_jobs(df: pd.DataFrame) -> pd.DataFrame:
    if "url" in df.columns:
        df = df.drop_duplicates(subset=["url"], keep="first")
    df = df.drop_duplicates(subset=["source", "company", "title", "location"], keep="first")
    return df


def main():
    all_jobs = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()

        for keyword in KEYWORDS:
            for location in LOCATIONS:
                for start in [0]:
                    url = build_linkedin_search_url(keyword, location, start)
                    print(f"Fetching: {url}")
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=60000)
                        time.sleep(3)
                        scroll_page(page)
                        jobs = extract_jobs_from_page(page, keyword, location)
                        print(f"  got {len(jobs)} jobs")
                        all_jobs.extend(jobs)
                        time.sleep(3)
                    except Exception as e:
                        print(f"  failed: {e}")
                        continue

        browser.close()

    if not all_jobs:
        print("No jobs found.")
        return

    df = pd.DataFrame(all_jobs)
    df = dedupe_jobs(df)

    before = len(df)
    df = df[~df["company"].apply(is_blocked_company)].reset_index(drop=True)
    print(f"Filtered to {len(df)} jobs after removing blocked companies (dropped {before - len(df)})")

    before = len(df)
    df = df[df["title"].apply(is_relevant)].reset_index(drop=True)
    print(f"Filtered to {len(df)} relevant jobs by title (dropped {before - len(df)})")

    before = len(df)
    df = df[df["title"].apply(is_entry_level)].reset_index(drop=True)
    print(f"Filtered to {len(df)} entry-level jobs by title (dropped {before - len(df)})")

    # Fetch descriptions and filter by experience/PhD requirements
    print(f"Fetching descriptions for {len(df)} jobs to check experience requirements...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        descriptions = []
        for i, row in df.iterrows():
            desc = fetch_description(page, row["url"]) if row.get("url") else ""
            descriptions.append(desc)
            if (i + 1) % 10 == 0:
                print(f"  {i + 1}/{len(df)} descriptions fetched")
        browser.close()

    df["description"] = descriptions

    before = len(df)
    df = df[df.apply(lambda r: is_entry_level_description(r["description"], r["title"]), axis=1)].reset_index(drop=True)
    print(f"Filtered to {len(df)} jobs after experience/PhD check (dropped {before - len(df)})")

    df.to_csv("linkedin_jobs.csv", index=False, encoding="utf-8-sig")
    print(f"Saved {len(df)} jobs to linkedin_jobs.csv")


if __name__ == "__main__":
    main()
