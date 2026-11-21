from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
import time
from typing import Any
import random 
import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from jobspy import scrape_jobs

JOBSPY_BOARDS = ["naukri", "linkedin", "indeed", "glassdoor", "zip_recruiter", "bayt", "bdjobs"]
PUBLIC_API_BOARDS = ["himalayas", "jobicy", "remotive", "remoteok", "arbeitnow"]
ALL_CONNECTORS = JOBSPY_BOARDS + PUBLIC_API_BOARDS


@dataclass
class ConnectorHealth:
    source: str
    status: str  # "OK", "WARNING", "ERROR"
    count: int
    latency_sec: float
    message: str


def _build_session() -> requests.Session:
    session = requests.Session()
    retries = Retry(total=2, backoff_factor=0.6, status_forcelist=[429, 500, 502, 503, 504])
    session.mount("https://", HTTPAdapter(max_retries=retries))
    session.headers.update({"User-Agent": "JobIntelligenceHub/2.0 (Streamlit Aggregator)"})
    return session


def _unix_to_iso(ts: Any) -> str:
    try:
        if ts:
            return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        pass
    return ""


def _safe_val(row: pd.Series, col: str, default: Any = None) -> Any:
    val = row.get(col, default)
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return default
    return val


def _fetch_jobspy_single_board(
    board: str,
    search_terms: list[str],
    location: str,
    country_indeed: str,
    remote_only: bool,
    results_per_query: int,
    hours_old: int | None,
    fetch_descriptions: bool,
    proxies: list[str],
) -> tuple[list[dict[str, Any]], ConnectorHealth]:
    t0 = time.perf_counter()
    rows: list[dict[str, Any]] = []
    notes: list[str] = []

    # LinkedIn supports Boolean OR — combine multiple terms into 1 call to avoid burst 429s
    if board == "linkedin" and len(search_terms) > 1:
        effective_terms = [" OR ".join(f'"{t}"' for t in search_terms)]
    else:
        effective_terms = search_terms

    # Without proxies, cap LinkedIn batch size and disable per-job description fetches
    effective_results = min(results_per_query, 15) if (board == "linkedin" and not proxies) else results_per_query
    effective_fetch_desc = fetch_descriptions if (board != "linkedin" or proxies) else False

    for idx, term in enumerate(effective_terms):
        kwargs: dict[str, Any] = {
            "site_name": [board],
            "search_term": term,
            "location": location,
            "results_wanted": effective_results,
            "country_indeed": country_indeed,
            "fetch_description": effective_fetch_desc,
            "verbose": 0,
        }
        if remote_only:
            kwargs["is_remote"] = True
        elif hours_old:
            kwargs["hours_old"] = hours_old

        if proxies:
            kwargs["proxies"] = proxies

        max_attempts = 2 if board == "linkedin" else 1
        for attempt in range(max_attempts):
            try:
                df = scrape_jobs(**kwargs)
                errors = df.attrs.get("errors", {})
                err_text = str(errors)

                # If LinkedIn returns 429 on first attempt, wait 65s for burst window to clear and retry once
                if board == "linkedin" and "429" in err_text and attempt == 0 and df.empty:
                    time.sleep(65)
                    continue

                if errors:
                    notes.append(f"{term}: {errors}")

                for _, r in df.iterrows():
                    skills_raw = _safe_val(r, "skills", "")
                    tags = [s.strip() for s in str(skills_raw).split(",") if s.strip()] if skills_raw else []
                    rows.append(
                        {
                            "source": str(_safe_val(r, "site", board)),
                            "search_query": term,
                            "title": _safe_val(r, "title", ""),
                            "company": _safe_val(r, "company", ""),
                            "company_url": _safe_val(r, "company_url", ""),
                            "company_logo": _safe_val(r, "company_logo", ""),
                            "company_industry": _safe_val(r, "company_industry", ""),
                            "company_rating": _safe_val(r, "company_rating", None),
                            "location": _safe_val(r, "location", ""),
                            "is_remote": _safe_val(r, "is_remote", None),
                            "job_type": _safe_val(r, "job_type", ""),
                            "seniority": _safe_val(r, "job_level", ""),
                            "experience": _safe_val(r, "experience_range", ""),
                            "min_salary": _safe_val(r, "min_amount", None),
                            "max_salary": _safe_val(r, "max_amount", None),
                            "currency": _safe_val(r, "currency", None),
                            "interval": _safe_val(r, "interval", None),
                            "salary_text": "",
                            "date_posted": str(_safe_val(r, "date_posted", "")),
                            "expiry_date": "",
                            "job_url": _safe_val(r, "job_url", ""),
                            "apply_url": _safe_val(r, "job_url_direct", "") or _safe_val(r, "job_url", ""),
                            "description": _safe_val(r, "description", ""),
                            "tags": tags,
                            "timezone_info": "",
                            "visa_sponsorship": False,
                        }
                    )
                break
            except Exception as exc:
                notes.append(f"{term}: {exc}")
                break

        if idx < len(effective_terms) - 1:
            time.sleep(random.uniform(6.0, 10.0) if board == "linkedin" else random.uniform(2.0, 4.0))

    latency = round(time.perf_counter() - t0, 2)
    status = "ERROR" if (notes and not rows) else ("WARNING" if notes else "OK")
    return rows, ConnectorHealth(board, status, len(rows), latency, "; ".join(notes) or "Success")


