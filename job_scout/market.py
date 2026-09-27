"""Market pay baseline from official German statistics (Bundesagentur für Arbeit, Entgeltatlas).

For an employer with no salary data of its own, this answers: how much of the market pays at
least your minimum, for this kind of role and region? It can't say what one company pays, only
how unusual your minimum is, which is what decides whether an unknown employer is a long shot.

The Entgeltatlas reports gross monthly pay of full-time employees by occupation (KldB code),
skill level and region: median, 25th and 75th percentile. Figures above the social-insurance
ceiling are withheld (reported as -2), which happens for senior software roles in Berlin and
Bavaria. Median and 25th percentile are always published, so the distribution is modelled as
lognormal from those, which fits wage data well.
"""
import json
import math
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .models import Job
from .salary import infer_level
from .sources import UA

API = "https://rest.arbeitsagentur.de/infosysbub/entgeltatlas/pc/v1/entgelte"
HEADERS = {"X-API-Key": "infosysbub-ega", "User-Agent": UA, "Accept": "application/json"}

# KldB 4341x: software development. Skill level: 2 Fachkraft, 3 Spezialist, 4 Experte.
LEVEL_TO_KLDB = {"junior": ("43412", "Fachkraft"), "mid": ("43413", "Spezialist"),
                 "senior": ("43414", "Experte"), "lead": ("43414", "Experte"),
                 "staff": ("43414", "Experte"), "principal": ("43414", "Experte")}

REGIONS = {"Deutschland": 1, "Schleswig-Holstein": 4, "Hamburg": 5, "Niedersachsen": 6, "Bremen": 7,
           "Nordrhein-Westfalen": 8, "Hessen": 9, "Rheinland-Pfalz": 10, "Baden-Württemberg": 11,
           "Bayern": 12, "Saarland": 13, "Berlin": 14, "Brandenburg": 15, "Mecklenburg-Vorpommern": 16,
           "Sachsen": 17, "Sachsen-Anhalt": 18, "Thüringen": 19}
_CITY = {
    "berlin": "Berlin", "potsdam": "Brandenburg", "hamburg": "Hamburg", "bremen": "Bremen",
    "munich": "Bayern", "münchen": "Bayern", "muenchen": "Bayern", "nürnberg": "Bayern",
    "nuremberg": "Bayern", "augsburg": "Bayern", "regensburg": "Bayern", "ingolstadt": "Bayern", "bavaria": "Bayern",
    "frankfurt": "Hessen", "wiesbaden": "Hessen", "darmstadt": "Hessen", "kassel": "Hessen",
    "köln": "Nordrhein-Westfalen", "cologne": "Nordrhein-Westfalen", "düsseldorf": "Nordrhein-Westfalen",
    "dortmund": "Nordrhein-Westfalen", "essen": "Nordrhein-Westfalen", "bonn": "Nordrhein-Westfalen",
    "aachen": "Nordrhein-Westfalen", "münster": "Nordrhein-Westfalen", "bochum": "Nordrhein-Westfalen",
    "stuttgart": "Baden-Württemberg", "karlsruhe": "Baden-Württemberg", "mannheim": "Baden-Württemberg",
    "heidelberg": "Baden-Württemberg", "freiburg": "Baden-Württemberg", "ulm": "Baden-Württemberg",
    "leipzig": "Sachsen", "dresden": "Sachsen", "chemnitz": "Sachsen", "hannover": "Niedersachsen",
    "hanover": "Niedersachsen", "braunschweig": "Niedersachsen", "kiel": "Schleswig-Holstein",
    "mainz": "Rheinland-Pfalz", "saarbrücken": "Saarland", "erfurt": "Thüringen", "jena": "Thüringen",
    "magdeburg": "Sachsen-Anhalt", "halle": "Sachsen-Anhalt", "rostock": "Mecklenburg-Vorpommern",
}


@dataclass
class MarketStat:
    region: str
    level: str
    median: float           # EUR per year
    q25: float
    q75: Optional[float]    # None when withheld above the reporting ceiling
    ceiling: float          # EUR per year above which figures are withheld

    @property
    def sigma(self) -> float:
        s = [math.log(self.median / self.q25) / 0.6745]
        if self.q75:
            s.append(math.log(self.q75 / self.median) / 0.6745)
        return sum(s) / len(s)

    def percentile(self, annual_eur: float) -> float:
        """Share of the market (0-100) paid less than this."""
        z = (math.log(annual_eur) - math.log(self.median)) / self.sigma
        return 50 * (1 + math.erf(z / math.sqrt(2)))

    def value_at(self, pct: float) -> float:
        """Annual pay at a given market percentile (inverse of percentile())."""
        # Inverse normal CDF via bisection: precise enough and dependency-free.
        lo, hi = -6.0, 6.0
        for _ in range(60):
            mid = (lo + hi) / 2
            if 50 * (1 + math.erf(mid / math.sqrt(2))) < pct:
                lo = mid
            else:
                hi = mid
        return math.exp(math.log(self.median) + lo * self.sigma)

    def display(self) -> str:
        q75 = f"{self.q75 / 1000:.0f}k" if self.q75 else f">{self.ceiling / 1000:.0f}k"
        return f"€{self.q25 / 1000:.0f}k / {self.median / 1000:.0f}k / {q75}"


def region_for(job: Job, profile) -> Optional[str]:
    loc = (job.location or "").lower()
    # A posting open in several cities: use yours, since that's where you'd be paid.
    home = (profile.city or "").lower()
    if home and home in loc:
        return _CITY.get(home) or next((r for r in REGIONS if r.lower() == home), None)
    for name in REGIONS:
        if name.lower() in loc and name != "Deutschland":
            return name
    for city, region in _CITY.items():
        if city in loc:
            return region
    if profile.country.lower() == "germany" and profile.in_country(job.location or ""):
        return "Deutschland"
    return None  # outside Germany: no official data


class MarketData:
    def __init__(self, cache_dir: Path, ttl_days: int = 30):
        self.cache_dir = cache_dir
        self.ttl = ttl_days * 86400
        self.errors = []

    def _fetch(self, kldb: str, region_id: int) -> list:
        path = self.cache_dir / f"{kldb}-{region_id}.json"
        if path.exists() and time.time() - path.stat().st_mtime < self.ttl:
            return json.loads(path.read_text())
        req = urllib.request.Request(f"{API}/{kldb}?r={region_id}", headers=HEADERS)
        with urllib.request.urlopen(req, timeout=20) as r:
            rows = json.load(r)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rows))
        return rows

    def stat(self, job: Job, profile) -> Optional[MarketStat]:
        region = region_for(job, profile)
        if region is None:
            return None
        kldb, level = LEVEL_TO_KLDB.get(infer_level(job.title), LEVEL_TO_KLDB["mid"])
        try:
            rows = self._fetch(kldb, REGIONS[region])
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            msg = f"market data {kldb}/{region}: {e}"
            if msg not in self.errors:
                self.errors.append(msg)
            return None
        # The all-ages, all-genders, all-industries row.
        row = next((r for r in rows if r["ageCategory"]["id"] == 1 and r["gender"]["id"] == 1
                    and r["branche"]["id"] == 1), None)
        if not row or row.get("entgelt", -1) <= 0 or row.get("entgeltQ25", -1) <= 0:
            return None
        ceiling = (row.get("region") or {}).get("beitragsBemessungsGrenze") or 0
        q75 = row.get("entgeltQ75")
        return MarketStat(region=region, level=level, median=row["entgelt"] * 12, q25=row["entgeltQ25"] * 12,
                          q75=q75 * 12 if q75 and q75 > 0 else None, ceiling=ceiling * 12)
