from __future__ import annotations

import io
from datetime import datetime

import pandas as pd
import streamlit as st

from connectors import ALL_CONNECTORS, ConnectorHealth, fetch_all_sources
from enricher import enrich_and_deduplicate
from fx import FXSnapshot, fetch_fx_to_inr
from storage import STATUS_OPTIONS, sync_jobs_with_db, update_job_status

st.set_page_config(
    page_title="Global Job Intelligence Hub",
    page_icon="🌐",
    layout="wide",
)

INDEED_COUNTRIES = [
    "India", "USA", "UK", "Canada", "Germany", "Australia",
    "Singapore", "UAE", "Netherlands", "Ireland", "Remote / Worldwide",
]


@st.cache_data(ttl=43200, show_spinner=False)
def get_cached_fx() -> FXSnapshot:
    return fetch_fx_to_inr()


@st.cache_data(ttl=1800, show_spinner=False)
def run_cached_pipeline(
    search_terms: tuple[str, ...],
    selected_sources: tuple[str, ...],
    location: str,
    country_indeed: str,
    remote_only: bool,
    results_per_query: int,
    hours_old: int | None,
    fetch_descriptions: bool,
    target_monthly_inr: float,
    proxies: tuple[str, ...],
    fx_snapshot: FXSnapshot,
) -> tuple[pd.DataFrame, list[ConnectorHealth]]:
    raw_rows, health = fetch_all_sources(
        search_terms=list(search_terms),
        selected_sources=list(selected_sources),
        location=location,
        country_indeed=country_indeed,
        remote_only=remote_only,
        results_per_query=results_per_query,
        hours_old=hours_old,
        fetch_descriptions=fetch_descriptions,
        proxies=list(proxies),
    )
    enriched = enrich_and_deduplicate(
        raw_jobs=raw_rows,
        fx=fx_snapshot,
        target_monthly_inr=target_monthly_inr,
        user_terms=list(search_terms),
    )
    return enriched, health


def get_secret_proxies() -> list[str]:
    try:
        raw = st.secrets.get("PROXIES", "")
        if isinstance(raw, list):
            return [str(p).strip() for p in raw if str(p).strip()]
        return [line.strip() for line in str(raw).splitlines() if line.strip()]
    except Exception:
        return []


