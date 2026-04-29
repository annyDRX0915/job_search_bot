"""
Job application automation using Playwright.
Handles: Greenhouse, Lever, LinkedIn Easy Apply, generic fallback.
Logs results to apply/applied_log.csv.
"""

import argparse
import csv
import json
import os
import re
import time
import yaml
from datetime import datetime
from pathlib import Path

from openai import OpenAI
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, Page

load_dotenv()

BASE_DIR = Path(__file__).parent.parent
PROFILE_PATH = BASE_DIR / "apply" / "profile.yaml"
LOG_PATH = BASE_DIR / "apply" / "applied_log.csv"
RANKED_CSV = BASE_DIR / "ranked_jobs.csv"

def _read_past_exp() -> str:
    try:
        return (BASE_DIR / "resume" / "past_exp.txt").read_text(encoding="utf-8")
    except Exception:
        return ""

PAST_EXPERIENCE = _read_past_exp()
LOG_FIELDS = ["url", "company", "title", "source", "status", "timestamp", "notes"]

APPLY_BTNS = [
    "a.btn-apply", "a[href*='#app']",
    "a:has-text('Apply for this Job')", "a:has-text('Apply for this job')",
    "a:has-text('Apply Now')", "a:has-text('Apply')",
    "button:has-text('Apply for this Job')", "button:has-text('Apply for this job')",
    "button:has-text('Apply Now')", "button:has-text('Apply')",
    "[data-mapped='true'] a",
]


# ── I/O ───────────────────────────────────────────────────────────────────────

def load_profile() -> dict:
    with open(PROFILE_PATH) as f:
        return yaml.safe_load(f)

def load_jobs() -> list[dict]:
    if not RANKED_CSV.exists():
        raise SystemExit(f"{RANKED_CSV} not found — run `python rank/ranker.py` first.")
    with open(RANKED_CSV, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))

def _norm_url(url: str) -> str:
    return url.strip().rstrip("/").lower().split("?")[0]

