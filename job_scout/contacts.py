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
    def __init__(self, cache_dir: Path, ttl_days: int = 30, api_key: str = None):
        self.key = api_key or os.environ.get("HUNTER_API_KEY")
        self.cache_dir = cache_dir
        self.ttl = ttl_days * 86400
        self.errors = []

    @property
    def enabled(self) -> bool:
        return bool(self.key)

    def _cached(self, domain: str):
        p = self.cache_dir / f"{domain}.json"
        if p.exists() and time.time() - p.stat().st_mtime < self.ttl:
            return json.loads(p.read_text())
        return None

    def recruiters(self, domain: str, limit: int = 5) -> list[dict]:
        if not domain:
            return []
        data = self._cached(domain)
        if data is None:
            if not self.enabled:
                return []
            q = urllib.parse.urlencode({"domain": domain, "department": "hr", "limit": 10,
                                        "api_key": self.key})
            try:
                req = urllib.request.Request(f"{HUNTER_URL}?{q}", headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=20) as r:
                    data = json.load(r).get("data") or {}
            except urllib.error.HTTPError as e:
                self.errors.append(f"hunter {domain}: HTTP {e.code}"
                                   + (" (quota exhausted?)" if e.code in (402, 429) else ""))
                return []
            except (urllib.error.URLError, TimeoutError) as e:
                self.errors.append(f"hunter {domain}: {e}")
                return []
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            (self.cache_dir / f"{domain}.json").write_text(json.dumps(data))
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
    def pattern(domain: str, cache_dir: Path) -> str:
        p = cache_dir / f"{domain}.json"
        if p.exists():
            return (json.loads(p.read_text()) or {}).get("pattern") or ""
        return ""