def to_excel_bytes(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Jobs")
    return buf.getvalue()


# ==========================================
# HEADER & LIVE FX BANNER
# ==========================================
fx = get_cached_fx()

h_left, h_right = st.columns([3, 1])
with h_left:
    st.title("🌐 Global Job Intelligence Hub")
    st.caption(
        "Multi-board concurrent job aggregator with dynamic salary extraction, "
        "live FX-to-INR conversion, cross-board deduplication, and SQLite tracking."
    )
with h_right:
    usd_rate = fx.rates_to_inr.get("USD", 85.0)
    eur_rate = fx.rates_to_inr.get("EUR", 92.0)
    st.caption(f"**Live FX ({fx.provider})**\n\n1 USD = ₹{usd_rate:.2f} | 1 EUR = ₹{eur_rate:.2f}")

# ==========================================
# SIDEBAR — FULLY GENERIC SEARCH CONTROLS
# ==========================================
with st.sidebar:
    st.header("Search Parameters")
    keywords_raw = st.text_input(
        "Job Titles / Skills (comma-separated)",
        value="",
        placeholder="e.g., .NET MAUI, Python Backend, Product Manager",
    )
    location = st.text_input("Location (for regional boards)", value="India")
    country_choice = st.selectbox("Indeed / Glassdoor Country", options=INDEED_COUNTRIES, index=0)
    remote_only = st.toggle("Remote-only filter (at scrape time)", value=True)

    selected_sources = st.multiselect(
        "Job Portals & APIs",
        options=ALL_CONNECTORS,
        default=["naukri", "linkedin", "indeed", "glassdoor", "himalayas", "jobicy", "remotive", "remoteok", "arbeitnow"],
    )

    st.divider()
    st.header("Compensation Target (INR)")
    target_mode = st.radio("Target Input Mode", ["Monthly (₹)", "Annual (LPA)"], horizontal=True)
    if target_mode == "Monthly (₹)":
        target_monthly_inr = st.number_input(
            "Min Target Monthly Gross (₹)",
            min_value=10_000,
            max_value=5_000_000,
            value=100_000,
            step=10_000,
        )
    else:
        target_lpa = st.number_input("Min Target Annual (LPA ₹)", min_value=1.0, max_value=300.0, value=12.0, step=1.0)
        target_monthly_inr = (target_lpa * 100_000) / 12.0

    st.divider()
    with st.expander("Advanced & Hosting Options"):
        results_per_query = st.slider("Max results per board / keyword", 5, 50, 20, step=5)
        freshness_days = st.selectbox("Max Posting Age (when not Remote-only)", [None, 1, 3, 7, 14, 30], format_func=lambda d: "Any" if d is None else f"Last {d} day(s)")
        fetch_descriptions = st.toggle(
            "Fetch full LinkedIn descriptions",
            value=False,
            help="Increases metadata richness but raises HTTP 429 risk on cloud IPs.",
        )
        secret_proxies = get_secret_proxies()
        if secret_proxies:
            st.success(f"{len(secret_proxies)} proxy/proxies loaded from secrets.")
        manual_proxies_raw = st.text_area("Override Proxies (one per line)", placeholder="user:pass@host:port", height=70)

    c1, c2 = st.columns(2)
    run_btn = c1.button("Search Jobs", type="primary", width="stretch")
    if c2.button("Clear Cache", width="stretch"):
        st.cache_data.clear()
        st.session_state.pop("jobs_df", None)
        st.session_state.pop("health_reports", None)
        st.rerun()

# ==========================================
# EXECUTE PIPELINE
# ==========================================
if run_btn:
    terms = tuple(t.strip() for t in keywords_raw.split(",") if t.strip())
    if not terms:
        st.error("Enter at least one job title or skill keyword in the sidebar.")
        st.stop()
    if not selected_sources:
        st.error("Select at least one job portal or API.")
        st.stop()

    manual_proxies = [p.strip() for p in manual_proxies_raw.splitlines() if p.strip()]
    active_proxies = tuple(manual_proxies if manual_proxies else secret_proxies)
    country_param = "India" if country_choice == "Remote / Worldwide" else country_choice

    with st.spinner(f"Querying {len(selected_sources)} sources concurrently and normalizing to INR..."):
        df_enriched, health_reports = run_cached_pipeline(
            search_terms=terms,
            selected_sources=tuple(selected_sources),
            location=location.strip() or "India",
            country_indeed=country_param,
            remote_only=remote_only,
            results_per_query=results_per_query,
            hours_old=freshness_days * 24 if freshness_days else None,
            fetch_descriptions=fetch_descriptions,
            target_monthly_inr=float(target_monthly_inr),
            proxies=active_proxies,
            fx_snapshot=fx,
        )
        df_synced = sync_jobs_with_db(df_enriched)
        st.session_state["jobs_df"] = df_synced
        st.session_state["health_reports"] = health_reports

# ==========================================
# MAIN DASHBOARD
# ==========================================
if "jobs_df" not in st.session_state:
    st.info("Enter any job title or skill in the sidebar (e.g., `.NET MAUI`, `Staff Backend Engineer`, `Data Scientist`) and click **Search Jobs**.")
    st.stop()

df: pd.DataFrame = st.session_state["jobs_df"]
health_reports: list[ConnectorHealth] = st.session_state.get("health_reports", [])

if df.empty:
    st.warning("No matching jobs returned. Check the **Connector Health** tab below to see if any portals rate-limited your IP.")
    st.dataframe(pd.DataFrame([h.__dict__ for h in health_reports]), width="stretch", hide_index=True)
    st.stop()

# KPI Row
k1, k2, k3, k4, k5 = st.columns(5)
k1.metric("Unique Jobs", len(df))
k2.metric("New Since Last Run", int(df["is_new"].sum()))
k3.metric("Meets INR Target", int((df["salary_status"] == "1 - Meets Target").sum()))
k4.metric("Salary Disclosed", int(df["est_annual_inr"].notna().sum()))
k5.metric("Multi-Board Matches", int((df["sources_count"] > 1).sum()))

tab_explore, tab_inspect, tab_analytics, tab_health = st.tabs(
    ["📋 Job Explorer", "🔍 Inspector & Tracker", "📊 Market Analytics", "🩺 Connector Health & FX"]
)

# ------------------------------------------
# TAB 1: JOB EXPLORER
# ------------------------------------------
with tab_explore:
    f1, f2, f3, f4 = st.columns([2, 2, 2, 2])
    sel_sal_status = f1.multiselect("Salary Bucket", sorted(df["salary_status"].unique()), default=sorted(df["salary_status"].unique()))
    sel_work_modes = f2.multiselect("Work Mode", sorted(df["work_mode"].unique()), default=sorted(df["work_mode"].unique()))
    sel_seniority = f3.multiselect("Seniority", sorted(df["seniority"].unique()), default=sorted(df["seniority"].unique()))
    sel_sources = f4.multiselect("Primary Source", sorted(df["source"].unique()), default=sorted(df["source"].unique()))

    f5, f6, f7, f8 = st.columns([3, 2, 1.5, 1.5])
    text_q = f5.text_input("Instant search (title, company, skills, description)", placeholder="e.g., Azure, fintech, visa")
    sel_app_status = f6.multiselect("Application Status", STATUS_OPTIONS, default=STATUS_OPTIONS)
    only_disclosed = f7.checkbox("Salary disclosed only", value=False)
    only_new = f8.checkbox("New listings only", value=False)

    filtered = df[
        df["salary_status"].isin(sel_sal_status)
        & df["work_mode"].isin(sel_work_modes)
        & df["seniority"].isin(sel_seniority)
        & df["source"].isin(sel_sources)
        & df["app_status"].isin(sel_app_status)
    ].copy()

    if only_disclosed:
        filtered = filtered[filtered["est_annual_inr"].notna()]
    if only_new:
        filtered = filtered[filtered["is_new"]]
    if text_q.strip():
        q = text_q.strip().lower()
        mask = (
            filtered["title"].str.lower().str.contains(q, regex=False)
            | filtered["company"].str.lower().str.contains(q, regex=False)
            | filtered["skills"].str.lower().str.contains(q, regex=False)
            | filtered["description"].str.lower().str.contains(q, regex=False)
        )
        filtered = filtered[mask]

    display_cols = [
        "app_status",
        "salary_status",
        "est_monthly_inr",
        "est_lpa",
        "original_salary",
        "salary_source",
        "title",
        "company",
        "work_mode",
        "seniority",
        "experience_text",
        "location",
        "skills",
        "source",
        "sources_count",
        "date_posted",
        "apply_url",
    ]

    st.dataframe(
        filtered[display_cols],
        width="stretch",
        hide_index=True,
        height=520,
        column_config={
            "app_status": st.column_config.TextColumn("Status"),
            "salary_status": st.column_config.TextColumn("Target Match"),
            "est_monthly_inr": st.column_config.NumberColumn("Est. Monthly (₹)", format="₹%,.0f"),
            "est_lpa": st.column_config.NumberColumn("Est. LPA (₹)", format="₹%.1f L"),
            "original_salary": st.column_config.TextColumn("Posted Salary"),
            "salary_source": st.column_config.TextColumn("Salary Source"),
            "title": st.column_config.TextColumn("Role", width="medium"),
            "company": st.column_config.TextColumn("Company"),
            "work_mode": st.column_config.TextColumn("Mode"),
            "seniority": st.column_config.TextColumn("Level"),
            "experience_text": st.column_config.TextColumn("Exp."),
            "location": st.column_config.TextColumn("Location"),
            "skills": st.column_config.TextColumn("Extracted Skills", width="medium"),
            "source": st.column_config.TextColumn("Board"),
            "sources_count": st.column_config.NumberColumn("Boards #"),
            "date_posted": st.column_config.TextColumn("Posted"),
            "apply_url": st.column_config.LinkColumn("Apply", display_text="Open"),
        },
    )

    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    d1, d2, d3, _ = st.columns([1, 1, 1, 3])
    d1.download_button("Export CSV", filtered.to_csv(index=False).encode("utf-8"), f"jobs_{stamp}.csv", "text/csv", width="stretch")
    d2.download_button("Export Excel", to_excel_bytes(filtered), f"jobs_{stamp}.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", width="stretch")
    d3.download_button("Export JSON", filtered.to_json(orient="records", indent=2).encode("utf-8"), f"jobs_{stamp}.json", "application/json", width="stretch")

# ------------------------------------------
# TAB 2: JOB INSPECTOR & PIPELINE TRACKER
# ------------------------------------------
with tab_inspect:
    if filtered.empty:
        st.info("No rows match the active filters.")
    else:
        labels = [
            f"[{r['app_status']}] {r['title']} — {r['company']} ({r['source']})"
            for _, r in filtered.iterrows()
        ]
        idx = st.selectbox("Select a job to inspect & update status", range(len(labels)), format_func=lambda i: labels[i])
        row = filtered.iloc[idx]

        top_l, top_r = st.columns([3, 1])
        with top_l:
            st.subheader(f"{row['title']} — {row['company']}")
            st.caption(
                f"Mode: **{row['work_mode']}** | Type: **{row['employment_type']}** | "
                f"Level: **{row['seniority']}** | Exp: **{row['experience_text'] or 'Not specified'}** | "
                f"Location: **{row['location']}**"
            )
            if row["timezone_info"]:
                st.caption(f"Timezone Eligibility: **{row['timezone_info']}**")
            if row["also_listed_on"]:
                st.caption(f"Also detected on: **{row['also_listed_on']}**")
        with top_r:
            if row["company_logo"]:
                st.image(row["company_logo"], width=80)
            st.link_button("Apply Directly ↗", row["apply_url"], type="primary", width="stretch")

        c_sal1, c_sal2, c_sal3, c_sal4 = st.columns(4)
        c_sal1.metric("Posted Salary", row["original_salary"] or "Undisclosed")
        c_sal2.metric("Converted Monthly (INR)", f"₹{row['est_monthly_inr']:,.0f}" if pd.notna(row["est_monthly_inr"]) else "N/A")
        c_sal3.metric("Converted Annual (LPA)", f"₹{row['est_lpa']:.1f} L" if pd.notna(row["est_lpa"]) else "N/A")
        c_sal4.metric("Extraction Source", row["salary_source"])

        if row["skills"]:
            st.markdown(f"**Detected Skills:** `{row['skills']}`")

        with st.form(key=f"tracker_{row['job_id']}"):
            st_col1, st_col2 = st.columns([1, 3])
            cur_status_idx = STATUS_OPTIONS.index(row["app_status"]) if row["app_status"] in STATUS_OPTIONS else 0
            new_status = st_col1.selectbox("Pipeline Status", STATUS_OPTIONS, index=cur_status_idx)
            new_notes = st_col2.text_input("Personal Notes", value=row["notes"])
            if st.form_submit_button("Save Status to SQLite"):
                update_job_status(row["job_id"], new_status, new_notes)
                st.session_state["jobs_df"].loc[st.session_state["jobs_df"]["job_id"] == row["job_id"], ["app_status", "notes"]] = [new_status, new_notes]
                st.success("Saved!")
                st.rerun()

        st.divider()
        st.markdown("#### Full Job Description")
        st.write(row["description"] or "No full description provided by this board. Open the apply link to view details.")

# ------------------------------------------
# TAB 3: MARKET ANALYTICS
# ------------------------------------------
with tab_analytics:
    a1, a2 = st.columns(2)
    with a1:
        st.subheader("Listings by Source")
        st.bar_chart(df["source"].value_counts())

        st.subheader("Work Mode Split")
        st.bar_chart(df["work_mode"].value_counts())

    with a2:
        st.subheader("Top Extracted Skills")
        all_skills = [s.strip() for cell in df["skills"].dropna() for s in cell.split(",") if s.strip()]
        if all_skills:
            st.bar_chart(pd.Series(all_skills).value_counts().head(15))
        else:
            st.info("No skills extracted yet.")

        st.subheader("Median Annual Salary by Seniority (LPA ₹)")
        sal_df = df[df["est_lpa"].notna()]
        if not sal_df.empty:
            st.bar_chart(sal_df.groupby("seniority")["est_lpa"].median().sort_values(ascending=False))
        else:
            st.info("No disclosed salaries in this batch to chart.")

# ------------------------------------------
# TAB 4: CONNECTOR HEALTH & FX AUDIT
# ------------------------------------------
with tab_health:
    st.subheader("Connector Execution Telemetry")
    st.dataframe(pd.DataFrame([h.__dict__ for h in health_reports]), width="stretch", hide_index=True)

    st.subheader("Live Exchange Rates Used (1 Unit -> INR)")
    st.caption(f"Provider: {fx.provider} | Last Updated: {fx.updated_utc} | Attribution: Rates by ExchangeRate-API")
    major_codes = ["USD", "EUR", "GBP", "CAD", "AUD", "SGD", "AED", "CHF", "JPY", "PLN", "SEK"]
    fx_rows = [{"Currency": c, "1 Unit in INR (₹)": round(fx.rates_to_inr[c], 2)} for c in major_codes if c in fx.rates_to_inr]
    st.dataframe(pd.DataFrame(fx_rows), width="stretch", hide_index=True)