def load_applied() -> tuple[set, set]:
    """Return (applied_urls, applied_company_titles) from the log."""
    if not LOG_PATH.exists():
        return set(), set()
    urls, company_titles = set(), set()
    with open(LOG_PATH, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("url"):
                urls.add(_norm_url(row["url"]))
            c = (row.get("company") or "").strip().lower()
            t = (row.get("title") or "").strip().lower()
            if c and t:
                company_titles.add((c, t))
    return urls, company_titles

def log_result(url, company, title, source, status, notes=""):
    write_header = not LOG_PATH.exists()
    with open(LOG_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        if write_header:
            writer.writeheader()
        writer.writerow({"url": url, "company": company, "title": title,
                         "source": source, "status": status,
                         "timestamp": datetime.now().isoformat(), "notes": notes})


# ── Low-level Playwright helpers ──────────────────────────────────────────────

def try_fill(frame, selectors: list[str], value: str, timeout: int = 1000) -> bool:
    for sel in selectors:
        try:
            el = frame.locator(sel).first
            if el.count() > 0:
                el.wait_for(state="visible", timeout=timeout)
                el.fill(str(value))
                return True
        except Exception:
            continue
    return False

def try_upload(frame, selectors: list[str], path: str, timeout: int = 1000) -> bool:
    for sel in selectors:
        try:
            el = frame.locator(sel).first
            if el.count() > 0:
                el.wait_for(timeout=timeout)
                el.set_input_files(path)
                return True
        except Exception:
            continue
    return False

def try_click(frame, selectors: list[str], timeout: int = 1000) -> bool:
    for sel in selectors:
        try:
            el = frame.locator(sel).first
            if el.count() > 0:
                el.wait_for(state="visible", timeout=timeout)
                el.click()
                return True
        except Exception:
            continue
    return False

def wait_for_form(page, selectors: list[str], timeout: int = 3000):
    for sel in selectors:
        try:
            page.wait_for_selector(sel, timeout=timeout)
            return
        except Exception:
            continue

def get_fill_target(page: Page):
    """Return a Greenhouse iframe if present, else the main page."""
    try:
        for frame in page.frames:
            if "greenhouse.io" in (frame.url or "") or "job-boards" in (frame.url or ""):
                return frame
    except Exception:
        pass
    return page

def is_greenhouse_page(page: Page) -> bool:
    try:
        c = page.content().lower()
        return "greenhouse" in c or "mygreenhouse" in c
    except Exception:
        return False

def click_apply_or_prompt(page: Page) -> bool:
    """Click the Apply button; if not found, continue silently (form may already be open)."""
    clicked = try_click(page, APPLY_BTNS, timeout=300)
    if not clicked:
        print("  ! Apply button not found — proceeding to fill form directly.")
    return clicked

def multi_page_fill_loop(
    page: Page, job: dict, profile: dict, resume: str, cl: str,
    first_page_label: str = ""
) -> tuple[str, str]:
    """
    After the first page is already filled, loop so the AI fills each
    subsequent page as the user navigates forward.
    """
    page_num = 1
    if first_page_label:
        print(f"  Pre-filled: {first_page_label}")

    while True:
        print(f"\n  [{job.get('company','?')} — {job.get('title','?')}] Page {page_num} ready.")
        print("  Enter = I submitted | n = I clicked Next (AI fills next page) | s = skip | q = quit: ", end="", flush=True)
        try:
            choice = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            choice = "q"

        if choice == "q":
            raise SystemExit("User quit.")
        if choice == "s":
            return "skipped", "skipped by user"
        if choice == "":
            return "applied", "submitted by user"
        if choice == "n":
            page_num += 1
            print(f"  Waiting for page {page_num}…")
            time.sleep(2)
            frame = get_fill_target(page)
            # Retry standard fields (address/resume may appear on later pages)
            fill_standard_fields(frame, profile, resume, cl)
            fill_country(frame, profile["personal"].get("country", "Canada"))
            handle_common_questions(frame, profile, job)
            print(f"  Page {page_num} filled.")


# ── Standard field filling (shared across all handlers) ───────────────────────

def fill_standard_fields(frame, profile: dict, resume: str, cl: str):
    """Fill name/email/phone/resume/cover/linkedin/github on any ATS form."""
    p = profile["personal"]
    full_name = f"{p['first_name']} {p['last_name']}"

    try_fill(frame, [
        "#first_name", "input[name='first_name']", "input[autocomplete='given-name']",
        "input[aria-label*='First']", "input[placeholder*='First name']",
        "input[placeholder*='First Name']", "input[name*='first']", "input[id*='first']",
    ], p["first_name"])
    try_fill(frame, [
        "#last_name", "input[name='last_name']", "input[autocomplete='family-name']",
        "input[aria-label*='Last']", "input[placeholder*='Last name']",
        "input[placeholder*='Last Name']", "input[name*='last']", "input[id*='last']",
    ], p["last_name"])
    try_fill(frame, ["input[name='name']", "input[placeholder*='Full Name']"], full_name)
    try_fill(frame, [
        "#email", "input[name='email']", "input[type='email']",
        "input[autocomplete='email']", "input[name*='email']", "input[id*='email']",
    ], p["email"])
    try_fill(frame, [
        "#phone", "input[name='phone']", "input[type='tel']", "input[autocomplete='tel']",
        "input[aria-label*='Phone']", "input[name*='phone']", "input[id*='phone']",
    ], p["phone"])
    try_upload(frame, [
        "input#resume", "input[type='file'][id*='resume']",
        "input[type='file'][name*='resume']", "input[type='file']",
    ], resume)
    try_fill(frame, [
        "#cover_letter", "textarea[name='cover_letter']", "textarea[name='comments']",
        "textarea[placeholder*='cover']", "textarea[id*='cover']",
    ], cl)
    try_fill(frame, [
        "input[id*='linkedin']", "input[name*='linkedin']",
        "input[placeholder*='LinkedIn']", "input[name='urls[LinkedIn]']",
    ], p["linkedin_url"])
    try_fill(frame, [
        "input[id*='website']", "input[name*='website']", "input[id*='github']",
        "input[name*='github']", "input[placeholder*='GitHub']",
        "input[name='urls[GitHub]']", "input[placeholder*='Website']",
    ], p.get("github_url", ""))
    if p.get("portfolio_url"):
        try_fill(frame, ["input[name='urls[Portfolio]']", "input[placeholder*='Portfolio']"],
                 p["portfolio_url"])
    if p.get("street_address"):
        try_fill(frame, [
            "input[name='address']", "input[name*='street']", "input[id*='street']",
            "input[autocomplete='street-address']", "input[placeholder*='Street']",
            "input[placeholder*='Address']", "input[aria-label*='Address']",
            "input[id*='address']", "input[name*='address']",
        ], p["street_address"])
    try_fill(frame, [
        "input[name='city']", "input[id*='city']", "input[name*='city']",
        "input[autocomplete='address-level2']",
        "input[aria-label*='City']", "input[placeholder*='City']",
    ], p.get("city", ""))
    if p.get("postal_code"):
        try_fill(frame, [
            "input[name='zip']", "input[name*='zip']", "input[id*='zip']",
            "input[name*='postal']", "input[id*='postal']",
            "input[autocomplete='postal-code']",
            "input[aria-label*='Postal']", "input[aria-label*='ZIP']",
            "input[placeholder*='Postal']", "input[placeholder*='ZIP']",
        ], p["postal_code"])
    # Province/state — try select first, then text input
    province = p.get("province_state", "")
    if province:
        try:
            sel = frame.locator(
                "select[name*='state'], select[name*='province'], select[id*='state'], "
                "select[id*='province'], select[autocomplete='address-level1']"
            ).first
            if sel.count() > 0:
                sel.wait_for(state="visible", timeout=2000)
                opts = sel.evaluate("el => Array.from(el.options).map(o => ({v: o.value, t: o.text}))")
                match = next((o["v"] for o in opts if province.lower() in o["t"].lower()
                              or o["t"].lower() in province.lower()), None)
                if match:
                    sel.select_option(match)
        except Exception:
            pass
        try_fill(frame, [
            "input[name*='state']", "input[id*='state']",
            "input[name*='province']", "input[id*='province']",
            "input[autocomplete='address-level1']",
            "input[aria-label*='State']", "input[aria-label*='Province']",
            "input[placeholder*='State']", "input[placeholder*='Province']",
        ], province)


def fill_country(frame, country: str):
    try:
        sel = frame.locator(
            "select[name='country'], select[id*='country'], select[aria-label*='Country']"
        ).first
        if sel.count() > 0:
            sel.wait_for(state="visible", timeout=2000)
            opts = sel.evaluate("el => Array.from(el.options).map(o => ({v: o.value, t: o.text}))")
            match = next((o["v"] for o in opts if country.lower() in o["t"].lower()), None)
            if match:
                sel.select_option(match)
                time.sleep(0.5)
    except Exception:
        pass


# ── AI + rule-based question answering ───────────────────────────────────────

# Labels that are page UI elements (nav, search bars, etc.), not application questions
_JUNK_LABEL_FRAGMENTS = [
    "search by title",
    "describe the job you want",
    "search jobs",
    "job title, keywords",
    "city, state, or zip",
    "search for jobs",
]

def _is_junk_label(label: str) -> bool:
    l = label.lower()
    return any(frag in l for frag in _JUNK_LABEL_FRAGMENTS)

_LABEL_JS = """(el => {
    const id = el.id;
    const explicit = id ? document.querySelector(`label[for='${id}']`) : null;
    if (explicit) return explicit.innerText.trim();
    const closest = el.closest('label');
    if (closest) return closest.innerText.trim();
    const field = el.closest('.field, [data-field], .application-question, .custom-field');
    if (field) {
        const lbl = field.querySelector('label, legend, .field-label');
        if (lbl) return lbl.innerText.trim();
    }
    return '';
})(el)"""


def _pick(options: list[str], keyword: str) -> str | None:
    return next((o for o in options if keyword.lower() in o.lower()), None)


def _rule_answer(label: str, options: list[str], profile: dict) -> str | None:
    l = label.lower()
    wa = profile["work_authorization"]
    ans = profile["answers"]
    p = profile["personal"]

    if any(k in l for k in ["authorized to work", "legally work", "eligible to work", "work authorization", "work permit"]):
        yn = "Yes" if (wa.get("canada") and "canada" in l) or (wa.get("usa") and ("us" in l or "united states" in l)) else "No"
        return _pick(options, yn) or yn
    if "sponsor" in l:
        return _pick(options, "No") or "No"
    if any(k in l for k in ["worked here", "worked for", "previously employed", "former employee"]):
        return _pick(options, "No") or "No"
    if any(k in l for k in ["convicted", "criminal", "pardon", "offense", "felony"]):
        return _pick(options, "No") or "No"
    if any(k in l for k in ["how did you hear", "how did you find", "where did you hear", "referral source"]):
        return _pick(options, "linkedin") or _pick(options, "online") or (options[0] if options else "LinkedIn")
    if "year" in l and "experience" in l and not options:
        return str(ans["years_of_experience"])
    if any(k in l for k in ["education", "degree", "highest"]):
        return _pick(options, "master") or _pick(options, "graduate")
    if "pronoun" in l:
        return _pick(options, "prefer not") or _pick(options, "decline") or (options[-1] if options else None)
    if "salary" in l or "compensation" in l:
        currency = "cad" if ("cad" in l or "canada" in l) else "usd"
        return str(ans.get(f"salary_expectation_{currency}", ans.get("salary_expectation_usd", 100000)))
    if "remote" in l or "work type" in l or "work arrangement" in l:
        return _pick(options, ans.get("preferred_work_type", "hybrid"))
    if "relocat" in l:
        return _pick(options, "Yes" if ans.get("willing_to_relocate") else "No")
    if "linkedin" in l and not options:
        return p["linkedin_url"]
    if any(k in l for k in ["website", "github", "portfolio"]) and not options:
        return p.get("github_url", "") or p.get("portfolio_url", "")
    return None


_USE_AI = True  # set to False via --no-ai flag


def _ai_answers(unanswered: list[dict], profile: dict, job: dict) -> dict[str, str]:
    if not unanswered:
        return {}
    if not _USE_AI:
        print("  ! AI disabled — skipping AI answers.")
        return {}
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("  ! OPENAI_API_KEY not set — skipping AI answers.")
        print("    Check that your .env file has OPENAI_API_KEY=sk-...")
        return {}
    print(f"  [AI] Sending {len(unanswered)} question(s) to OpenAI:")
    for q in unanswered:
        print(f"       • {q['label']!r}  type={q['type']}  options={q['options']}")
    try:
        p = profile["personal"]
        summary = (
            f"Name: {p['first_name']} {p['last_name']}, Email: {p['email']}\n"
            f"Location: {p.get('city')}, {p.get('province_state')}, {p.get('country')}\n"
            f"Work auth: Canada={profile['work_authorization']['canada']}, "
            f"USA={profile['work_authorization']['usa']}, "
            f"Sponsorship needed={profile['work_authorization']['requires_sponsorship']}\n"
            f"Experience: {profile['answers']['years_of_experience']} years, "
            f"Degree: {profile['answers']['highest_degree']} in {profile['answers']['field_of_study']}\n"
            f"LinkedIn: {p['linkedin_url']}, GitHub: {p.get('github_url','')}\n"
        )
        exp_section = f"\nApplicant's past experience:\n{PAST_EXPERIENCE[:1500]}\n" if PAST_EXPERIENCE else ""
        prompt = (
            f'Fill out a job application for "{job.get("title","")}" at "{job.get("company","")}".\n'
            f"Applicant:\n{summary}{exp_section}\n"
            "Answer ONLY the questions below. If options are given, answer with one of those exact strings.\n"
            "Free-text answers should be concise (1-3 sentences) and draw on the applicant's real experience above.\n"
            "Reply ONLY with a JSON object {label: answer}.\n\n"
            f"Questions:\n{json.dumps([{'label':' '.join(q['label'].split()),'type':q['type'],'options':q['options']} for q in unanswered], indent=2)}"
        )
        resp = OpenAI(api_key=api_key).chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=1500,
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}],
        )
        result = json.loads(resp.choices[0].message.content)
        print(f"  [AI] Response: {result}")
        return result
    except Exception as e:
        print(f"  ! AI answer failed: {type(e).__name__}: {e}")
        return {}


