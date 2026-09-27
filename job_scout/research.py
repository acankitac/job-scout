"""Web research for what a posting doesn't say: pay, and the experience a role expects.

Uses Claude with web search, restricted to an allowlist of salary and career-data sites plus
the company's own domain, so answers rest on sources you'd trust yourself. Results are cached
and the number of paid lookups per run is capped.

Runs on Amazon Bedrock and/or the Anthropic API (see llm.py), tried in the configured order.
With neither available nothing is looked up, and roles are kept rather than rejected on a guess.
"""
import hashlib
import json
import re
import threading
import time
from pathlib import Path
from typing import Optional

from .models import Job
from .experience import YoeEstimate, title_stem
from .llm import CallFailed, Router, build_router
from .salary import Estimate, Profile, infer_level

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


EXPERIENCE_PROMPT = """You are working out how much professional experience a job posting expects.
The posting does not state a years-of-experience requirement.

Company: {company}
Posting title: {title}
Posting location(s): {location}

Posting excerpt:
\"\"\"{excerpt}\"\"\"

Do this:
1. Work out which of {company}'s internal levels this posting corresponds to (e.g. "L4",
   "Senior Engineer II"). Titles can mislead: some companies give every engineer the same
   title (e.g. "Member of Technical Staff"), so use the responsibilities and scope in the excerpt.
2. Search the allowed sources for the years of professional experience engineers at that level
   at {company} typically have. levels.fyi reports years of experience per level; the company's
   own career or leveling pages are also authoritative.
3. If {company}-specific data is thin, use industry norms for that level, lower the confidence,
   and say so in notes.

Reply with ONLY a JSON object, no prose, in exactly this shape:
{{"level": "company level name", "years_min": 0, "years_typical": 0, "years_max": 0,
  "confidence": "low|medium|high",
  "notes": "one or two sentences: what the range rests on and any caveats",
  "sources": [{{"title": "...", "url": "..."}}]}}"""


class ResearchError(Exception):
    pass


class WebResearch:
    """Shared plumbing: backends, cache, per-run cap, and the web-search conversation."""
    label = "research"
    cap_key = "max_lookups_per_run"

    def __init__(self, cfg: dict, profile: Profile, cache_dir: Path, api_key: str = None,
                 router: Router = None):
        self.cfg = cfg
        self.profile = profile
        self.cache_dir = cache_dir
        self.router = router or build_router(cfg, api_key)
        self.disabled = False  # set to stop paid lookups; cached answers are still used
        self.tool = cfg.get("tool_version", "web_search_20250305")
        self.max_searches = cfg.get("max_searches_per_lookup", 5)
        self.max_lookups = cfg.get(self.cap_key, 0)  # 0 = no overall limit; see Router caps
        self.ttl = cfg.get("cache_days", 30) * 86400
        self.domains = cfg.get("trusted_domains") or DEFAULT_DOMAINS
        self.lookups = 0
        self.skipped = 0
        self.errors = []
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return not self.disabled and self.router.available and self.cfg.get("enabled", True)

    def _cache_path(self, *parts: str) -> Path:
        k = "|".join(parts).lower()
        slug = re.sub(r"[^a-z0-9]+", "-", k)[:60]
        return self.cache_dir / f"{slug}-{hashlib.sha1(k.encode()).hexdigest()[:8]}.json"

    def _lookup(self, path: Path, prompt: str, domain: str, what: str) -> Optional[dict]:
        """Cached answer if fresh; otherwise a paid lookup, if enabled and under the cap."""
        if path.exists():
            cached = json.loads(path.read_text())
            # Answers given without a live search are kept only briefly, so a later run with a
            # search-capable backend replaces them.
            ttl = self.ttl if cached.get("_searched", True) else 3 * 86400
            if time.time() - path.stat().st_mtime < ttl:
                return cached
        if not self.enabled:
            return None
        with self._lock:
            if (self.max_lookups and self.lookups >= self.max_lookups) or self.router.exhausted:
                self.skipped += 1
                return None
            self.lookups += 1
        try:
            data = self._ask(prompt, domain)
        except ResearchError as e:
            with self._lock:
                self.lookups -= 1  # count only lookups that returned an answer
                # Backend went down or hit its cap mid-run: the router already reported why.
                if self.router.exhausted or not self.router.available:
                    self.skipped += 1
                    return None
            self.errors.append(f"{self.label} {what}: {e}")
            return None
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=1))
        return data

    def _ask(self, prompt: str, domain: str) -> dict:
        domains = list(dict.fromkeys(self.domains + ([domain] if domain else [])))
        messages = [{"role": "user", "content": prompt}]
        body = {"max_tokens": 3000, "messages": messages,
                "tools": [{"type": self.tool, "name": "web_search", "max_uses": self.max_searches,
                           "allowed_domains": domains}]}
        found, text = [], ""
        try:
            resp, backend, searched = self.router.create(body)
        except CallFailed as e:
            raise ResearchError(str(e)) from None
        for turn in range(4):  # server-side tool use can pause long turns; resume them
            if turn:
                try:
                    resp = self.router.resume(backend, body, searched)
                except CallFailed as e:
                    raise ResearchError(str(e)) from None
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
            raise ResearchError("model did not return a JSON answer")
        if not data.get("sources"):
            data["sources"] = found[:5]
        data["_backend"], data["_searched"] = backend, searched
        return data

    @staticmethod
    def _basis(data: dict) -> str:
        return "web research" if data.get("_searched", True) else "model knowledge (no web search)"

    @staticmethod
    def _confidence(data: dict) -> str:
        c = str(data.get("confidence", "low")).lower()
        return c if data.get("_searched", True) else "low"

    @staticmethod
    def _sources(data: dict) -> list:
        return [s for s in data.get("sources") or [] if isinstance(s, dict) and s.get("url")][:5]