def _fetch_himalayas(search_terms: list[str], results_per_query: int) -> tuple[list[dict[str, Any]], ConnectorHealth]:
    t0 = time.perf_counter()
    session = _build_session()
    rows: list[dict[str, Any]] = []
    notes: list[str] = []

    for term in search_terms:
        try:
            resp = session.get(
                "https://himalayas.app/jobs/api/search",
                params={"q": term, "page": 1},
                timeout=15,
            )
            resp.raise_for_status()
            jobs = resp.json().get("jobs", [])[:results_per_query]
            for j in jobs:
                locs = j.get("locationRestrictions") or []
                tzs = j.get("timezoneRestrictions") or []
                tz_label = "Worldwide" if len(tzs) >= 20 or not tzs else ", ".join(f"UTC{z:+d}" for z in tzs[:6])
                rows.append(
                    {
                        "source": "himalayas",
                        "search_query": term,
                        "title": j.get("title", ""),
                        "company": j.get("companyName", ""),
                        "company_url": f"https://himalayas.app/companies/{j.get('companySlug')}" if j.get("companySlug") else "",
                        "company_logo": j.get("companyLogo", ""),
                        "company_industry": ", ".join(j.get("parentCategories") or []),
                        "location": ", ".join(locs) if locs else "Worldwide",
                        "is_remote": True,
                        "job_type": j.get("employmentType", ""),
                        "seniority": ", ".join(j.get("seniority") or []) if isinstance(j.get("seniority"), list) else (j.get("seniority") or ""),
                        "min_salary": j.get("minSalary"),
                        "max_salary": j.get("maxSalary"),
                        "currency": j.get("currency"),
                        "interval": j.get("salaryPeriod") or "yearly",
                        "salary_text": "",
                        "date_posted": _unix_to_iso(j.get("pubDate")),
                        "expiry_date": _unix_to_iso(j.get("expiryDate")),
                        "job_url": j.get("guid") or j.get("applicationLink") or "",
                        "apply_url": j.get("applicationLink") or j.get("guid") or "",
                        "description": j.get("description") or j.get("excerpt") or "",
                        "tags": j.get("categories") or [],
                        "timezone_info": tz_label,
                    }
                )
        except Exception as exc:
            notes.append(f"{term}: {exc}")

    latency = round(time.perf_counter() - t0, 2)
    status = "ERROR" if (notes and not rows) else ("WARNING" if notes else "OK")
    return rows, ConnectorHealth("himalayas", status, len(rows), latency, "; ".join(notes) or "Success")