def _apply_answer(frame, q: dict, answer: str):
    label, qtype = q["label"], q["type"]

    # Native select by id
    if qtype == "select" and q.get("id"):
        try:
            sel = frame.locator(f"#{q['id']}").first
            if sel.count() > 0:
                opts = sel.evaluate("el => Array.from(el.options).map(o => ({v: o.value, t: o.text}))")
                m = next((o["v"] for o in opts if answer.lower() in o["t"].lower()), None)
                if m:
                    sel.select_option(m)
                    return
        except Exception:
            pass

    # Any native select matching by label
    try:
        for sel in frame.locator("select").all():
            lbl = sel.evaluate(_LABEL_JS).lower()
            if label.lower()[:30] in lbl or lbl in label.lower():
                opts = sel.evaluate("el => Array.from(el.options).map(o => ({v: o.value, t: o.text}))")
                m = next((o["v"] for o in opts if answer.lower() in o["t"].lower()), None)
                if m:
                    sel.select_option(m)
                    return
    except Exception:
        pass

    # Custom combobox — click to open, pick option
    try:
        for lbl_el in frame.locator("label, legend, .field-label").all():
            if label.lower()[:30] in (lbl_el.text_content() or "").strip().lower():
                trigger = frame.locator(
                    "[role='combobox'], [role='button'][aria-haspopup], .select__control, .select-wrapper > div"
                ).first
                if trigger.count() > 0:
                    trigger.click()
                    time.sleep(0.5)
                    for opt in frame.locator("[role='option'], .select__option, ul li, [class*='option']").all():
                        if answer.lower() in (opt.text_content() or "").lower():
                            opt.click()
                            return
    except Exception:
        pass

    # Radio group — match by group name attr or by scanning all radios for the right option
    if qtype == "radio":
        try:
            name = q.get("name")
            radios = (frame.locator(f"input[type='radio'][name='{name}']").all()
                      if name else frame.locator("input[type='radio']").all())
            for radio in radios:
                lbl = radio.evaluate(_LABEL_JS).strip()
                val = (radio.get_attribute("value") or "").strip()
                if answer.lower() in lbl.lower() or answer.lower() in val.lower():
                    radio.check()
                    return
        except Exception:
            pass
        return

    # Radio fallback for non-radio qtype (legacy path)
    try:
        for radio in frame.locator("input[type='radio']").all():
            lbl = radio.evaluate(_LABEL_JS).lower()
            val = (radio.get_attribute("value") or "").lower()
            if label.lower()[:30] in lbl and (answer.lower() in val or answer.lower() in lbl):
                radio.check()
                return
    except Exception:
        pass

    # Text / textarea
    if qtype in ("text", "textarea"):
        try:
            inp = frame.locator(f"#{q['id']}").first if q.get("id") else next(
                (i for i in frame.locator("input[type='text'], input:not([type]), textarea").all()
                 if label.lower()[:20] in i.evaluate(_LABEL_JS).lower()), None
            )
            if inp and inp.count() > 0:
                inp.fill(answer)
        except Exception:
            pass


