"""Private job portals: LinkedIn, jobs.ch (Switzerland), Jobindex (Denmark).

How each is read, and on what terms:

  jobindex  Official RSS feeds for searches, which Jobindex publishes for exactly this use.
  jobs.ch   The public search and job pages, which embed the listings. robots.txt allows these;
            its internal /api/ is disallowed, so it is not used.
  linkedin  The logged-out public job search that LinkedIn serves to anyone without an account.
            No login, cookies or account are involved, so your LinkedIn account is never at risk.
            LinkedIn's robots.txt and terms do not permit automated access, so this source is
            opt-in: enable it knowingly, keep volumes low, and expect it to stop working if
            LinkedIn blocks it (the run reports that and carries on with the other sources).

All requests identify the tool honestly, go one at a time per site with a pause between them,
and stop for that site on the first throttling response.
"""
import html
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from typing import Optional

from .models import Job
from .sources import UA, SourceError, _dt
from .textutil import html_to_text, parse_salary


class Throttled(SourceError):
    pass


class Site:
    """One site's polite HTTP client: serial requests, a minimum gap, stop on throttling."""

    def __init__(self, name: str, gap_s: float):
        self.name = name
        self.gap = gap_s
        self._lock = threading.Lock()
        self._last = 0.0
        self.blocked = None  # reason, once the site has throttled or refused us

    def get(self, url: str) -> str:
        if self.blocked:
            raise Throttled(f"{self.name}: {self.blocked}")
        with self._lock:
            wait = self._last + self.gap - time.time()
            if wait > 0:
                time.sleep(wait)
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en"})
                with urllib.request.urlopen(req, timeout=25) as r:
                    return r.read().decode("utf-8", errors="replace")
            except urllib.error.HTTPError as e:
                if e.code in (403, 429, 999):  # 999 is LinkedIn's "go away"
                    self.blocked = f"HTTP {e.code}, stopped for this run"
                    raise Throttled(f"{self.name}: {self.blocked}") from None
                raise SourceError(f"{self.name}: HTTP {e.code}") from None
            except (urllib.error.URLError, TimeoutError) as e:
                raise SourceError(f"{self.name}: {e}") from None
            finally:
                self._last = time.time()


def _text(s: str) -> str:
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or ""))).strip()


# ---------- LinkedIn ----------

_LI_SEARCH = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
_LI_DETAIL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{}"


def _li_cards(page: str) -> list[dict]:
    out = []
    for card in page.split("<li>")[1:]:
        m_id = re.search(r"urn:li:jobPosting:(\d+)", card)
        if not m_id:
            continue
        def grab(pattern):
            m = re.search(pattern, card, re.S)
            return _text(m.group(1)) if m else ""
        url = re.search(r'class="base-card__full-link[^"]*"\s+href="([^"?]+)', card)
        out.append({
            "id": m_id.group(1),
            "title": grab(r'base-search-card__title">(.*?)</h3>'),
            "company": grab(r'base-search-card__subtitle">(.*?)</h4>'),
            "location": grab(r'job-search-card__location">(.*?)</span>'),
            "date": (re.search(r'datetime="([\d-]+)"', card) or [None, None])[1],
            "url": url.group(1) if url else f"https://www.linkedin.com/jobs/view/{m_id.group(1)}",
        })
    return out


def _li_loader(site: Site, job: Job):
    def load():
        page = site.get(_LI_DETAIL.format(job.job_id))
        m = re.search(r'show-more-less-html__markup[^>]*>(.*?)</div>', page, re.S)
        crit = re.findall(r'job-criteria__subheader">(.*?)</h3>\s*<span[^>]*>(.*?)</span>', page, re.S)
        extra = "\n".join(f"{_text(k)}: {_text(v)}" for k, v in crit)
        job.description = "\n".join(filter(None, [html_to_text(m.group(1)) if m else "", extra]))
        job.salary = job.salary or parse_salary(job.description)
    return load


def linkedin(queries: list, places: list, days: int = 7, pages: int = 2, site: Optional[Site] = None) -> list[Job]:
    site = site or Site("linkedin", 2.0)
    jobs, seen = [], set()
    for q in queries:
        for place in places:
            for page in range(pages):
                params = {"keywords": q, "location": place, "start": page * 10}
                if days:
                    params["f_TPR"] = f"r{days * 86400}"
                try:
                    cards = _li_cards(site.get(f"{_LI_SEARCH}?{urllib.parse.urlencode(params)}"))
                except Throttled:
                    if jobs:
                        return jobs  # keep what we have; the CLI reports the block
                    raise
                for c in cards:
                    if c["id"] in seen:
                        continue
                    seen.add(c["id"])
                    job = Job(company=c["company"], title=c["title"], location=c["location"], url=c["url"],
                              source="linkedin", job_id=c["id"], published=_dt(c["date"]))
                    job.loader = _li_loader(site, job)
                    jobs.append(job)
                if len(cards) < 10:
                    break
    return jobs


# ---------- jobs.ch ----------

_JCH_SEARCH = "https://www.jobs.ch/en/vacancies/"
_JCH_DETAIL = "https://www.jobs.ch/en/vacancies/detail/{}/"


