import hashlib
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import quote_plus


def _make_job_key(title: str, company: str, location: str) -> str:
    def _n(s): return re.sub(r'[^a-z0-9]', '', (s or '').lower().strip())
    raw = f"{_n(title)}|{_n(company)}|{_n(location)}"
    return hashlib.md5(raw.encode()).hexdigest()[:12]

import pandas as pd
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()

sys.path.insert(0, str(Path(__file__).parent.parent))
_USE_SHEETS = bool(os.getenv("SPREADSHEET_ID"))
if _USE_SHEETS:
    from utils.gsheets import write_sheet

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
    # "United States",
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
    "alignerr",
    "Jobs Ai",
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
    "sr ",
    "staff",
    "principal",
    "director",
    "manager",
    "vice president",
    " vp ",
    "head of",
    "lead ",
    " lead",
    # "intern",
    # "internship",
    "student",
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


_CLOSED_PHRASES = [
    "no longer accepting applications",
    "this job is no longer available",
    "page not found",
    "job has expired",
    "join linkedin",
    "agree & join",
]

_CLOSED_RE = re.compile("|".join(re.escape(p) for p in _CLOSED_PHRASES), re.IGNORECASE)


def _first_element_text(page, selectors: list) -> str:
    for sel in selectors:
        el = page.locator(sel).first
        if el.count() > 0:
            return el.inner_text()
    return ""


def fetch_description(page, url: str) -> str:
    """Returns description text, or None if the job is closed/expired."""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(2)

        if _CLOSED_RE.search(page.content()):
            return None

        text = _first_element_text(page, [
            # logged-in selectors
            "div.jobs-description-content__text",
            "div.jobs-description__content",
            "article.jobs-description__container",
            # logged-out / public selectors
            "div.description__text",
            "div.show-more-less-html__markup",
            "div#job-details",
        ])
        criteria = _first_element_text(page, [
            "div.job-criteria-list",
            "section.job-criteria",
            "ul.description__job-criteria-list",
            "div[data-testid='job-posting-criteria']",
        ])
        if not text:
            text = _first_element_text(page, ["div.core-rail", "main"])
        return "\n".join(filter(None, [text, criteria]))
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


def scroll_page(page, rounds: int = 8, pause: float = 1.5):
    """Scroll the job list panel to trigger lazy loading."""
    # Find the scrollable parent of the first job card via JS (most robust)
    scrolled_via_js = page.evaluate("""() => {
        const card = document.querySelector('div.job-card-container, div.base-card');
        if (!card) return false;
        let el = card.parentElement;
        while (el && el !== document.body) {
            if (el.scrollHeight > el.clientHeight + 10) {
                el.scrollBy(0, 99999);
                return true;
            }
            el = el.parentElement;
        }
        return false;
    }""")

    if not scrolled_via_js:
        # Fallback: move mouse over left panel (~350px from left) then wheel-scroll
        try:
            page.mouse.move(350, 400)
        except Exception:
            pass
        for _ in range(rounds):
            try:
                page.mouse.wheel(0, 3000)
                time.sleep(pause)
            except Exception:
                break
        return

    # After JS scroll-to-bottom, wait for lazy cards to load
    time.sleep(3)


def _locator_text(el, selectors: list) -> str:
    for sel in selectors:
        loc = el.locator(sel)
        if loc.count() > 0:
            return loc.first.inner_text().strip()
    return ""


def _locator_attr(el, selectors: list, attr: str) -> str:
    for sel in selectors:
        loc = el.locator(sel)
        if loc.count() > 0:
            return loc.first.get_attribute(attr) or ""
    return ""


def extract_jobs_from_page(page, keyword: str, location: str):
    jobs = []

    # Logged-in DOM uses div.job-card-container; logged-out uses div.base-card
    cards = page.locator("div.job-card-container").all()
    logged_in = bool(cards)
    if not cards:
        cards = page.locator("div.base-card").all()

    for card in cards:
        try:
            if logged_in:
                raw_title = _locator_text(card, [
                    "a.job-card-list__title--link",
                    "span.job-card-list__title--link",
                ])
                # LinkedIn appends "\n<title> with verification" — keep only first line
                title = raw_title.split("\n")[0].strip()

                company = _locator_text(card, [
                    "div.artdeco-entity-lockup__subtitle span",
                    "span.job-card-container__primary-description",
                    "div.job-card-container__primary-description",
                ])

                job_location = _locator_text(card, [
                    "li.job-card-container__metadata-item",
                    "div.job-card-container__metadata-wrapper li",
                    "div.artdeco-entity-lockup__caption",
                    "span.job-card-container__bullet",
                    "span[class*='bullet']",
                ])
                # Fallback: parse location from card text (line after company name)
                if not job_location and company:
                    lines = [l.strip() for l in card.inner_text().split("\n") if l.strip()]
                    try:
                        idx = next(i for i, l in enumerate(lines) if company in l)
                        if idx + 1 < len(lines):
                            candidate = lines[idx + 1]
                            # Rough check: location lines contain commas or known keywords
                            if any(k in candidate for k in [",", "Remote", "Canada", "United States", "Ontario", "BC", "Toronto", "Vancouver", "Montreal"]):
                                job_location = candidate
                    except StopIteration:
                        pass

                url = _locator_attr(card, [
                    "a.job-card-list__title--link",
                    "a.job-card-container__link",
                ], "href")
                posted = _locator_attr(card, ["time"], "datetime") or _locator_text(card, ["time"])
            else:
                title = _locator_text(card, ["h3"])
                company = _locator_text(card, ["h4"])
                job_location = _locator_text(card, [".job-search-card__location"])
                url = _locator_attr(card, ["a"], "href")
                posted = _locator_attr(card, ["time"], "datetime") or _locator_text(card, ["time"])

            # Normalise URL: strip query params, ensure absolute
            if url:
                url = url.split("?")[0]
                if url.startswith("/"):
                    url = "https://www.linkedin.com" + url

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