def handle_common_questions(frame, profile: dict, job: dict = None):
    """Extract all form questions, fill rule-based, ask AI for the rest."""
    job = job or {}
    try:
        questions = frame.evaluate("""() => {
            const results = [], seen = new Set();
            const SKIP_TYPES = new Set(['hidden','submit','button','file','image','reset','checkbox']);
            const SKIP_NAMES = new Set(['first_name','last_name','email','phone','name',
                                        'resume','cover_letter','linkedin','github','website',
                                        'city','zip','postal','address','state','province','country']);

            function normLabel(t) {
                return t.replace(/\\s+/g, ' ').replace(/[*\\u00a0]+/g, '').trim();
            }
            function isSkippedName(el) {
                const n = (el.name || el.id || '').toLowerCase();
                return Array.from(SKIP_NAMES).some(k => n.includes(k));
            }
            function classifyCtrl(ctrl) {
                if (!ctrl) return null;
                if (ctrl.tagName === 'SELECT') {
                    if (ctrl.value) return null; // already answered
                    const opts = Array.from(ctrl.options).map(o => o.text.trim())
                        .filter(t => t && !/^(select|choose|--|please)/i.test(t));
                    return {type: 'select', options: opts, id: ctrl.id || null, name: null};
                }
                if (ctrl.tagName === 'TEXTAREA') {
                    if (ctrl.value) return null;
                    return {type: 'textarea', options: [], id: ctrl.id || null, name: null};
                }
                if (ctrl.tagName === 'INPUT') {
                    if (SKIP_TYPES.has(ctrl.type) || ctrl.value) return null;
                    if (isSkippedName(ctrl)) return null;
                    return {type: 'text', options: [], id: ctrl.id || null, name: null};
                }
                return null;
            }

            // ── Pass 1: label[for] → control ─────────────────────────────────
            document.querySelectorAll('label[for]').forEach(lbl => {
                const text = normLabel(lbl.innerText);
                if (!text || seen.has(text)) return;
                const ctrl = document.getElementById(lbl.htmlFor);
                if (!ctrl) return;
                if (isSkippedName(ctrl)) return;
                const info = classifyCtrl(ctrl);
                if (!info) return;
                seen.add(text);
                results.push({label: text, ...info});
            });

            // ── Pass 2: fieldset/legend → radio group ────────────────────────
            document.querySelectorAll('fieldset').forEach(fs => {
                const legend = fs.querySelector('legend');
                if (!legend) return;
                const text = normLabel(legend.innerText);
                if (!text || seen.has(text)) return;
                if (fs.querySelector('input[type="radio"]:checked')) return;
                const radios = fs.querySelectorAll('input[type="radio"]');
                if (!radios.length) return;
                seen.add(text);
                const opts = Array.from(radios).map(r => {
                    const lbl = r.id ? document.querySelector('label[for="' + r.id + '"]') : null;
                    if (lbl) return lbl.innerText.trim();
                    const p = r.closest('label');
                    return p ? normLabel(p.innerText) : r.value;
                }).filter(Boolean);
                results.push({label: text, type: 'radio', options: opts,
                              id: null, name: radios[0].name || null});
            });

            // ── Pass 3: container fallback for unlabeled custom dropdowns ─────
            const containers = [
                '.custom-question', '.application-question', '[data-field]',
                '.lever-question', '.application-field', '.s-form-field',
                '[class*="custom_field"]', '[class*="customField"]'
            ].join(', ');
            document.querySelectorAll(containers).forEach(c => {
                const lbl = c.querySelector('label, legend, .field-label');
                if (!lbl) return;
                const text = normLabel(lbl.innerText);
                if (!text || seen.has(text)) return;

                // Custom combobox (no native select available)
                const hasCombo = c.querySelector('[role="combobox"], [aria-haspopup="listbox"]');
                const hasSel   = c.querySelector('select');
                if (hasCombo && !hasSel) {
                    seen.add(text);
                    results.push({label: text, type: 'custom_select', options: [], id: null, name: null});
                    return;
                }
                // Radio group not inside a fieldset
                const radios = c.querySelectorAll('input[type="radio"]');
                if (radios.length && !c.querySelector('input[type="radio"]:checked')) {
                    seen.add(text);
                    const opts = Array.from(radios).map(r => {
                        const el = r.id ? document.querySelector('label[for="' + r.id + '"]') : null;
                        return el ? el.innerText.trim() : r.value;
                    }).filter(Boolean);
                    results.push({label: text, type: 'radio', options: opts,
                                  id: null, name: radios[0].name || null});
                }
            });

            return results;
        }""")
    except Exception as e:
        print(f"  ! Could not extract questions: {e}")
        return

    unanswered = []
    for q in (questions or []):
        if _is_junk_label(q["label"]):
            continue
        answer = _rule_answer(q["label"], q["options"], profile)
        if answer is not None:
            _apply_answer(frame, q, answer)
            time.sleep(0.2)
        else:
            unanswered.append(q)

    # Only send free-text questions to AI — selects/radios must be handled by rules
    TEXT_TYPES = {"text", "textarea"}
    ai_questions = [q for q in unanswered if q["type"] in TEXT_TYPES]
    skipped = [q["label"] for q in unanswered if q["type"] not in TEXT_TYPES]
    if skipped:
        print(f"  → Skipping AI for {len(skipped)} select/radio question(s): {skipped}")

    if ai_questions:
        print(f"  → Asking AI to answer {len(ai_questions)} text question(s)…")
        ai = _ai_answers(ai_questions, profile, job)
        # Normalize AI response keys to handle whitespace/newline differences
        ai_norm = {" ".join(k.split()): v for k, v in ai.items()}
        for q in ai_questions:
            norm_label = " ".join(q["label"].split())
            answer = ai_norm.get(norm_label) or ai_norm.get(norm_label.rstrip("*").strip())
            if answer:
                _apply_answer(frame, q, answer)
                time.sleep(0.2)