def _init_state(page: str) -> dict:
    i = page.find("__INIT__")
    if i < 0:
        raise SourceError("jobs.ch: page layout changed (no __INIT__ state)")
    return json.JSONDecoder().raw_decode(page[page.index("{", i):])[0]


def _jch_location(v: dict) -> str:
    parts = []
    for loc in v.get("locations") or []:
        # jobs.ch sometimes swaps fields (city "Switzerland", postalCode "Zürich").
        for k in ("city", "postalCode", "place"):
            val = (loc.get(k) or "").strip()
            if val and not val.isdigit() and val.lower() != "switzerland":
                parts.append(val)
    place = (v.get("place") or "").strip()
    if place and place.lower() != "switzerland":
        parts.append(place)
    return ", ".join(dict.fromkeys(parts)) + (", Switzerland" if parts else "Switzerland")


def _jch_loader(site: Site, job: Job):
    def load():
        page = site.get(_JCH_DETAIL.format(job.job_id))
        m = re.search(r'data-cy="vacancy-description"[^>]*>(.*?)</div>\s*</div>', page, re.S)
        job.description = html_to_text(m.group(1)) if m else ""
        job.salary = job.salary or parse_salary(job.description)
    return load


def jobs_ch(queries: list, places: list, pages: int = 2, site: Optional[Site] = None) -> list[Job]:
    site = site or Site("jobs.ch", 1.5)
    jobs, seen = [], set()
    for q in queries:
        for place in places or [""]:
            for page in range(1, pages + 1):
                params = {"term": q, "page": page}
                if place:
                    params["location"] = place
                state = _init_state(site.get(f"{_JCH_SEARCH}?{urllib.parse.urlencode(params)}"))
                results = (((state.get("vacancy") or {}).get("results") or {}).get("main") or {}).get("results") or []
                for v in results:
                    if v.get("id") in seen or v.get("isActive") is False:
                        continue
                    seen.add(v.get("id"))
                    job = Job(company=((v.get("company") or {}).get("name") or "").strip(),
                              title=(v.get("title") or "").strip(), location=_jch_location(v),
                              url=_JCH_DETAIL.format(v["id"]), source="jobs.ch", job_id=v["id"],
                              published=_dt(v.get("publicationDate") or v.get("initialPublicationDate")))
                    job.loader = _jch_loader(site, job)
                    jobs.append(job)
                if len(results) < 20:
                    break
    return jobs


# ---------- Jobindex ----------

_JI_RSS = "https://www.jobindex.dk/jobsoegning.rss"


def jobindex(queries: list, pages: int = 2, site: Optional[Site] = None) -> list[Job]:
    site = site or Site("jobindex", 1.5)
    jobs, seen = [], set()
    for q in queries:
        for page in range(1, pages + 1):
            params = {"q": f'"{q}"', "page": page}  # quoted: all words, not any
            try:
                root = ET.fromstring(site.get(f"{_JI_RSS}?{urllib.parse.urlencode(params)}"))
            except ET.ParseError:
                raise SourceError("jobindex: feed could not be parsed") from None
            items = root.findall(".//item")
            for it in items:
                guid = (it.findtext("guid") or it.findtext("link") or "").strip()
                if not guid or guid in seen:
                    continue
                seen.add(guid)
                raw = it.findtext("description") or ""
                area = re.search(r'jix_robotjob--area">(.*?)</span>', raw, re.S)
                title, _, company = (it.findtext("title") or "").rpartition(", ")
                try:
                    published = parsedate_to_datetime(it.findtext("pubDate") or "")
                except (TypeError, ValueError):
                    published = None
                desc = html_to_text(raw)
                jobs.append(Job(company=company.strip(), title=(title or company).strip(),
                                location=(_text(area.group(1)) + ", Denmark") if area else "Denmark",
                                url=(it.findtext("link") or guid).strip(), source="jobindex",
                                job_id=guid.rsplit("/", 1)[-1], description=desc, published=published,
                                salary=parse_salary(desc)))
            if len(items) < 20:
                break
    return jobs


def fetch_portals(pcfg: dict, queries: list) -> tuple[list[Job], list[tuple[str, str]]]:
    """Run every enabled portal in parallel (each one serial internally). Returns (jobs, failures)."""
    from concurrent.futures import ThreadPoolExecutor
    q = pcfg.get("queries") or queries
    calls = {
        "linkedin": lambda: linkedin(q, pcfg.get("linkedin_places") or ["Germany"], pcfg.get("linkedin_days", 7),
                                     pcfg.get("linkedin_pages", 2)),
        "jobs.ch": lambda: jobs_ch(q, pcfg.get("jobs_ch_places") or [""], pcfg.get("pages", 2)),
        "jobindex": lambda: jobindex(q, pcfg.get("pages", 2)),
    }
    wanted = [s for s in pcfg.get("enabled", []) if s in calls]
    unknown = [s for s in pcfg.get("enabled", []) if s not in calls]
    jobs, failures = [], [(s, "unknown portal") for s in unknown]
    if not wanted:
        return jobs, failures
    with ThreadPoolExecutor(max_workers=len(wanted)) as pool:
        futures = {name: pool.submit(calls[name]) for name in wanted}
        for name, fut in futures.items():
            try:
                jobs.extend(fut.result())
            except SourceError as e:
                failures.append((name, str(e)))
    return jobs, failures
