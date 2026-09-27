from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

# Multipliers to annualise a salary figure.
INTERVAL_TO_YEAR = {"year": 1, "month": 12, "week": 52, "day": 230, "hour": 2080}


@dataclass
class Salary:
    min: Optional[float]
    max: Optional[float]
    currency: str
    interval: str = "year"
    origin: str = "structured"  # "structured" from the ATS, "parsed" from description text

    def to_eur_year(self, fx: dict) -> tuple[Optional[float], Optional[float]]:
        rate = fx.get(self.currency.upper())
        if rate is None:
            return None, None
        mult = INTERVAL_TO_YEAR.get(self.interval, 1) * rate
        lo = self.min * mult if self.min is not None else None
        hi = self.max * mult if self.max is not None else None
        return lo, hi

    def display(self) -> str:
        def fmt(v):
            return f"{v / 1000:.0f}k" if v >= 1000 else f"{v:.0f}"
        parts = [fmt(v) for v in (self.min, self.max) if v is not None]
        rng = " – ".join(dict.fromkeys(parts))
        suffix = "" if self.interval == "year" else f"/{self.interval}"
        note = " (parsed)" if self.origin == "parsed" else ""
        return f"{self.currency} {rng}{suffix}{note}"


@dataclass
class Job:
    company: str
    title: str
    location: str
    url: str
    source: str
    job_id: str
    description: str = ""
    department: str = ""
    published: Optional[datetime] = None
    remote: Optional[bool] = None
    salary: Optional[Salary] = None
    # Filled in by the filter/scoring stage.
    score: int = 0
    matched: list = field(default_factory=list)
    years_required: Optional[int] = None
    location_reason: str = ""
    is_new: bool = False
    variants: int = 1  # >1 when the same role was posted once per country

    @property
    def key(self) -> str:
        return f"{self.source}:{self.company}:{self.job_id}"