# ── ATS Handlers ──────────────────────────────────────────────────────────────

def detect_ats(url: str) -> str:
    if not url:
        return "unknown"
    u = url.lower()
    if "greenhouse.io" in u or "gh_jid=" in u:
        return "greenhouse"
    if "lever.co" in u:
        return "lever"
    if "linkedin.com" in u:
        return "linkedin"
    if "workday.com" in u or "myworkdayjobs.com" in u:
        return "workday"
    return "generic"

def resolve_resume(profile: dict, job_title: str) -> str:
    title = (job_title or "").lower()
    if any(kw in title for kw in ["agent", "agentic", "ai engineer", "ml engineer", "machine learning", "nlp", "llm"]):
        path = BASE_DIR / "resume/Resume_Agentic_AI/resume.pdf"
    elif any(kw in title for kw in ["data scientist", "data science"]):
        path = BASE_DIR / "resume/Resume_Data_Science/resume.pdf"
    elif any(kw in title for kw in ["model", "modeling"]):
        path = BASE_DIR / "resume/Resume_Modeling/resume.pdf"
    elif any(kw in title for kw in ["software engineer", "software developer", "swe", "backend", "frontend", "full stack", "fullstack"]):
        path = BASE_DIR / "resume/Resume_Software_Engineer/resume.pdf"
    else:
        path = BASE_DIR / profile["resume_path"]
    print(path)
    return str(path) if path.exists() else str(BASE_DIR / profile["resume_path"])