def _fetch_jobicy(search_terms: list[str], results_per_query: int) -> tuple[list[dict[str, Any]], ConnectorHealth]:
    t0 = time.perf_counter()
    session = _build_session()
    rows: list[dict[str, Any]] = []
    notes: list[str] = []

    for term in search_terms:
        try:
            params: dict[str, Any] = {"count": min(max(results_per_query, 10), 50)}
            if len(term.strip()) >= 3:
                params["tag"] = term.strip()[:50]
            resp = session.get("https://jobicy.com/api/v2/remote-jobs", params=params, timeout=15)
            resp.raise_for_status()
            for j in resp.json().get("jobs", []):
                job_types = j.get("jobType") or []
                rows.append(
                    {
                        "source": "jobicy",
                        "search_query": term,
                        "title": j.get("jobTitle", ""),
                        "company": j.get("companyName", ""),
                        "company_logo": j.get("companyLogo", ""),
                        "company_industry": ", ".join(j.get("jobIndustry") or []),
                        "location": j.get("jobGeo") or "Anywhere",
                        "is_remote": True,
                        "job_type": ", ".join(job_types) if isinstance(job_types, list) else str(job_types),
                        "seniority": j.get("jobLevel", ""),
                        "min_salary": j.get("salaryMin") or j.get("annualSalaryMin"),
                        "max_salary": j.get("salaryMax") or j.get("annualSalaryMax"),
                        "currency": j.get("salaryCurrency"),
                        "interval": j.get("salaryPeriod") or "yearly",
                        "salary_text": "",
                        "date_posted": str(j.get("pubDate", ""))[:10],
                        "job_url": j.get("url", ""),
                        "apply_url": j.get("url", ""),
                        "description": j.get("jobDescription") or j.get("jobExcerpt") or "",
                        "tags": j.get("jobIndustry") or [],
                    }
                )
        except Exception as exc:
            notes.append(f"{term}: {exc}")

    latency = round(time.perf_counter() - t0, 2)
    status = "ERROR" if (notes and not rows) else ("WARNING" if notes else "OK")
    return rows, ConnectorHealth("jobicy", status, len(rows), latency, "; ".join(notes) or "Success")


def _fetch_remotive(search_terms: list[str], results_per_query: int) -> tuple[list[dict[str, Any]], ConnectorHealth]:
    t0 = time.perf_counter()
    session = _build_session()
    rows: list[dict[str, Any]] = []
    notes: list[str] = []

    for term in search_terms:
        try:
            resp = session.get(
                "https://remotive.com/api/remote-jobs",
                params={"search": term, "limit": results_per_query},
                timeout=15,
            )
            resp.raise_for_status()
            for j in resp.json().get("jobs", []):
                rows.append(
                    {
                        "source": "remotive",
                        "search_query": term,
                        "title": j.get("title", ""),
                        "company": j.get("company_name", ""),
                        "company_logo": j.get("company_logo", ""),
                        "company_industry": j.get("category", ""),
                        "location": j.get("candidate_required_location") or "Worldwide",
                        "is_remote": True,
                        "job_type": j.get("job_type", ""),
                        "salary_text": j.get("salary", ""),
                        "date_posted": str(j.get("publication_date", ""))[:10],
                        "job_url": j.get("url", ""),
                        "apply_url": j.get("url", ""),
                        "description": j.get("description", ""),
                        "tags": j.get("tags") or [],
                    }
                )
        except Exception as exc:
            notes.append(f"{term}: {exc}")

    latency = round(time.perf_counter() - t0, 2)
    status = "ERROR" if (notes and not rows) else ("WARNING" if notes else "OK")
    return rows, ConnectorHealth("remotive", status, len(rows), latency, "; ".join(notes) or "Success")


def _fetch_remoteok(search_terms: list[str], results_per_query: int) -> tuple[list[dict[str, Any]], ConnectorHealth]:
    t0 = time.perf_counter()
    session = _build_session()
    rows: list[dict[str, Any]] = []
    kw_lower = [k.lower() for k in search_terms]

    try:
        resp = session.get("https://remoteok.com/api", timeout=15)
        resp.raise_for_status()
        data = resp.json()
        for j in data[1:]:
            blob = f"{j.get('position', '')} {j.get('description', '')} {' '.join(j.get('tags') or [])}".lower()
            matched = [k for k in kw_lower if k in blob]
            if not matched:
                continue
            rows.append(
                {
                    "source": "remoteok",
                    "search_query": ", ".join(matched),
                    "title": j.get("position", ""),
                    "company": j.get("company", ""),
                    "company_logo": j.get("company_logo", ""),
                    "location": j.get("location") or "Worldwide",
                    "is_remote": True,
                    "job_type": "full_time",
                    "min_salary": j.get("salary_min") or None,
                    "max_salary": j.get("salary_max") or None,
                    "currency": "USD",
                    "interval": "yearly",
                    "date_posted": str(j.get("date", ""))[:10],
                    "job_url": j.get("url", ""),
                    "apply_url": j.get("apply_url") or j.get("url", ""),
                    "description": j.get("description", ""),
                    "tags": j.get("tags") or [],
                }
            )
            if len(rows) >= results_per_query * len(search_terms):
                break
        return rows, ConnectorHealth("remoteok", "OK", len(rows), round(time.perf_counter() - t0, 2), "Success")
    except Exception as exc:
        return [], ConnectorHealth("remoteok", "ERROR", 0, round(time.perf_counter() - t0, 2), str(exc))