def linkedin_login(page) -> bool:
    """Log in to LinkedIn. Returns True on success."""
    email = os.getenv("LINKEDIN_EMAIL")
    password = os.getenv("LINKEDIN_PASSWORD")
    if not email or not password:
        print("No LinkedIn credentials found in .env — skipping login.")
        return False
    print(f"Logging in to LinkedIn as {email[:4]}***")

    page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded", timeout=60000)
    time.sleep(3)

    # Fill email — LinkedIn uses React auto-IDs; try each nth match until one is actionable
    email_filled = False
    for sel in ["input[autocomplete='username']", "input[type='email']"]:
        for nth in range(3):
            try:
                page.locator(sel).nth(nth).fill(email, timeout=2000)
                print(f"  Email filled via {sel}[{nth}]")
                email_filled = True
                break
            except Exception:
                continue
        if email_filled:
            break
    if not email_filled:
        print(f"  ! Could not fill email field. Inputs on page: {_debug_inputs(page)}")
        return False

    time.sleep(0.5)

    # Fill password — track the working locator so we can press Enter on it
    pwd_locator = None
    for sel in ["input[autocomplete='current-password']", "input[type='password']"]:
        for nth in range(3):
            try:
                loc = page.locator(sel).nth(nth)
                loc.fill(password, timeout=2000)
                print(f"  Password filled via {sel}[{nth}]")
                pwd_locator = loc
                break
            except Exception:
                continue
        if pwd_locator:
            break
    if not pwd_locator:
        print("  ! Could not fill password field")
        return False

    time.sleep(0.5)
    submitted = False
    for sel in [
        "button[type='submit']",
        "button[data-litms-control-urn='login-submit']",
        "button.btn__primary--large",
        "[aria-label='Sign in']",
    ]:
        try:
            page.click(sel, timeout=4000)
            submitted = True
            break
        except Exception:
            continue
    if not submitted:
        pwd_locator.press("Enter")

    try:
        page.wait_for_url(lambda u: "linkedin.com/login" not in u, timeout=15000)
    except Exception:
        print("  ! Still on login page after submit — check credentials")
        return False

    if any(k in page.url for k in ["checkpoint", "challenge", "captcha", "verify", "two-step"]):
        print("LinkedIn security check — complete it in the browser, then press Enter.")
        input()

    print(f"Logged in. Current page: {page.url[:60]}")
    return True


def _debug_inputs(page) -> str:
    try:
        return str(page.evaluate(
            "() => Array.from(document.querySelectorAll('input'))"
            ".map(i => i.id + '|' + i.name + '|' + i.type)"
        ))
    except Exception:
        return "unavailable"


def main():
    all_jobs = []
    state_file = tempfile.mktemp(suffix=".json")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()

        linkedin_login(page)
        context.storage_state(path=state_file)

        for keyword in KEYWORDS:
            for location in LOCATIONS:
                for start in [0, 25, 50]:
                    url = build_linkedin_search_url(keyword, location, start)
                    print(f"Fetching: {url}")
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=60000)
                        time.sleep(3)
                        scroll_page(page)
                        jobs = extract_jobs_from_page(page, keyword, location)
                        print(f"  got {len(jobs)} jobs")
                        all_jobs.extend(jobs)
                        time.sleep(4)
                    except Exception as e:
                        print(f"  failed: {e}")
                        continue

        context.storage_state(path=state_file)
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

    # Fetch descriptions using saved login session
    print(f"Fetching descriptions for {len(df)} jobs to check experience requirements...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=state_file)
        page = context.new_page()
        descriptions = []
        for i, row in df.iterrows():
            desc = fetch_description(page, row["url"]) if row.get("url") else ""
            descriptions.append(desc)
            if (i + 1) % 10 == 0:
                print(f"  {i + 1}/{len(df)} descriptions fetched")
        browser.close()

    try:
        os.remove(state_file)
    except OSError:
        pass

    df["description"] = descriptions

    before = len(df)
    df = df[df["description"].notna() & ~df["description"].str.contains(_CLOSED_RE.pattern, case=False, na=False, regex=True)].reset_index(drop=True)
    print(f"Filtered to {len(df)} jobs after removing closed/expired/login-wall (dropped {before - len(df)})")

    before = len(df)
    df = df[df.apply(lambda r: is_entry_level_description(r["description"], r["title"]), axis=1)].reset_index(drop=True)
    print(f"Filtered to {len(df)} jobs after experience/PhD check (dropped {before - len(df)})")

    df["job_key"] = [_make_job_key(r["title"], r["company"], r["location"]) for _, r in df.iterrows()]

    if _USE_SHEETS:
        write_sheet("linkedin_jobs", df)
        print(f"Saved {len(df)} jobs to Sheets:linkedin_jobs")
    else:
        df.to_csv("linkedin_jobs.csv", index=False, encoding="utf-8-sig")
        print(f"Saved {len(df)} jobs to linkedin_jobs.csv")


if __name__ == "__main__":
    main()
