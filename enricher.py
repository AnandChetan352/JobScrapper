from __future__ import annotations

import hashlib
import html
import re
from typing import Any

import pandas as pd

from fx import FXSnapshot, resolve_currency_code

PERIOD_MULTIPLIERS: dict[str, float] = {
    "hourly": 2080.0,
    "hour": 2080.0,
    "hr": 2080.0,
    "daily": 260.0,
    "day": 260.0,
    "weekly": 52.0,
    "week": 52.0,
    "wk": 52.0,
    "fortnightly": 26.0,
    "biweekly": 26.0,
    "monthly": 12.0,
    "month": 12.0,
    "mo": 12.0,
    "yearly": 1.0,
    "annual": 1.0,
    "annually": 1.0,
    "year": 1.0,
    "yr": 1.0,
    "pa": 1.0,
    "p.a.": 1.0,
    "lpa": 100_000.0,
}

# Broad, domain-agnostic skill catalog (auto-extended at runtime with user search queries)
COMMON_SKILLS: list[str] = [
    "Python", "Java", "C#", ".NET", ".NET MAUI", "Xamarin", "JavaScript", "TypeScript",
    "React", "Angular", "Vue", "Next.js", "Node.js", "Go", "Rust", "Kotlin", "Swift",
    "Flutter", "React Native", "C++", "PHP", "Ruby", "SQL", "PostgreSQL", "MySQL",
    "MongoDB", "Redis", "Snowflake", "Spark", "Kafka", "Airflow", "dbt",
    "AWS", "Azure", "GCP", "Docker", "Kubernetes", "Terraform", "CI/CD", "GitHub Actions",
    "GraphQL", "REST", "gRPC", "Microservices", "Blazor", "ASP.NET", "Django", "FastAPI",
    "Spring Boot", "PyTorch", "TensorFlow", "LLM", "RAG", "LangChain",
    "Figma", "Product Management", "Agile", "Scrum", "Salesforce", "SAP", "Power BI", "Tableau",
]

# Regex patterns for parsing free-text salaries safely
LPA_PATTERN = re.compile(
    r"(?:₹|rs\.?|inr)?\s*(\d+(?:\.\d+)?)\s*(?:[-–to]+\s*(\d+(?:\.\d+)?))?\s*(?:lpa|lakhs?\s*(?:p\.?a\.?|per\s*annum)?|lacs?\s*(?:p\.?a\.?|per\s*annum)?)\b",
    re.IGNORECASE,
)

CURRENCY_SALARY_PATTERN = re.compile(
    r"(?P<curr>US\$|CA\$|AU\$|C\$|A\$|S\$|USD|EUR|GBP|INR|CAD|AUD|SGD|AED|CHF|Rs\.?|₹|\$|€|£)\s*"
    r"(?P<min>\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*(?P<min_k>[kK])?"
    r"(?:\s*(?:-|–|—|to)\s*(?:US\$|CA\$|AU\$|C\$|A\$|S\$|USD|EUR|GBP|INR|CAD|AUD|SGD|AED|CHF|Rs\.?|₹|\$|€|£)?\s*"
    r"(?P<max>\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*(?P<max_k>[kK])?)?"
    r"(?:\s*(?:/|per\s+)?(?P<period>hour|hr|day|week|wk|month|mo|year|yr|annum|p\.?a\.?))?",
    re.IGNORECASE,
)

EXP_PATTERN = re.compile(
    r"\b(\d{1,2})\s*(?:\+|(?:-|–|to)\s*(\d{1,2}))?\s*(?:years?|yrs?)\s+(?:of\s+)?(?:relevant\s+|work\s+|hands[- ]on\s+)?(?:experience|exp)\b",
    re.IGNORECASE,
)


