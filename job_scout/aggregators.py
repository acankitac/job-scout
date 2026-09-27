"""Market-wide job search, without a company list.

  arbeitsagentur  Germany's federal job board (Bundesagentur für Arbeit). Covers most jobs posted
                  in Germany, often with a structured salary range. Official public API, no key.
  arbeitnow       Aggregated tech and English-speaking jobs in Germany/Europe. Public API, no key.
                  No server-side search, so pages are fetched and filtered locally.
  remotive        Remote jobs worldwide, searchable. Public API, no key.
  adzuna          Large aggregator with salary data. Needs a free key: ADZUNA_APP_ID, ADZUNA_APP_KEY.

Arbeitsagentur search results carry no description, so each Job gets a `loader` that fetches
the full posting; the CLI calls it only for jobs that survive the cheap title/location filter.
"""
import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from .models import Job, Salary
from .sources import UA, SourceError, _dt, get_json
from .textutil import html_to_text, parse_salary

BA_BASE = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service"
# Public client id the Bundesagentur publishes for its own job-search apps.
BA_HEADERS = {"X-API-Key": "jobboerse-jobsuche", "User-Agent": UA, "Accept": "application/json"}
_BA_INTERVAL = {"JAHRESGEHALT": "year", "MONATSGEHALT": "month", "STUNDENLOHN": "hour"}
_COUNTRY = {"DEUTSCHLAND": "Germany", "ÖSTERREICH": "Austria", "SCHWEIZ": "Switzerland",
            "NIEDERLANDE": "Netherlands", "FRANKREICH": "France", "POLEN": "Poland"}


def _ba_get(path: str, params: Optional[dict] = None, retries: int = 2) -> dict:
    url = f"{BA_BASE}/{path}" + (f"?{urllib.parse.urlencode(params)}" if params else "")
    last = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=BA_HEADERS), timeout=25) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504):
                raise SourceError(f"arbeitsagentur HTTP {e.code}") from None
            last = f"HTTP {e.code}"
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = str(e)
        time.sleep(2 * (attempt + 1))
    raise SourceError(f"arbeitsagentur: {last}")


def _ba_location(j: dict) -> str:
    parts = []
    for loc in j.get("stellenlokationen") or []:
        a = loc.get("adresse") or {}
        land = _COUNTRY.get((a.get("land") or "").upper(), (a.get("land") or "").title())
        parts.append(", ".join(filter(None, [a.get("ort"), land])))
    loc = "; ".join(dict.fromkeys(p for p in parts if p))
    if j.get("homeofficemoeglich"):
        loc += " (home office possible)"
    return loc


def _ba_salary(j: dict) -> Optional[Salary]:
    interval = _BA_INTERVAL.get(j.get("verguetungsangabe") or "", "year")
    lo, hi = j.get("gehaltsspanneVon"), j.get("gehaltsspanneBis")
    if lo or hi:
        return Salary(lo, hi, "EUR", interval)
    if j.get("festgehalt"):
        return Salary(j["festgehalt"], j["festgehalt"], "EUR", interval)
    return None


def _ba_loader(job: Job, refnr: str):
    def load():
        b64 = base64.b64encode(refnr.encode()).decode()
        d = _ba_get(f"pc/v4/jobdetails/{b64}")
        job.description = html_to_text(d.get("stellenangebotsBeschreibung") or "")
        if not job.salary:
            job.salary = parse_salary(job.description)
    return load


def arbeitsagentur(queries: list, where: list, radius_km: int = 30, max_pages: int = 3) -> list[Job]:
    jobs, seen = [], set()
    for q in queries:
        for w in where or [""]:
            for page in range(1, max_pages + 1):
                params = {"was": q, "angebotsart": 1, "size": 100, "page": page}
                if w:
                    params.update(wo=w, umkreis=radius_km)
                items = _ba_get("pc/v6/jobs", params).get("ergebnisliste") or []
                for j in items:
                    ref = j.get("referenznummer")
                    if not ref or ref in seen:
                        continue
                    seen.add(ref)
                    job = Job(
                        company=(j.get("firma") or "").strip(), title=(j.get("stellenangebotsTitel") or "").strip(),
                        location=_ba_location(j),
                        url=j.get("externeURL") or f"https://www.arbeitsagentur.de/jobsuche/jobdetail/{ref}",
                        source="arbeitsagentur", job_id=ref,
                        published=_dt((j.get("veroeffentlichungszeitraum") or {}).get("von")
                                      or j.get("datumErsteVeroeffentlichung")),
                        salary=_ba_salary(j), department=j.get("hauptberuf") or "")
                    job.loader = _ba_loader(job, ref)
                    jobs.append(job)
                if len(items) < 100:
                    break
    return jobs


