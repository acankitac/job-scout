"""Which country a job is in, its currency, and your salary minimum there.

A salary minimum only means something relative to a country: €120k is an ordinary senior salary
in Zurich and an exceptional one in Warsaw. So every salary check uses the minimum for the job's
country, from [criteria.salary_min_by_country], falling back to the built-in defaults below,
then to salary_min_eur for countries in neither.
"""
import re
from typing import Optional

# Annual base salary minimums in EUR, used when the config doesn't set a country.
DEFAULT_MIN_BY_COUNTRY = {
    "Germany": 115000, "Switzerland": 160000, "Denmark": 110000,
    "Netherlands": 115000, "Ireland": 115000, "Poland": 80000,
}

CURRENCY = {"Switzerland": "CHF", "Denmark": "DKK", "Poland": "PLN", "Sweden": "SEK", "Norway": "NOK",
            "United Kingdom": "GBP", "Czechia": "CZK", "Hungary": "HUF", "United States": "USD"}

# Country names (English and local) and major tech cities, lowercase -> canonical English name.
_NAMES = {
    "Germany": ["germany", "deutschland"], "Switzerland": ["switzerland", "schweiz", "suisse", "svizzera"],
    "Denmark": ["denmark", "danmark"], "Netherlands": ["netherlands", "the netherlands", "nederland", "holland"],
    "Ireland": ["ireland", "éire"], "Poland": ["poland", "polska"], "Austria": ["austria", "österreich"],
    "France": ["france"], "Spain": ["spain", "españa"], "Portugal": ["portugal"], "Italy": ["italy", "italia"],
    "Sweden": ["sweden", "sverige"], "Norway": ["norway", "norge"], "Finland": ["finland", "suomi"],
    "Belgium": ["belgium", "belgië", "belgique"], "Luxembourg": ["luxembourg"], "Czechia": ["czechia", "czech republic"],
    "Estonia": ["estonia", "eesti"], "Hungary": ["hungary"], "United Kingdom": ["united kingdom", "uk", "england", "scotland"],
    "United States": ["united states", "usa", "u.s."],
}
_CITIES = {
    "Germany": ["berlin", "munich", "münchen", "hamburg", "frankfurt", "cologne", "köln", "düsseldorf", "stuttgart",
                "leipzig", "dresden", "hannover", "nuremberg", "nürnberg", "karlsruhe", "bonn", "potsdam", "bavaria"],
    "Switzerland": ["zurich", "zürich", "geneva", "genève", "basel", "bern", "lausanne", "zug"],
    "Denmark": ["copenhagen", "københavn", "aarhus", "odense"],
    "Netherlands": ["amsterdam", "rotterdam", "utrecht", "eindhoven", "the hague", "den haag", "delft"],
    "Ireland": ["dublin", "cork", "galway", "limerick"],
    "Poland": ["warsaw", "warszawa", "krakow", "kraków", "wroclaw", "wrocław", "gdansk", "gdańsk", "poznan", "poznań"],
    "Austria": ["vienna", "wien", "graz", "linz"], "France": ["paris", "lyon", "bordeaux", "toulouse", "nantes"],
    "Spain": ["madrid", "barcelona", "valencia"], "Portugal": ["lisbon", "lisboa", "porto"],
    "Italy": ["milan", "milano", "rome", "roma", "turin"], "Sweden": ["stockholm", "gothenburg", "malmö"],
    "Norway": ["oslo", "bergen"], "Finland": ["helsinki", "espoo", "tampere"], "Belgium": ["brussels", "antwerp", "ghent"],
    "Czechia": ["prague", "praha", "brno"], "Estonia": ["tallinn", "tartu"], "Hungary": ["budapest"],
    "United Kingdom": ["london", "manchester", "edinburgh", "cambridge", "oxford"],
    "United States": ["san francisco", "new york", "seattle", "palo alto", "mountain view", "austin", "boston"],
}
_LOOKUP = [(w, c) for c, ws in list(_NAMES.items()) + list(_CITIES.items()) for w in ws]
_LOOKUP.sort(key=lambda wc: -len(wc[0]))  # "the netherlands" before "netherlands"


def countries_in(location: str) -> list[str]:
    """Countries named or implied by a location string, in order of first mention."""
    loc = (location or "").lower()
    hits = []
    for word, country in _LOOKUP:
        m = re.search(r"(?<![a-zà-ÿ])" + re.escape(word) + r"(?![a-zà-ÿ])", loc)
        if m:
            hits.append((m.start(), country))
    return list(dict.fromkeys(c for _, c in sorted(hits)))


def job_country(location: str, home: str) -> str:
    """The country a job would employ you in. Your home country if the posting lists it
    (or names no country at all, e.g. "Remote - Europe"); otherwise the first one listed."""
    found = countries_in(location)
    if not found or home in found:
        return home
    return found[0]


def currency_of(country: str) -> str:
    return CURRENCY.get(country, "EUR")


def minimum_for(country: str, by_country: dict, fallback_eur: float) -> float:
    for table in (by_country or {}, DEFAULT_MIN_BY_COUNTRY):
        for k, v in table.items():
            if k.lower() == country.lower():
                return float(v)
    return float(fallback_eur or 0)