def cover_letter_text(profile: dict, title: str, company: str) -> str:
    return profile["cover_letter"].format(
        title=title or "this position", company=company or "your company"
    )


def apply_greenhouse(page: Page, job: dict, profile: dict) -> tuple[str, str]:
    url = job["url"]
    if "gh_jid=" in url and "greenhouse.io" not in url:
        from urllib.parse import urlparse, parse_qs
        jid = parse_qs(urlparse(url).query).get("gh_jid", [None])[0]
        slug = job.get("company", "").lower().replace(" ", "")
        if jid:
            url = f"https://job-boards.greenhouse.io/{slug}/jobs/{jid}"
    resume = resolve_resume(profile, job.get("title", ""))
    cl = cover_letter_text(profile, job.get("title", ""), job.get("company", ""))
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(2)
        clicked = click_apply_or_prompt(page)
        if clicked:
            wait_for_form(page, ["#first_name", "input[name='first_name']",
                                  "input[autocomplete='given-name']", "input[type='file']"], timeout=5000)
        time.sleep(1)
        frame = get_fill_target(page)
        fill_standard_fields(frame, profile, resume, cl)
        fill_country(frame, profile["personal"].get("country", "Canada"))
        handle_common_questions(frame, profile, job)
        time.sleep(1)
        return multi_page_fill_loop(page, job, profile, resume, cl,
                                    "name, email, phone, address, city, province, postal, country, resume, cover_letter, linkedin, github")
    except Exception as e:
        return "failed", str(e)[:200]


