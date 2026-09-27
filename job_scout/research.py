"""Salary research for roles that list no pay, using Claude with web search.

Searches are restricted to an allowlist of salary-data sites plus the company's own domain,
so estimates rest on sources you'd trust yourself. Results are cached per
(company, level, country) and the number of paid lookups per run is capped.

Needs ANTHROPIC_API_KEY. Without it, this module does nothing and those roles are kept,
marked "no estimate".
"""
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from .models import Job
from .salary import Estimate, Profile, infer_level

API_URL = "https://api.anthropic.com/v1/messages"

DEFAULT_DOMAINS = [
    "levels.fyi", "glassdoor.com", "glassdoor.de", "glassdoor.co.uk", "kununu.com",
    "stepstone.de", "gehalt.de", "indeed.com", "payscale.com", "teamblind.com",
    "arbeitsagentur.de",
]

PROMPT = """You are estimating pay for a job posting that does not list a salary.

Company: {company}
Posting title: {title}
Posting location(s): {location}
Inferred seniority from the title: {level}
Candidate: {years} years of professional software engineering experience, based in {where}.
Estimate for employment in {country}, paid in {currency}.

Posting excerpt:
\"\"\"{excerpt}\"\"\"

Do this:
1. Work out which of {company}'s internal levels this posting and experience correspond to
   (e.g. "L4", "Senior Engineer II"), if the company uses named levels.
2. Search the allowed salary sources for {company}'s annual BASE salary (gross, excluding bonus
   and equity) at that level in {country}. Prefer data points from the last two years.
3. If {company}-specific data for {country} is thin, use {company}'s data for a nearby market or
   comparable companies in {country}, lower the confidence, and say so in notes.
4. Assess negotiation room: does {company} use fixed or formula-based pay (little room), or are
   offers commonly negotiated up, and by roughly how much on base?

Reply with ONLY a JSON object, no prose, in exactly this shape:
{{"currency": "EUR", "level": "company level name", "base_low": 0, "base_typical": 0,
  "base_high": 0, "negotiation_room": "low|medium|high", "confidence": "low|medium|high",
  "notes": "one or two sentences: what the numbers rest on and any caveats",
  "sources": [{{"title": "...", "url": "..."}}]}}"""


class ResearchError(Exception):
    pass


class SalaryResearch:
    def __init__(self, cfg: dict, profile: Profile, fx: dict, cache_dir: Path, api_key: str = None):
        self.cfg = cfg
        self.profile = profile
        self.fx = fx
        self.cache_dir = cache_dir
        self.key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self.model = cfg.get("model", "claude-sonnet-5")
        self.tool = cfg.get("tool_version", "web_search_20250305")
        self.max_searches = cfg.get("max_searches_per_lookup", 5)
        self.max_lookups = cfg.get("max_lookups_per_run", 20)
        self.ttl = cfg.get("cache_days", 30) * 86400
        self.domains = cfg.get("trusted_domains") or DEFAULT_DOMAINS
        self.lookups = 0
        self.skipped = 0
        self.errors = []

    @property
    def enabled(self) -> bool:
        return bool(self.key) and self.cfg.get("enabled", True)

    def _cache_path(self, company: str, level: str) -> Path:
        k = f"{company}|{level}|{self.profile.country}".lower()
        slug = re.sub(r"[^a-z0-9]+", "-", k)[:60]
        return self.cache_dir / f"{slug}-{hashlib.sha1(k.encode()).hexdigest()[:8]}.json"

    def estimate(self, job: Job, domain: str = "") -> Optional[Estimate]:
        level = infer_level(job.title)
        path = self._cache_path(job.company, level)
        if path.exists() and time.time() - path.stat().st_mtime < self.ttl:
            return self._to_estimate(json.loads(path.read_text()), level)
        if not self.enabled:
            return None
        if self.lookups >= self.max_lookups:
            self.skipped += 1
            return None
        self.lookups += 1
        try:
            data = self._ask(job, level, domain)
        except ResearchError as e:
            self.errors.append(f"salary research {job.company} ({level}): {e}")
            return None
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=1))
        return self._to_estimate(data, level)

    # ---- API ----

    def _post(self, body: dict) -> dict:
        req = urllib.request.Request(API_URL, data=json.dumps(body).encode(), method="POST", headers={
            "x-api-key": self.key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            raise ResearchError(f"HTTP {e.code}: {detail}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            raise ResearchError(str(e)) from None

    def _ask(self, job: Job, level: str, domain: str) -> dict:
        p = self.profile
        prompt = PROMPT.format(
            company=job.company, title=job.title, location=job.location[:300], level=level,
            years=p.years_experience, where=", ".join(filter(None, [p.city, p.country])),
            country=p.country, currency=p.currency, excerpt=job.description[:1500])
        domains = list(dict.fromkeys(self.domains + ([domain] if domain else [])))
        messages = [{"role": "user", "content": prompt}]
        body = {"model": self.model, "max_tokens": 3000, "messages": messages,
                "tools": [{"type": self.tool, "name": "web_search", "max_uses": self.max_searches,
                           "allowed_domains": domains}]}
        found, text = [], ""
        for _ in range(4):  # server-side tool use can pause long turns; resume them
            resp = self._post(body)
            for block in resp.get("content", []):
                if block.get("type") == "text":
                    text += block.get("text", "")
                elif block.get("type") == "web_search_tool_result":
                    for r in block.get("content") or []:
                        if isinstance(r, dict) and r.get("url"):
                            found.append({"title": r.get("title", ""), "url": r["url"]})
            if resp.get("stop_reason") != "pause_turn":
                break
            messages.append({"role": "assistant", "content": resp["content"]})
        data = parse_json_object(text)
        if data is None:
            raise ResearchError("model did not return a JSON estimate")
        if not data.get("sources"):
            data["sources"] = found[:5]
        return data

    # ---- validation ----

    def _to_estimate(self, data: dict, level: str) -> Optional[Estimate]:
        rate = self.fx.get(str(data.get("currency", "")).upper())
        if rate is None:
            return None

        def num(k):
            v = data.get(k)
            return float(v) * rate if isinstance(v, (int, float)) and v > 0 else None

        low, typ, high = num("base_low"), num("base_typical"), num("base_high")
        if typ is None and low and high:
            typ = (low + high) / 2
        if typ is None or not (15_000 <= typ <= 1_500_000):
            return None
        room = str(data.get("negotiation_room", "unknown")).lower()
        return Estimate(
            low=low or typ, typical=typ, high=high, level=str(data.get("level") or level),
            basis="web research", confidence=str(data.get("confidence", "low")).lower(),
            room=room if room in ("low", "medium", "high") else "unknown",
            notes=str(data.get("notes", ""))[:400],
            sources=[s for s in data.get("sources") or [] if isinstance(s, dict) and s.get("url")][:5])


def parse_json_object(text: str) -> Optional[dict]:
    text = re.sub(r"```(?:json)?", "", text or "")
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None
