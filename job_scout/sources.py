"""Fetchers for public ATS job-board APIs. Each returns a list of normalised Job objects.

These are the official, unauthenticated endpoints the ATS vendors publish for embedding job
boards, so no scraping or login is involved:

  Greenhouse  https://boards-api.greenhouse.io/v1/boards/<token>/jobs?content=true
  Ashby       https://api.ashbyhq.com/posting-api/job-board/<token>?includeCompensation=true
  Lever       https://api.lever.co/v0/postings/<token>?mode=json
"""
import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Optional

from .models import Job, Salary
from .textutil import html_to_text, parse_salary

UA = "job-scout/0.1 (personal job search tool)"


class SourceError(Exception):
    pass


def get_json(url: str, retries: int = 2, timeout: int = 25):
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise SourceError("board not found (404) - check the token") from None
            if e.code != 429 and e.code < 500:
                raise SourceError(f"HTTP {e.code}") from None
            last = f"HTTP {e.code}"
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = str(e)
        time.sleep(2 * (attempt + 1))
    raise SourceError(f"failed after {retries + 1} attempts: {last}")


def _dt(value) -> Optional[datetime]:
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):  # Lever: epoch milliseconds
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        # Date-only values ("2026-09-24") parse as naive; make every date comparable.
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, OSError):
        return None


def _join(parts) -> str:
    seen = {}
    for part in parts:
        for p in (part or "").split(";"):  # some boards pre-join locations with ";"
            p = re.sub(r"\s+", " ", p).strip()
            if p:
                seen.setdefault(p.lower(), p)
    return "; ".join(seen.values())


def _looks_remote(location: str) -> Optional[bool]:
    return True if "remote" in location.lower() else None


def greenhouse(company: str, token: str) -> list[Job]:
    data = get_json(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true")
    jobs = []
    for j in data.get("jobs", []):
        loc = _join([(j.get("location") or {}).get("name", "")]
                    + [o.get("name", "") for o in j.get("offices") or []])
        desc = html_to_text(j.get("content") or "")
        jobs.append(Job(
            company=company, title=j.get("title", "").strip(), location=loc,
            url=j.get("absolute_url", ""), source="greenhouse", job_id=str(j.get("id")),
            description=desc,
            department=_join(d.get("name", "") for d in j.get("departments") or []),
            published=_dt(j.get("first_published") or j.get("updated_at")),
            remote=_looks_remote(loc),
            salary=parse_salary(desc),
        ))
    return jobs


_ASHBY_INTERVAL = {"1 YEAR": "year", "1 MONTH": "month", "1 WEEK": "week", "1 DAY": "day", "1 HOUR": "hour"}


def _ashby_salary(comp: dict) -> Optional[Salary]:
    comps = [c for c in (comp or {}).get("summaryComponents") or []
             if c.get("compensationType") == "Salary" and c.get("currencyCode")]
    if not comps:
        return None
    mins = [c["minValue"] for c in comps if c.get("minValue") is not None]
    maxs = [c["maxValue"] for c in comps if c.get("maxValue") is not None]
    return Salary(min(mins) if mins else None, max(maxs) if maxs else None,
                  comps[0]["currencyCode"], _ASHBY_INTERVAL.get(comps[0].get("interval"), "year"))


def _ashby_loc(entry) -> str:
    if isinstance(entry, str):
        return entry
    if not isinstance(entry, dict):
        return ""
    addr = (entry.get("address") or {}).get("postalAddress") or {}
    return _join([entry.get("location", ""), addr.get("addressLocality", ""), addr.get("addressCountry", "")])


def ashby(company: str, token: str) -> list[Job]:
    data = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true")
    jobs = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        addr = (j.get("address") or {}).get("postalAddress") or {}
        loc = _join([j.get("location", ""), addr.get("addressLocality", ""), addr.get("addressCountry", "")]
                    + [_ashby_loc(s) for s in j.get("secondaryLocations") or []])
        desc = j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml") or "")
        remote = True if (j.get("isRemote") or (j.get("workplaceType") or "").lower() == "remote") else _looks_remote(loc)
        jobs.append(Job(
            company=company, title=j.get("title", "").strip(), location=loc,
            url=j.get("jobUrl", ""), source="ashby", job_id=str(j.get("id")),
            description=desc, department=_join([j.get("department", ""), j.get("team", "")]),
            published=_dt(j.get("publishedAt")), remote=remote,
            salary=_ashby_salary(j.get("compensation")) or parse_salary(desc),
        ))
    return jobs


_LEVER_INTERVAL = {"per-year-salary": "year", "per-month-salary": "month", "per-hour-wage": "hour",
                   "per-week-salary": "week", "per-day-wage": "day"}


def lever(company: str, token: str) -> list[Job]:
    data = get_json(f"https://api.lever.co/v0/postings/{token}?mode=json")
    jobs = []
    for j in data if isinstance(data, list) else []:
        cats = j.get("categories") or {}
        loc = _join([cats.get("location", "")] + list(cats.get("allLocations") or []))
        lists = "\n".join(f"{l.get('text', '')}\n{html_to_text(l.get('content', ''))}" for l in j.get("lists") or [])
        desc = "\n".join(filter(None, [j.get("descriptionPlain", ""), lists, j.get("additionalPlain", "")]))
        sr = j.get("salaryRange") or {}
        salary = (Salary(sr.get("min"), sr.get("max"), sr["currency"], _LEVER_INTERVAL.get(sr.get("interval"), "year"))
                  if sr.get("currency") else parse_salary(desc))
        wt = (j.get("workplaceType") or "").lower()
        jobs.append(Job(
            company=company, title=j.get("text", "").strip(), location=loc,
            url=j.get("hostedUrl", ""), source="lever", job_id=str(j.get("id")),
            description=desc, department=_join([cats.get("department", ""), cats.get("team", "")]),
            published=_dt(j.get("createdAt")),
            remote=True if wt == "remote" else _looks_remote(loc),
            salary=salary,
        ))
    return jobs


FETCHERS = {"greenhouse": greenhouse, "ashby": ashby, "lever": lever}