def apply_lever(page: Page, job: dict, profile: dict) -> tuple[str, str]:
    url = job["url"].rstrip("/")
    if not url.endswith("/apply"):
        url += "/apply"
    resume = resolve_resume(profile, job.get("title", ""))
    cl = cover_letter_text(profile, job.get("title", ""), job.get("company", ""))
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        wait_for_form(page, ["input[name='name']", "input[type='email']", "input[type='file']"], timeout=5000)
        time.sleep(1)
        fill_standard_fields(page, profile, resume, cl)
        handle_common_questions(page, profile, job)
        time.sleep(1)
        return multi_page_fill_loop(page, job, profile, resume, cl,
                                    "name, email, phone, resume, cover_letter, linkedin, github")
    except Exception as e:
        return "failed", str(e)[:200]


def linkedin_login(page: Page):
    email = os.getenv("LINKEDIN_EMAIL")
    password = os.getenv("LINKEDIN_PASSWORD")
    if not email or not password:
        raise RuntimeError("LinkedIn credentials missing from .env")
    page.goto("https://www.linkedin.com/login", wait_until="load", timeout=30000)
    page.wait_for_selector("#username", timeout=30000)
    time.sleep(1)
    page.click("#username")
    page.type("#username", email, delay=60)
    page.wait_for_selector("#password", timeout=10000)
    page.click("#password")
    page.type("#password", password, delay=60)
    time.sleep(0.5)
    page.click("button[type='submit']")
    time.sleep(5)


_LI_EASY_APPLY = (
    "a[aria-label*='Easy Apply'], "
    "button[aria-label*='Easy Apply'], "
    "button:has-text('Easy Apply'), "
    "button.jobs-apply-button, "
    ".jobs-s-apply button, "
    "[data-control-name='jobdetails_topcard_inapply'], "
    "button.artdeco-button--primary:has-text('Apply'), "
    "button:has-text('Apply')"  # broad fallback — buttons only, never <a> tags
)


def _clean_linkedin_url(url: str) -> str:
    """Use canonical LinkedIn job URL (numeric ID only) to avoid slug/subdomain redirects."""
    match = re.search(r'/jobs/view/\D*?(\d+)', url)
    if match:
        return f"https://www.linkedin.com/jobs/view/{match.group(1)}/"
    # Fallback: strip params and normalize subdomain
    url = url.split("?")[0].rstrip("/")
    url = re.sub(r"https://[a-z]{2}\.linkedin\.com", "https://www.linkedin.com", url)
    return url