def _fetch_arbeitnow(search_terms: list[str], remote_only: bool, results_per_query: int) -> tuple[list[dict[str, Any]], ConnectorHealth]:
    t0 = time.perf_counter()
    session = _build_session()
    rows: list[dict[str, Any]] = []
    kw_lower = [k.lower() for k in search_terms]

    try:
        resp = session.get("https://www.arbeitnow.com/api/job-board-api", timeout=15)
        resp.raise_for_status()
        for j in resp.json().get("data", []):
            if remote_only and not j.get("remote"):
                continue
            blob = f"{j.get('title', '')} {j.get('description', '')} {' '.join(j.get('tags') or [])}".lower()
            matched = [k for k in kw_lower if k in blob]
            if not matched:
                continue
            rows.append(
                {
                    "source": "arbeitnow",
                    "search_query": ", ".join(matched),
                    "title": j.get("title", ""),
                    "company": j.get("company_name", ""),
                    "location": j.get("location", ""),
                    "is_remote": bool(j.get("remote")),
                    "job_type": ", ".join(j.get("job_types") or []),
                    "visa_sponsorship": bool(j.get("visa_sponsorship", False)),
                    "date_posted": _unix_to_iso(j.get("created_at")),
                    "job_url": j.get("url", ""),
                    "apply_url": j.get("url", ""),
                    "description": j.get("description", ""),
                    "tags": j.get("tags") or [],
                }
            )
            if len(rows) >= results_per_query * len(search_terms):
                break
        return rows, ConnectorHealth("arbeitnow", "OK", len(rows), round(time.perf_counter() - t0, 2), "Success")
    except Exception as exc:
        return [], ConnectorHealth("arbeitnow", "ERROR", 0, round(time.perf_counter() - t0, 2), str(exc))


def fetch_all_sources(
    search_terms: list[str],
    selected_sources: list[str],
    location: str,
    country_indeed: str,
    remote_only: bool,
    results_per_query: int,
    hours_old: int | None,
    fetch_descriptions: bool,
    proxies: list[str],
) -> tuple[list[dict[str, Any]], list[ConnectorHealth]]:
    """Executes all selected job connectors concurrently with isolated error handling."""
    all_rows: list[dict[str, Any]] = []
    health_reports: list[ConnectorHealth] = []

    with ThreadPoolExecutor(max_workers=min(8, max(1, len(selected_sources)))) as pool:
        futures = {}
        for src in selected_sources:
            if src in JOBSPY_BOARDS:
                fut = pool.submit(
                    _fetch_jobspy_single_board,
                    src,
                    search_terms,
                    location,
                    country_indeed,
                    remote_only,
                    results_per_query,
                    hours_old,
                    fetch_descriptions,
                    proxies,
                )
            elif src == "himalayas":
                fut = pool.submit(_fetch_himalayas, search_terms, results_per_query)
            elif src == "jobicy":
                fut = pool.submit(_fetch_jobicy, search_terms, results_per_query)
            elif src == "remotive":
                fut = pool.submit(_fetch_remotive, search_terms, results_per_query)
            elif src == "remoteok":
                fut = pool.submit(_fetch_remoteok, search_terms, results_per_query)
            elif src == "arbeitnow":
                fut = pool.submit(_fetch_arbeitnow, search_terms, remote_only, results_per_query)
            else:
                continue
            futures[fut] = src

        for fut in as_completed(futures):
            src = futures[fut]
            try:
                rows, health = fut.result()
                all_rows.extend(rows)
                health_reports.append(health)
            except Exception as exc:
                health_reports.append(ConnectorHealth(src, "ERROR", 0, 0.0, str(exc)))

    health_reports.sort(key=lambda h: h.source)
    return all_rows, health_reports