def clean_html_to_text(raw: Any) -> str:
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return ""
    text = html.unescape(str(raw))
    text = re.sub(r"<(br|p|div|li|h[1-6])[^>]*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _normalize_period(raw_period: str | None) -> str:
    if not raw_period or pd.isna(raw_period):
        return "yearly"
    key = str(raw_period).strip().lower().replace("per ", "").replace("/", "")
    for token, _ in PERIOD_MULTIPLIERS.items():
        if token in key:
            if token in ("hourly", "hour", "hr"):
                return "hourly"
            if token in ("daily", "day"):
                return "daily"
            if token in ("weekly", "week", "wk"):
                return "weekly"
            if token in ("fortnightly", "biweekly"):
                return "fortnightly"
            if token in ("monthly", "month", "mo"):
                return "monthly"
            if token == "lpa":
                return "lpa"
            return "yearly"
    return "yearly"


def _infer_period_from_magnitude(amount: float, currency: str, explicit_period: str | None) -> str:
    """Safeguard when APIs omit pay period (e.g., $65 is hourly, $120000 is yearly, ₹18 is LPA)."""
    if explicit_period and str(explicit_period).strip():
        period = _normalize_period(explicit_period)
        if currency == "INR" and period == "yearly" and 1 <= amount <= 250:
            return "lpa"
        return period

    if currency == "INR":
        if 1 <= amount <= 250:
            return "lpa"
        if 15_000 <= amount <= 250_000:
            return "monthly"
        return "yearly"

    # Non-INR major currencies (USD/EUR/GBP/CAD/AUD/SGD/CHF)
    if 8 <= amount <= 400:
        return "hourly"
    if 400 < amount <= 1500:
        return "daily"
    if 1500 < amount <= 18_000:
        return "monthly"
    return "yearly"


def _parse_salary_from_text(text: str, default_currency: str | None = None) -> dict[str, Any] | None:
    if not text:
        return None

    # 1. Check Indian LPA syntax first (e.g., "12 - 20 LPA", "18 Lakhs p.a.")
    m_lpa = LPA_PATTERN.search(text)
    if m_lpa:
        lo = float(m_lpa.group(1))
        hi = float(m_lpa.group(2)) if m_lpa.group(2) else lo
        if 0.5 <= lo <= 300 and hi >= lo:
            return {
                "min_salary": lo * 100_000,
                "max_salary": hi * 100_000,
                "currency": "INR",
                "interval": "yearly",
                "raw_match": m_lpa.group(0).strip(),
            }

    # 2. Check general currency salary patterns (skipping funding/revenue strings)
    for match in CURRENCY_SALARY_PATTERN.finditer(text):
        start, end = match.span()
        surrounding = text[max(0, start - 35): min(len(text), end + 35)].lower()
        if any(noise in surrounding for noise in ("funding", "raised", "series ", "valuation", "revenue", "arr", "million", "billion")):
            continue

        curr = resolve_currency_code(match.group("curr"), fallback=default_currency)
        if not curr:
            continue

        lo = float(match.group("min").replace(",", ""))
        hi = float(match.group("max").replace(",", "")) if match.group("max") else lo

        if match.group("min_k"):
            lo *= 1_000
        if match.group("max_k"):
            hi *= 1_000
        elif match.group("min_k") and not match.group("max_k") and hi < 1_000:
            hi *= 1_000

        period = _infer_period_from_magnitude(max(lo, hi), curr, match.group("period"))

        # Plausibility bounds to avoid matching arbitrary numbers (years, counts, phone numbers)
        mult = PERIOD_MULTIPLIERS.get(period, 1.0)
        annual_check = hi * mult
        if curr == "INR":
            if not (120_000 <= annual_check <= 50_000_000):
                continue
        else:
            if not (6_000 <= annual_check <= 1_000_000):
                continue

        return {
            "min_salary": lo,
            "max_salary": hi,
            "currency": curr,
            "interval": period,
            "raw_match": match.group(0).strip(),
        }

    return None


def _convert_to_annual_inr(amount: float | None, currency: str | None, interval: str | None, fx: FXSnapshot) -> float | None:
    if amount is None or pd.isna(amount) or amount <= 0:
        return None
    curr = resolve_currency_code(currency, fallback="USD") or "USD"
    rate = fx.rates_to_inr.get(curr)
    if not rate:
        return None
    period = _infer_period_from_magnitude(float(amount), curr, interval)
    mult = PERIOD_MULTIPLIERS.get(period, 1.0)
    return round(float(amount) * mult * rate)


def infer_work_mode(is_remote: Any, location: str, title: str, description: str) -> str:
    if is_remote is True or str(is_remote).strip().lower() == "true":
        return "Remote"
    blob = f"{location} {title}".lower()
    if any(k in blob for k in ("remote", "work from home", "wfh", "anywhere", "distributed")):
        return "Remote"
    if "hybrid" in blob:
        return "Hybrid"
    if "hybrid" in description[:600].lower():
        return "Hybrid"
    if any(k in description[:600].lower() for k in ("100% remote", "fully remote", "remote-first")):
        return "Remote"
    return "On-site / Unspecified"


def infer_seniority(raw_level: Any, title: str) -> str:
    if raw_level and not pd.isna(raw_level) and str(raw_level).strip().lower() not in ("", "any", "none", "not applicable"):
        return str(raw_level).strip().title()
    t = title.lower()
    if any(k in t for k in ("intern", "trainee", "apprentice")):
        return "Internship"
    if any(k in t for k in ("vp", "vice president", "cto", "ceo", "chief", "head of", "director")):
        return "Executive / Director"
    if any(k in t for k in ("manager", "engineering manager")):
        return "Manager"
    if any(k in t for k in ("principal", "staff", "architect", "lead", "tech lead")):
        return "Lead / Staff / Principal"
    if any(k in t for k in ("senior", "sr.", "sr ", "iii", "iv")):
        return "Senior"
    if any(k in t for k in ("junior", "jr.", "jr ", "entry", "fresher", "associate", " i ")):
        return "Entry / Junior"
    return "Mid / Unspecified"


def infer_employment_type(raw_type: Any, title: str, description: str) -> str:
    blob = f"{raw_type or ''} {title}".lower()
    if any(k in blob for k in ("contract", "contractor", "freelance", "c2c", "b2b")):
        return "Contract / Freelance"
    if "part" in blob and "time" in blob:
        return "Part-Time"
    if "intern" in blob:
        return "Internship"
    if any(k in blob for k in ("full", "permanent", "regular")):
        return "Full-Time"
    if "full-time" in description[:800].lower() or "full time" in description[:800].lower():
        return "Full-Time"
    return "Unspecified"


def extract_experience(raw_exp: Any, text: str) -> tuple[int | None, str]:
    if raw_exp and not pd.isna(raw_exp) and str(raw_exp).strip():
        m = re.search(r"(\d+)", str(raw_exp))
        return (int(m.group(1)) if m else None), str(raw_exp).strip()
    m = EXP_PATTERN.search(text)
    if m:
        lo = int(m.group(1))
        hi = m.group(2)
        return lo, f"{lo}-{hi} yrs" if hi else f"{lo}+ yrs"
    return None, ""


def extract_skills(existing_tags: list[str], title: str, description: str, user_terms: list[str]) -> list[str]:
    found = {t.strip() for t in existing_tags if t and str(t).strip()}
    blob = f" {title} {description} "
    catalog = list(dict.fromkeys(COMMON_SKILLS + user_terms))
    for skill in catalog:
        escaped = re.escape(skill)
        if re.search(rf"(?<!\w){escaped}(?!\w)", blob, flags=re.IGNORECASE):
            found.add(skill)
    return sorted(found)[:15]


def make_job_fingerprint(company: str, title: str, url: str) -> str:
    c = re.sub(r"[^a-z0-9]", "", (company or "").lower())
    t = re.sub(r"[^a-z0-9]", "", (title or "").lower())
    key = f"{c}|{t}" if (c and t) else (url or "").strip().lower()
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def enrich_and_deduplicate(
    raw_jobs: list[dict[str, Any]],
    fx: FXSnapshot,
    target_monthly_inr: float,
    user_terms: list[str],
) -> pd.DataFrame:
    if not raw_jobs:
        return pd.DataFrame()

    target_annual_inr = target_monthly_inr * 12.0
    enriched_rows: list[dict[str, Any]] = []

    for job in raw_jobs:
        title = str(job.get("title") or "").strip()
        company = str(job.get("company") or "Unknown Company").strip()
        location = str(job.get("location") or "").strip()
        desc_text = clean_html_to_text(job.get("description"))
        raw_salary_text = str(job.get("salary_text") or "").strip()

        min_sal = job.get("min_salary")
        max_sal = job.get("max_salary")
        currency = resolve_currency_code(job.get("currency"))
        interval = job.get("interval")
        salary_source = "undisclosed"
        original_display = raw_salary_text

        # Tier 1: Structured salary fields from board/API
        has_structured = (min_sal is not None and not pd.isna(min_sal) and float(min_sal) > 0) or (
            max_sal is not None and not pd.isna(max_sal) and float(max_sal) > 0
        )
        if has_structured:
            salary_source = "structured"
            if not currency:
                currency = "INR" if ("india" in location.lower() or job.get("source") == "naukri") else "USD"
            if not original_display:
                lo_s = f"{float(min_sal):,.0f}" if min_sal and not pd.isna(min_sal) else ""
                hi_s = f"{float(max_sal):,.0f}" if max_sal and not pd.isna(max_sal) else ""
                rng = f"{lo_s} - {hi_s}".strip(" -")
                original_display = f"{currency} {rng} ({_infer_period_from_magnitude(float(max_sal or min_sal), currency, interval)})"
        else:
            # Tier 2: Parse raw salary_text string
            parsed = _parse_salary_from_text(raw_salary_text)
            if parsed:
                salary_source = "salary_text"
            else:
                # Tier 3: Parse description body dynamically
                default_curr = "INR" if "india" in location.lower() else None
                parsed = _parse_salary_from_text(desc_text, default_currency=default_curr)
                if parsed:
                    salary_source = "description_parsed"

            if parsed:
                min_sal = parsed["min_salary"]
                max_sal = parsed["max_salary"]
                currency = parsed["currency"]
                interval = parsed["interval"]
                original_display = original_display or parsed["raw_match"]

        min_inr_yr = _convert_to_annual_inr(min_sal, currency, interval, fx)
        max_inr_yr = _convert_to_annual_inr(max_sal, currency, interval, fx)
        est_annual_inr = max_inr_yr or min_inr_yr
        est_monthly_inr = round(est_annual_inr / 12.0) if est_annual_inr else None
        est_lpa = round(est_annual_inr / 100_000.0, 2) if est_annual_inr else None
        fx_rate_used = fx.rates_to_inr.get(currency) if currency else None

        if est_annual_inr is None:
            salary_status = "2 - Undisclosed (Verify)"
        elif est_annual_inr >= target_annual_inr:
            salary_status = "1 - Meets Target"
        else:
            salary_status = "3 - Below Target"

        min_exp_yrs, exp_text = extract_experience(job.get("experience"), desc_text)
        skills = extract_skills(job.get("tags") or [], title, desc_text, user_terms)
        work_mode = infer_work_mode(job.get("is_remote"), location, title, desc_text)
        seniority = infer_seniority(job.get("seniority"), title)
        emp_type = infer_employment_type(job.get("job_type"), title, desc_text)

        job_url = str(job.get("job_url") or "").strip()
        apply_url = str(job.get("apply_url") or job_url).strip()

        enriched_rows.append(
            {
                "job_id": make_job_fingerprint(company, title, job_url),
                "source": str(job.get("source") or "unknown"),
                "search_query": str(job.get("search_query") or ""),
                "title": title,
                "company": company,
                "company_url": str(job.get("company_url") or ""),
                "company_logo": str(job.get("company_logo") or ""),
                "company_industry": str(job.get("company_industry") or ""),
                "company_rating": job.get("company_rating"),
                "location": location or "Not specified",
                "timezone_info": str(job.get("timezone_info") or ""),
                "work_mode": work_mode,
                "employment_type": emp_type,
                "seniority": seniority,
                "experience_text": exp_text,
                "min_exp_years": min_exp_yrs,
                "visa_sponsorship": bool(job.get("visa_sponsorship", False)),
                "skills": ", ".join(skills),
                "salary_status": salary_status,
                "salary_source": salary_source,
                "original_salary": original_display,
                "posted_currency": currency or "",
                "fx_rate_to_inr": fx_rate_used,
                "min_inr_annual": min_inr_yr,
                "max_inr_annual": max_inr_yr,
                "est_annual_inr": est_annual_inr,
                "est_monthly_inr": est_monthly_inr,
                "est_lpa": est_lpa,
                "date_posted": str(job.get("date_posted") or "")[:10],
                "expiry_date": str(job.get("expiry_date") or "")[:10],
                "job_url": job_url,
                "apply_url": apply_url,
                "description": desc_text,
                "also_listed_on": "",
                "sources_count": 1,
            }
        )

    df = pd.DataFrame(enriched_rows)
    if df.empty:
        return df

    # Cross-board smart deduplication: keep the row with richest salary + description
    df["_richness"] = (
        df["est_annual_inr"].notna().astype(int) * 4
        + (df["salary_source"] == "structured").astype(int) * 2
        + (df["description"].str.len() > 200).astype(int) * 2
        + (df["company_logo"].str.len() > 0).astype(int)
    )
    df = df.sort_values(by=["job_id", "_richness", "date_posted"], ascending=[True, False, False])

    merged_rows = []
    for _, group in df.groupby("job_id", sort=False):
        best = group.iloc[0].copy()
        all_sources = list(dict.fromkeys(group["source"].tolist()))
        best["sources_count"] = len(all_sources)
        other_sources = [s for s in all_sources if s != best["source"]]
        best["also_listed_on"] = ", ".join(other_sources)
        merged_rows.append(best)

    result = pd.DataFrame(merged_rows).drop(columns=["_richness"])
    result = result.sort_values(
        by=["salary_status", "est_annual_inr", "date_posted"],
        ascending=[True, False, False],
        na_position="last",
    ).reset_index(drop=True)

    return result