"""Recruiter contact discovery.

Three sources, in order of reliability:
  1. Emails written into the job posting itself (rare, but exact when present).
  2. Hunter.io domain search, filtered to recruiting/HR titles. Needs HUNTER_API_KEY.
     Results are cached per domain so the free tier's monthly quota isn't burned on reruns.
  3. LinkedIn people-search links, opened by you in your own browser. LinkedIn is never
     fetched by this tool: automated access violates its terms and gets accounts restricted.
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .sources import UA

HUNTER_URL = "https://api.hunter.io/v2/domain-search"
RECRUITING = ("recruit", "talent", "sourc", "hiring", "people", "hr ", "human resources", "acquisition")


def linkedin_people_links(company: str, department: str = "") -> dict:
    def people(q):
        return "https://www.linkedin.com/search/results/people/?" + urllib.parse.urlencode(
            {"keywords": q, "origin": "GLOBAL_SEARCH_HEADER"})
    links = {
        "Technical recruiters": people(f"{company} technical recruiter"),
        "Talent acquisition": people(f"{company} talent acquisition engineering"),
    }
    team = (department.split(";")[0] or "engineering").strip()
    links["Engineering managers"] = people(f"{company} engineering manager {team}")
    return links


def linkedin_jobs_link(keywords: str, location: str, days: int = 7) -> str:
    params = {"keywords": keywords, "location": location}
    if days:
        params["f_TPR"] = f"r{days * 86400}"
    return "https://www.linkedin.com/jobs/search/?" + urllib.parse.urlencode(params)


class Hunter:
    def __init__(self, cache_dir: Path, ttl_days: int = 30, api_key: str = None, max_lookups: int = 10):
        self.key = api_key or os.environ.get("HUNTER_API_KEY")
        self.max_lookups = max_lookups  # paid lookups per run; the free plan has ~25 a month
        self.lookups = 0
        self.cache_dir = cache_dir
        self.ttl = ttl_days * 86400
        self.errors = []

    @property
    def enabled(self) -> bool:
        return bool(self.key)

    @staticmethod
    def _slug(domain: str, company: str) -> str:
        import re
        return domain or ("company-" + re.sub(r"[^a-z0-9]+", "-", company.lower()).strip("-"))

    def _cached(self, domain: str):
        p = self.cache_dir / f"{domain}.json"
        if p.exists() and time.time() - p.stat().st_mtime < self.ttl:
            return json.loads(p.read_text())
        return None

    def recruiters(self, domain: str, limit: int = 5, company: str = "") -> list[dict]:
        """Look up by domain, or by company name when the domain isn't known (Hunter resolves it)."""
        if not domain and not company:
            return []
        slug = self._slug(domain, company)
        data = self._cached(slug)
        if data is None:
            if not self.enabled or self.lookups >= self.max_lookups:
                return []
            self.lookups += 1
            target = {"domain": domain} if domain else {"company": company}
            q = urllib.parse.urlencode({**target, "department": "hr", "limit": 10, "api_key": self.key})
            try:
                req = urllib.request.Request(f"{HUNTER_URL}?{q}", headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=20) as r:
                    data = json.load(r).get("data") or {}
            except urllib.error.HTTPError as e:
                self.errors.append(f"hunter {slug}: HTTP {e.code}"
                                   + (" (quota exhausted?)" if e.code in (402, 429) else ""))
                return []
            except (urllib.error.URLError, TimeoutError) as e:
                self.errors.append(f"hunter {slug}: {e}")
                return []
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            (self.cache_dir / f"{slug}.json").write_text(json.dumps(data))
        people = []
        for e in data.get("emails") or []:
            pos = (e.get("position") or "").lower()
            people.append({
                "name": " ".join(filter(None, [e.get("first_name"), e.get("last_name")])) or "(unnamed)",
                "position": e.get("position") or "",
                "email": e.get("value"),
                "confidence": e.get("confidence"),
                "linkedin": e.get("linkedin"),
                "recruiting": any(k in pos + " " for k in RECRUITING),
            })
        # Recruiting titles first, then by Hunter's confidence score.
        people.sort(key=lambda p: (not p["recruiting"], -(p["confidence"] or 0)))
        return people[:limit]

    @staticmethod
    def pattern(domain: str, cache_dir: Path, company: str = "") -> str:
        p = cache_dir / f"{Hunter._slug(domain, company)}.json"
        if p.exists():
            d = json.loads(p.read_text()) or {}
            return f"{d['pattern']}@{d['domain']}" if d.get("pattern") and d.get("domain") else d.get("pattern") or ""
        return ""