class SalaryResearch(WebResearch):
    """Base-salary range and negotiation room, cached per (company, level, country)."""
    label = "salary research"

    def __init__(self, cfg: dict, profile: Profile, fx: dict, cache_dir: Path, api_key: str = None,
                 router: Router = None):
        super().__init__(cfg, profile, cache_dir, api_key, router)
        self.fx = fx

    def key(self, job: Job) -> tuple:
        return (job.company, infer_level(job.title))

    def estimate(self, job: Job, domain: str = "") -> Optional[Estimate]:
        level = infer_level(job.title)
        p = self.profile
        prompt = PROMPT.format(
            company=job.company, title=job.title, location=job.location[:300], level=level,
            years=p.years_experience, where=", ".join(filter(None, [p.city, p.country])),
            country=p.country, currency=p.currency, excerpt=job.description[:1500])
        data = self._lookup(self._cache_path(job.company, level, p.country), prompt, domain,
                            f"{job.company} ({level})")
        return self._to_estimate(data, level) if data else None

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
            basis=self._basis(data), confidence=self._confidence(data),
            room=room if room in ("low", "medium", "high") else "unknown",
            notes=str(data.get("notes", ""))[:400], sources=self._sources(data))


class ExperienceResearch(WebResearch):
    """Typical years of experience for a role's level, cached per (company, title stem)."""
    label = "experience research"
    cap_key = "max_experience_lookups_per_run"

    def key(self, job: Job) -> tuple:
        return (job.company, title_stem(job.title))

    def expected(self, job: Job, domain: str = "") -> Optional[YoeEstimate]:
        stem = title_stem(job.title)
        prompt = EXPERIENCE_PROMPT.format(company=job.company, title=job.title,
                                          location=job.location[:300], excerpt=job.description[:2500])
        data = self._lookup(self._cache_path("yoe", job.company, stem), prompt, domain,
                            f"{job.company} ({stem})")
        return self._to_yoe(data) if data else None

    def _to_yoe(self, data: dict) -> Optional[YoeEstimate]:
        def num(k):
            v = data.get(k)
            return int(v) if isinstance(v, (int, float)) and 0 <= v <= 30 else None

        lo, typ, hi = num("years_min"), num("years_typical"), num("years_max")
        if lo is None and typ is None:
            return None
        lo = lo if lo is not None else typ
        if hi is not None and hi < lo:
            hi = None
        return YoeEstimate(min=lo, max=hi, typical=typ, level=str(data.get("level") or ""),
                           basis=self._basis(data), confidence=self._confidence(data),
                           notes=str(data.get("notes", ""))[:400], sources=self._sources(data))


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