def arbeitnow(max_pages: int = 5, delay: float = 1.5) -> list[Job]:
    jobs = []
    for page in range(1, max_pages + 1):
        if page > 1:
            time.sleep(delay)  # the API rate-limits rapid paging
        try:
            d = get_json(f"https://www.arbeitnow.com/api/job-board-api?page={page}", retries=3)
        except SourceError as e:
            if jobs:  # keep what we have if a later page is throttled
                break
            raise SourceError(f"arbeitnow: {e}") from None
        for j in d.get("data") or []:
            desc = html_to_text(j.get("description") or "")
            remote = bool(j.get("remote"))
            loc = j.get("location") or ""
            jobs.append(Job(
                company=(j.get("company_name") or "").strip(), title=(j.get("title") or "").strip(),
                location=loc + (" (remote)" if remote and "remote" not in loc.lower() else ""),
                url=j.get("url", ""), source="arbeitnow", job_id=j.get("slug", ""),
                description=desc, remote=remote or None,
                published=_dt(j["created_at"] * 1000) if isinstance(j.get("created_at"), (int, float)) else None,
                salary=parse_salary(desc), department=", ".join(j.get("tags") or [])))
        if not (d.get("links") or {}).get("next"):
            break
    return jobs


def remotive(queries: list) -> list[Job]:
    jobs, seen = [], set()
    for q in queries:
        q = urllib.parse.quote(q)
        d = get_json(f"https://remotive.com/api/remote-jobs?category=software-dev&search={q}")
        for j in d.get("jobs") or []:
            if j.get("id") in seen:
                continue
            seen.add(j.get("id"))
            desc = html_to_text(j.get("description") or "")
            jobs.append(Job(
                company=(j.get("company_name") or "").strip(), title=(j.get("title") or "").strip(),
                location="Remote - " + (j.get("candidate_required_location") or "unspecified"),
                url=j.get("url", ""), source="remotive", job_id=str(j.get("id")),
                description=desc, published=_dt(j.get("publication_date")), remote=True,
                salary=parse_salary(j.get("salary") or "") or parse_salary(desc)))
    return jobs


def adzuna(queries: list, where: list, country: str = "de", max_pages: int = 2) -> list[Job]:
    app_id, app_key = os.environ.get("ADZUNA_APP_ID"), os.environ.get("ADZUNA_APP_KEY")
    if not (app_id and app_key):
        return []
    jobs, seen = [], set()
    for q in queries:
        for w in where or [""]:
            for page in range(1, max_pages + 1):
                params = {"app_id": app_id, "app_key": app_key, "what": q, "results_per_page": 50,
                          "content-type": "application/json"}
                if w:
                    params["where"] = w
                url = f"https://api.adzuna.com/v1/api/jobs/{country}/search/{page}?" + urllib.parse.urlencode(params)
                results = get_json(url).get("results") or []
                for j in results:
                    if j.get("id") in seen:
                        continue
                    seen.add(j.get("id"))
                    lo, hi = j.get("salary_min"), j.get("salary_max")
                    predicted = str(j.get("salary_is_predicted")) == "1"
                    salary = None
                    if (lo or hi) and not predicted:  # Adzuna's own predictions aren't the employer's
                        salary = Salary(lo, hi, "EUR" if country in ("de", "nl", "fr", "at", "es", "it") else "GBP")
                    jobs.append(Job(
                        company=((j.get("company") or {}).get("display_name") or "").strip(),
                        title=(j.get("title") or "").strip(),
                        location=(j.get("location") or {}).get("display_name", ""),
                        url=j.get("redirect_url", ""), source="adzuna", job_id=str(j.get("id")),
                        description=html_to_text(j.get("description") or ""),
                        published=_dt(j.get("created")), salary=salary))
                if len(results) < 50:
                    break
    return jobs


def fetch_generic(gcfg: dict) -> tuple[list[Job], list[tuple[str, str]]]:
    """Run every configured aggregator. Returns (jobs, failures)."""
    from concurrent.futures import ThreadPoolExecutor
    queries = gcfg.get("queries") or ["backend engineer"]
    where = gcfg.get("where") or []
    calls = {
        "arbeitsagentur": lambda: arbeitsagentur(queries, where, gcfg.get("radius_km", 30), gcfg.get("max_pages", 3)),
        "arbeitnow": lambda: arbeitnow(gcfg.get("arbeitnow_pages", 5)),
        "remotive": lambda: remotive(queries),
        "adzuna": lambda: adzuna(queries, where, gcfg.get("adzuna_country", "de"), gcfg.get("max_pages", 3)),
    }
    wanted = gcfg.get("sources") or list(calls)
    jobs, failures = [], []
    with ThreadPoolExecutor(max_workers=len(wanted) or 1) as pool:
        futures = {}
        for name in wanted:
            if name not in calls:
                failures.append((name, "unknown source"))
                continue
            futures[name] = pool.submit(calls[name])
        for name, fut in futures.items():
            try:
                jobs.extend(fut.result())
            except SourceError as e:
                failures.append((name, str(e)))
    return jobs, failures