def apply_linkedin(page: Page, job: dict, profile: dict, _allow_new_tab: bool = True) -> tuple[str, str]:
    try:
        clean_url = _clean_linkedin_url(job["url"])
        page.goto(clean_url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(2)  # let LinkedIn SPA finish any client-side routing

        # Verify we're still on the job page (LinkedIn SPA can redirect us away)
        if "/jobs/view/" not in page.url:
            print(f"  ! Redirected away from job page → {page.url[:80]}")
            return "skipped", f"LinkedIn redirected away: {page.url[:120]}"

        # Check for "No longer accepting applications" before doing anything else
        closed = page.locator("text=No longer accepting applications").first
        if closed.count() > 0:
            return "skipped", "No longer accepting applications"
        # Wait for the apply button to appear rather than a blind sleep
        try:
            page.wait_for_selector(_LI_EASY_APPLY, timeout=5000)
        except Exception:
            pass
        btn = page.locator(_LI_EASY_APPLY).first
        if btn.count() == 0:
            # No Easy Apply — click the external Apply link and let LinkedIn's JS navigate
            ext = page.locator("a:has-text('Apply'), a.jobs-apply-button").first
            if ext.count() > 0:
                print("  → No Easy Apply, clicking external Apply link.")
                opens_new_tab = ext.get_attribute("target") in ("_blank", "_new")
                if opens_new_tab and _allow_new_tab:
                    with page.context.expect_page(timeout=15000) as new_page_info:
                        ext.click()
                    new_page = new_page_info.value
                    new_page.wait_for_load_state("domcontentloaded", timeout=15000)
                    time.sleep(2)
                    print(f"  → Opened new tab: {new_page.url[:80]}")
                    if "linkedin.com/jobs/view/" in new_page.url:
                        result = apply_linkedin(new_page, job, profile, _allow_new_tab=False)
                    else:
                        result = apply_generic(new_page, job, profile, already_navigated=True)
                    new_page.close()
                    return result
                elif opens_new_tab:
                    # Secondary tab context — goto href directly to avoid spawning another tab
                    href = ext.get_attribute("href") or ""
                    if not href:
                        return "skipped", "External apply link has no href"
                    print(f"  → Navigating in-place to: {href[:80]}")
                    page.goto(href, wait_until="domcontentloaded", timeout=30000)
                    time.sleep(2)
                    return apply_generic(page, job, profile, already_navigated=True)
                else:
                    try:
                        with page.expect_navigation(wait_until="domcontentloaded", timeout=15000):
                            ext.click()
                    except Exception:
                        pass  # some redirects don't trigger a full navigation event
                    time.sleep(2)
                    print(f"  → Landed on: {page.url[:80]}")
                    return apply_generic(page, job, profile, already_navigated=True)
            return "skipped", "No Apply button found"
        btn.click()
        time.sleep(2)
        resume = resolve_resume(profile, job.get("title", ""))
        cl = cover_letter_text(profile, job.get("title", ""), job.get("company", ""))
        fill_standard_fields(page, profile, resume, cl)
        fill_country(page, profile["personal"].get("country", "Canada"))
        handle_common_questions(page, profile, job)
        return multi_page_fill_loop(page, job, profile, resume, cl,
                                    "name, email, phone, address, city, province, postal, resume, linkedin")
    except Exception as e:
        return "failed", str(e)[:200]


def apply_generic(page: Page, job: dict, profile: dict, already_navigated: bool = False) -> tuple[str, str]:
    resume = resolve_resume(profile, job.get("title", ""))
    cl = cover_letter_text(profile, job.get("title", ""), job.get("company", ""))
    try:
        if not already_navigated:
            page.goto(job["url"], wait_until="domcontentloaded", timeout=30000)
            time.sleep(2)
        if "linkedin.com/jobs/view/" in page.url:
            return apply_linkedin(page, job, profile, _allow_new_tab=False)
        if is_greenhouse_page(page):
            print("  → Detected Greenhouse form, switching handler.")
            return apply_greenhouse(page, job, profile)
        clicked = click_apply_or_prompt(page)
        if clicked:
            time.sleep(2)
            if is_greenhouse_page(page):
                print("  → Detected Greenhouse form after Apply click, switching handler.")
                return apply_greenhouse(page, job, profile)
            if "linkedin.com" in page.url:
                return apply_linkedin(page, job, profile, _allow_new_tab=False)
            wait_for_form(page, ["input[type='email']", "input[name*='email']",
                                   "input[name*='first']", "form", "input[type='file']"], timeout=5000)
        time.sleep(1)
        frame = get_fill_target(page)
        fill_standard_fields(frame, profile, resume, cl)
        handle_common_questions(frame, profile, job)
        time.sleep(1)
        return multi_page_fill_loop(page, job, profile, resume, cl,
                                    "name, email, phone, resume, cover_letter, linkedin")
    except Exception as e:
        return "failed", str(e)[:200]


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    global _USE_AI
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-ai", action="store_true", help="Disable AI for custom questions")
    args = parser.parse_args()
    if args.no_ai:
        _USE_AI = False
        print("AI disabled — only rule-based answers will be used.")

    profile = load_profile()
    jobs = load_jobs()
    applied_urls, applied_company_titles = load_applied()

    def already_applied(j: dict) -> bool:
        if j.get("url") and _norm_url(j["url"]) in applied_urls:
            return True
        c = (j.get("company") or "").strip().lower()
        t = (j.get("title") or "").strip().lower()
        return bool(c and t and (c, t) in applied_company_titles)

    pending = [j for j in jobs if j.get("url") and not already_applied(j)]
    print(f"Total: {len(jobs)} | Applied: {len(applied_urls)} | Pending: {len(pending)}")
    if not pending:
        print("Nothing to apply to.")
        return

    counts = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        page = browser.new_context().new_page()
        try:
            linkedin_login(page)
            print("LinkedIn login successful.")
        except Exception as e:
            print(f"LinkedIn login failed: {e}")

        for i, job in enumerate(pending):
            url, company, title, source = job.get("url",""), job.get("company",""), job.get("title",""), job.get("source","")
            ats = detect_ats(url)
            print(f"[{i+1}/{len(pending)}] {company} — {title} ({ats})")
            if ats == "greenhouse":
                status, notes = apply_greenhouse(page, job, profile)
            elif ats == "lever":
                status, notes = apply_lever(page, job, profile)
            elif ats == "linkedin":
                status, notes = apply_linkedin(page, job, profile)
            elif ats == "workday":
                status, notes = "skipped", "Workday not supported"
            else:
                status, notes = apply_generic(page, job, profile)
            print(f"  → {status}" + (f": {notes}" if notes else ""))
            if status != "failed":
                log_result(url, company, title, source, status, notes)
            counts[status] = counts.get(status, 0) + 1
            time.sleep(2)

        browser.close()

    print(f"\nDone. Applied: {counts.get('applied',0)} | Skipped: {counts.get('skipped',0)} | Failed: {counts.get('failed',0)}")
    print(f"Log: {LOG_PATH}")


if __name__ == "__main__":
    main()
