"""Markdown and CSV output."""
import csv
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus

from .contacts import linkedin_jobs_link, linkedin_people_links
from .textutil import emails_in


def _salary_cell(job, fx):
    if not job.salary:
        return "not listed"
    s = job.salary.display()
    if job.salary.currency != "EUR":
        lo, hi = job.salary.to_eur_year(fx)
        if hi or lo:
            s += f" (≈ €{(hi or lo) / 1000:.0f}k/yr max)"
    return s


def write_csv(path: Path, jobs, fx):
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["new", "score", "company", "title", "location", "salary", "salary_max_eur_year",
                    "years_required", "posted", "matched_keywords", "url"])
        for j in jobs:
            _, hi = j.salary.to_eur_year(fx) if j.salary else (None, None)
            w.writerow(["NEW" if j.is_new else "", j.score, j.company, j.title, j.location,
                        j.salary.display() if j.salary else "", f"{hi:.0f}" if hi else "",
                        j.years_required or "", f"{j.published:%Y-%m-%d}" if j.published else "",
                        ", ".join(j.matched), j.url])


def write_markdown(path: Path, jobs, cfg, crit, contacts, failures, stats, hunter_enabled):
    now = datetime.now()
    out = [f"# Job scout report, {now:%Y-%m-%d %H:%M}", ""]
    new = sum(j.is_new for j in jobs)
    out += [f"**{len(jobs)} matching jobs** ({new} new since last run) from "
            f"{stats['fetched']} postings across {stats['companies']} companies.", ""]
    crit_bits = [f"locations: {', '.join(crit.locations) or 'any'}"]
    if crit.allow_remote:
        crit_bits.append(f"remote in: {', '.join(crit.remote_regions) or 'anywhere'}")
    if crit.salary_min_eur or crit.salary_max_eur:
        crit_bits.append(f"salary €{crit.salary_min_eur:,.0f}–{crit.salary_max_eur or '∞'}")
    if crit.max_age_days:
        crit_bits.append(f"posted within {crit.max_age_days} days")
    if crit.max_years_required:
        crit_bits.append(f"≤ {crit.max_years_required} years required")
    out += ["Criteria: " + "; ".join(crit_bits), ""]

    if failures:
        out += ["> **Sources that failed:** " + "; ".join(f"{c} ({e})" for c, e in failures), ""]

    out += ["## Jobs", "", "| | Score | Company | Role | Location | Salary | Yrs | Posted |",
            "|---|---|---|---|---|---|---|---|"]
    for j in jobs:
        title = j.title.replace("|", "/")
        loc = j.location if len(j.location) <= 60 else j.location[:57] + "..."
        if j.variants > 1:
            loc += f" (+{j.variants - 1} more postings)"
        out.append(f"| {'🆕' if j.is_new else ''} | {j.score} | {j.company} | [{title}]({j.url}) | "
                   f"{loc.replace('|', '/')} | {_salary_cell(j, crit.fx_to_eur)} | "
                   f"{j.years_required or ''} | {f'{j.published:%Y-%m-%d}' if j.published else ''} |")
    out.append("")

    # ---- contacts, one block per company that has matches ----
    by_company = defaultdict(list)
    for j in jobs:
        by_company[j.company].append(j)
    out += ["## Recruiter contacts", ""]
    if not hunter_enabled:
        out += ["> Email lookup is off. Set `HUNTER_API_KEY` to fetch recruiter emails "
                "(free key at hunter.io). LinkedIn search links below work regardless.", ""]
    for company, cjobs in sorted(by_company.items(), key=lambda kv: -max(j.score for j in kv[1])):
        out += [f"### {company}", ""]
        posted = sorted({e for j in cjobs for e in emails_in(j.description)})
        if posted:
            out.append("- **Emails in the postings:** " + ", ".join(posted))
        people = contacts.get(company) or []
        for p in people:
            li = f" · [LinkedIn]({p['linkedin']})" if p.get("linkedin") else ""
            conf = f" ({p['confidence']}% confidence)" if p.get("confidence") else ""
            out.append(f"- {p['name']}, {p['position'] or 'title unknown'}: `{p['email']}`{conf}{li}")
        pattern = cfg["_patterns"].get(company)
        if pattern:
            out.append(f"- Email pattern at this company: `{pattern}`")
        dept = cjobs[0].department
        links = linkedin_people_links(company, dept)
        out.append("- LinkedIn: " + " · ".join(f"[{k}]({v})" for k, v in links.items()))
        out.append("")

    # ---- boards with no public API ----
    manual = cfg.get("manual", [])
    q = cfg.get("search", {}).get("query", "backend engineer")
    loc = cfg.get("search", {}).get("location", "Berlin")
    if manual or q:
        out += ["## Check by hand", "",
                "Companies without a public job API, plus LinkedIn Jobs. Links are pre-filled.", ""]
        out.append(f"- **LinkedIn Jobs** (past week): [{q} in {loc}]({linkedin_jobs_link(q, loc, 7)})")
        for m in manual:
            url = m["url"].replace("{query}", quote_plus(q)).replace("{location}", quote_plus(loc))
            out.append(f"- **{m['name']}**: {url}")
        out.append("")

    path.write_text("\n".join(out))
