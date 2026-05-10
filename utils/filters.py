"""
Central filter config loader. All crawlers, ranker, and agents import from here.
Source of truth: filters.yaml in the repo root.
"""
import yaml
from pathlib import Path

_ROOT = Path(__file__).parent.parent
_cfg: dict | None = None


def _load() -> dict:
    global _cfg
    if _cfg is None:
        with open(_ROOT / "filters.yaml") as f:
            _cfg = yaml.safe_load(f)
    return _cfg


def location_keywords() -> list[str]:
    return _load()["locations"]["allowed"]

def linkedin_locations() -> list[str]:
    return _load()["locations"]["linkedin_search"]

def ineligible_locations() -> list[str]:
    return _load()["locations"]["ineligible"]

def location_score_tiers() -> list[dict]:
    return _load()["locations"]["score_tiers"]

def location_fallback_scores() -> dict:
    return _load()["scoring"]["location_fallback"]

def title_allowed() -> list[str]:
    return _load()["titles"]["allowed"]

def title_excluded() -> list[str]:
    return _load()["titles"]["excluded"]

def senior_phrases() -> list[str]:
    return _load()["titles"]["senior_phrases"]

def company_blocklist() -> set[str]:
    return set(_load()["companies"]["blocklist"])

def company_tier1() -> set[str]:
    return set(_load()["companies"]["tier1"])

def company_tier2() -> set[str]:
    return set(_load()["companies"]["tier2"])

def experience_max_years() -> int:
    return _load()["experience"]["max_years"]

def phd_patterns() -> list[str]:
    return _load()["experience"]["phd_patterns"]

def recency_hours(source: str = "crawler") -> int:
    key = "email_hours" if source == "email" else "crawler_hours"
    return _load()["recency"][key]

def linkedin_time_range() -> str:
    return _load()["recency"]["linkedin_time_range"]

def scoring_weights() -> dict:
    return _load()["scoring"]["weights"]

def scoring_keywords() -> list[str]:
    return _load()["scoring"]["keywords"]

def top_n() -> int:
    return _load()["output"]["top_n"]
