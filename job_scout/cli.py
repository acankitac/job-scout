import argparse
import json
import sys
import tomllib
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

from .aggregators import fetch_generic
from .contacts import Hunter
from .filters import Criteria, _any, company_key, dedupe, evaluate, prefilter
from .country import DEFAULT_MIN_BY_COUNTRY, job_country, minimum_for
from .market import MarketData
from .report import write_csv, write_markdown
from .experience import judge_experience
from .llm import build_router
from .research import ExperienceResearch, SalaryResearch
from .salary import (Negotiation, Profile, judge_estimate, judge_listed, listed_salary_usable,
                     sibling_estimate)
from .sources import FETCHERS, SourceError

ROOT = Path(__file__).resolve().parent.parent


def parse_args(argv):
    p = argparse.ArgumentParser(prog="scout", description="Find matching jobs across the job market.")
    p.add_argument("-c", "--config", default=str(ROOT / "config.toml"))
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--companies", action="store_true",
                      help="search only the [[companies]] job boards in the config, not the whole market")
    mode.add_argument("--all-sources", action="store_true",
                      help="search the whole market and the [[companies]] job boards")
    p.add_argument("--query", action="append", help="override [generic] queries (repeatable)")
    p.add_argument("--where", action="append", help="override [generic] where (repeatable)")
    p.add_argument("--only", action="append", metavar="COMPANY", help="restrict to these companies (repeatable)")
    p.add_argument("--location", action="append", help="override criteria.locations (repeatable)")
    p.add_argument("--min-salary", type=float, help="minimum annual salary in EUR")
    p.add_argument("--max-salary", type=float, help="maximum annual salary in EUR")
    p.add_argument("--require-salary", action="store_true", help="drop jobs that don't list a salary")
    p.add_argument("--max-age", type=int, metavar="DAYS", help="only jobs posted in the last N days")
    p.add_argument("--new-only", action="store_true", help="only jobs not seen in a previous run")
    p.add_argument("--no-contacts", action="store_true", help="skip Hunter.io email lookups")
    p.add_argument("--research", action="store_true",
                   help="estimate unlisted salaries by web research even without a salary bound")
    p.add_argument("--no-research", action="store_true", help="never run paid salary web research")
    p.add_argument("--top", type=int, default=0, help="keep only the N highest-scoring jobs")
    p.add_argument("--explain", action="store_true", help="print why jobs were rejected")
    p.add_argument("--out", default=str(ROOT / "reports"))
    return p.parse_args(argv)


def load_config(path: str) -> dict:
    if not Path(path).exists():
        raise SystemExit(f"{path} not found. Start from the template:\n"
                         f"  cp config.example.toml config.toml")
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    cfg.setdefault("companies", [])
    for c in cfg["companies"]:
        if c.get("ats") not in FETCHERS:
            raise SystemExit(f"{path}: company {c.get('name')!r} has unknown ats {c.get('ats')!r}; "
                             f"use one of {', '.join(FETCHERS)}")
    return cfg


def _prefetch(res, jobs, fn, domains, workers):
    """Run paid lookups in parallel, one per cache key, so later calls are cache hits."""
    if not res.enabled or not jobs:
        return
    reps = {}
    for j in jobs:
        reps.setdefault(res.key(j), j)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(lambda j: fn(j, domains.get(j.company, "")), reps.values()))


def _report_research(res, what):
    for e in res.errors:
        print(f"  ! {e}", file=sys.stderr)
    if res.lookups:
        print(f"{what} research: {res.lookups} web lookups this run", file=sys.stderr)
    if res.skipped and res.router.available:
        print(f"{what} research: {res.skipped} roles not researched, a per-run cap was reached "
              f"(rerun to continue; finished lookups are cached)", file=sys.stderr)


def _unverified_status(j, crit, neg, profile, market, high_payers, threshold):
    """For a role with no listed or estimated salary: how plausible is your minimum?"""
    if company_key(j.company) in high_payers:
        return "high payer"
    stat = market.stat(j, profile) if market else None
    if stat is None or not j.min_eur:
        return "unverified"
    j.market = stat
    j.market_share_above_min = 100 - stat.percentile(j.min_eur)
    # A typical offer this far up the market, plus negotiation, would reach your minimum.
    needed = j.min_eur / (1 + neg.default_headroom_pct / 100)
    return "market unlikely" if stat.percentile(needed) > threshold else "market plausible"


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    cfg = load_config(args.config)
    crit = Criteria.from_dict(cfg.get("criteria", {}))
    if args.location:
        crit.locations = args.location
    if args.min_salary is not None:  # one minimum for every country, this run only
        crit.salary_min_eur = args.min_salary
        crit.salary_min_by_country, crit.use_default_minimums = {}, False
    if args.max_salary is not None:
        crit.salary_max_eur = args.max_salary
    if args.require_salary:
        crit.require_salary = True
    if args.max_age is not None:
        crit.max_age_days = args.max_age

    use_market = not args.companies
    use_boards = args.companies or args.all_sources
    companies = cfg["companies"] if use_boards else []
    if use_boards and not companies:
        raise SystemExit(f"{args.config}: --companies needs [[companies]] entries in the config")
    if args.only and companies:
        wanted = {o.lower() for o in args.only}
        companies = [c for c in companies if c["name"].lower() in wanted]
        if not companies:
            raise SystemExit(f"--only matched no configured company: {', '.join(args.only)}")

    # ---- fetch ----
    all_jobs, failures = [], []
    gcfg = dict(cfg.get("generic", {}))
    if args.query:
        gcfg["queries"] = args.query
    if args.where:
        gcfg["where"] = args.where
    with ThreadPoolExecutor(max_workers=8) as pool:
        market = pool.submit(fetch_generic, gcfg) if use_market else None
        futures = {pool.submit(FETCHERS[c["ats"]], c["name"], c["token"]): c for c in companies}
        for fut in as_completed(futures):
            c = futures[fut]
            try:
                all_jobs.extend(fut.result())
            except SourceError as e:
                failures.append((c["name"], str(e)))
        if market:
            jobs, fails = market.result()
            all_jobs.extend(jobs)
            failures.extend(fails)
    if args.only and use_market:
        wanted = {o.lower() for o in args.only}
        all_jobs = [j for j in all_jobs if j.company.lower() in wanted or any(w in j.company.lower() for w in wanted)]
    by_source = Counter(j.source for j in all_jobs)
    print(f"fetched {len(all_jobs)} postings from {len({j.company for j in all_jobs})} employers "
          f"({', '.join(f'{s} {n}' for s, n in by_source.most_common())})", file=sys.stderr)
    for name, err in failures:
        print(f"  ! {name}: {err}", file=sys.stderr)

    # ---- fetch full descriptions where search results didn't include them ----
    need = [j for j in all_jobs if j.loader and prefilter(j, crit) is None]
    cap = gcfg.get("max_detail_fetches", 200)
    if len(need) > cap:
        print(f"  ! {len(need) - cap} postings not opened, max_detail_fetches ({cap}) reached", file=sys.stderr)
    detail_errors = 0
    with ThreadPoolExecutor(max_workers=6) as pool:
        for fut in [pool.submit(j.loader) for j in need[:cap]]:
            try:
                fut.result()
            except SourceError:
                detail_errors += 1
    if detail_errors:
        print(f"  ! {detail_errors} job descriptions couldn't be fetched; judged on title only", file=sys.stderr)

    # ---- filter ----
    matches, reasons = [], Counter()
    for job in all_jobs:
        why = evaluate(job, crit)
        if why is None:
            matches.append(job)
        else:
            reasons[why.split(":")[0]] += 1
            if args.explain:
                print(f"  - {job.company}: {job.title} [{job.location}] -> {why}", file=sys.stderr)

    before = len(matches)
    matches = dedupe(matches)
    if before != len(matches):
        print(f"collapsed {before - len(matches)} per-country duplicate postings", file=sys.stderr)

    # ---- new-since-last-run ----
    state_path = ROOT / ".state" / "seen.json"
    seen = json.loads(state_path.read_text()) if state_path.exists() else {}
    today = date.today().isoformat()
    for j in matches:
        j.is_new = j.key not in seen
    if args.new_only:
        matches = [j for j in matches if j.is_new]

    profile = Profile.from_dict(cfg.get("profile", {}))
    domains = {c["name"]: c.get("domain", "") for c in cfg["companies"]}
    # Best matches first, so research spends any per-run cap on the roles most worth it.
    matches.sort(key=lambda j: (-j.score, j.company, j.title))
    by_country = dict(DEFAULT_MIN_BY_COUNTRY) if crit.use_default_minimums else {}
    by_country.update(crit.salary_min_by_country or {})
    crit.effective_minimums = by_country
    for j in matches:
        j.country = job_country(j.location, profile.country)
        j.min_eur = minimum_for(j.country, by_country, crit.salary_min_eur)

    # ---- experience: from the JD, else researched for the role's level ----
    rcfg = cfg.get("research", {})
    router = build_router(rcfg)  # shared, so a backend found unavailable is skipped for both
    workers = max(1, int(rcfg.get("concurrency", 4)))
    xp = ExperienceResearch(rcfg, profile, ROOT / ".cache" / "experience", router=router)
    xp_bounded = bool(crit.max_years_required or crit.min_years_expected)
    if args.no_research or not xp_bounded or not rcfg.get("experience", True):
        xp.disabled = True  # cached results are still used; no new paid lookups
    _prefetch(xp, [j for j in matches if j.yoe is None], xp.expected, domains, workers)
    kept = []
    for j in matches:
        if j.yoe is None:
            j.yoe = xp.expected(j, domains.get(j.company, ""))  # cache hit after prefetch
        why = judge_experience(j, j.yoe, crit.max_years_required, crit.min_years_expected)
        if why:
            reasons["experience"] += 1
            if args.explain:
                print(f"  - {j.company}: {j.title} [{j.location}] -> {why}", file=sys.stderr)
        else:
            kept.append(j)
    matches = kept
    for n in router.notices:
        print(f"  ! research: {n}", file=sys.stderr)
    shown = len(router.notices)
    _report_research(xp, "experience")
    unknown = sum(1 for j in matches if j.yoe is None)
    if xp_bounded and unknown:
        hint = "" if xp.enabled or args.no_research else " (no research backend available; see [research])"
        print(f"{unknown} roles don't state years of experience; kept{hint}", file=sys.stderr)

    # ---- salary: listed, estimated, high payer, market check ----
    neg = Negotiation.from_dict(cfg.get("negotiation", {}))
    scfg = cfg.get("salary_check", {})
    bounded = bool(crit.salary_max_eur or any(j.min_eur for j in matches))
    research = SalaryResearch(rcfg, profile, crit.fx_to_eur, ROOT / ".cache" / "salary", router=router)
    if args.no_research or not (bounded or args.research):
        research.disabled = True  # cached results are still used; no new paid lookups
    is_role = lambda title: not crit.title_include or _any(crit.title_include, title)
    high_payers = {company_key(n) for n in scfg.get("high_payers", [])}
    market = MarketData(ROOT / ".cache" / "market") if scfg.get("market_data", True) else None
    threshold = scfg.get("unlikely_above_percentile", 75)
    mode = scfg.get("unverified", "separate")

    for j in matches:  # company-band estimates are free; do them before paying for research
        if not listed_salary_usable(j, profile):
            j.estimate = sibling_estimate(j, all_jobs, profile, crit.fx_to_eur, is_role)
    _prefetch(research, [j for j in matches if not listed_salary_usable(j, profile) and not j.estimate],
              research.estimate, domains, workers)

    kept, side = [], []
    for j in matches:
        why = None
        if listed_salary_usable(j, profile):
            why = judge_listed(j, j.min_eur, crit.salary_max_eur, crit.fx_to_eur, neg)
            j.salary_status = "listed"
        elif crit.require_salary:
            why = "salary: not listed"
        else:
            j.estimate = j.estimate or research.estimate(j, domains.get(j.company, ""))
            if j.estimate:
                why = judge_estimate(j, j.estimate, j.min_eur, crit.salary_max_eur, neg)
                j.salary_status = "estimated"
            elif bounded:
                j.salary_status = _unverified_status(j, crit, neg, profile, market, high_payers, threshold)
        if why:
            reasons["salary"] += 1
            if args.explain:
                print(f"  - {j.company}: {j.title} [{j.location}] -> {why}", file=sys.stderr)
        elif j.salary_status in ("market unlikely", "unverified") and mode != "keep":
            if mode == "drop":
                reasons["salary"] += 1
            else:
                side.append(j)
            if args.explain:
                print(f"  - {j.company}: {j.title} [{j.location}] -> salary: {j.salary_status}", file=sys.stderr)
        else:
            kept.append(j)
    matches = kept
    _report_research(research, "salary")
    if market:
        for e in market.errors:
            print(f"  ! {e}", file=sys.stderr)
    if side:
        n_unlikely = sum(j.salary_status == "market unlikely" for j in side)
        print(f"{len(side)} roles moved to 'Salary not verified' ({n_unlikely} unlikely to reach your minimum "
              f"by market data, {len(side) - n_unlikely} with no data)", file=sys.stderr)

    for j in matches:
        seen.setdefault(j.key, today)
    state_path.parent.mkdir(exist_ok=True)
    state_path.write_text(json.dumps(seen, indent=0, sort_keys=True))

    matches.sort(key=lambda j: (-j.score, j.company, j.title))
    side.sort(key=lambda j: (-j.score, j.company, j.title))
    if args.top:
        matches = matches[: args.top]

    for n in router.notices[shown:]:
        print(f"  ! research: {n}", file=sys.stderr)
    if router.used:
        print(f"research backends used: {', '.join(sorted(router.used))}", file=sys.stderr)

    # ---- contacts ----
    hcfg = cfg.get("hunter", {})
    hunter = Hunter(ROOT / ".cache" / "hunter", ttl_days=hcfg.get("cache_days", 30),
                    max_lookups=hcfg.get("max_lookups_per_run", 10))
    contacts, patterns = {}, {}
    if not args.no_contacts:
        for company in dict.fromkeys(j.company for j in matches):  # best-scoring employers first
            contacts[company] = hunter.recruiters(domains.get(company, ""),
                                                  limit=cfg.get("hunter", {}).get("per_company", 5),
                                                  company=company)
            patterns[company] = Hunter.pattern(domains.get(company, ""), hunter.cache_dir, company)
    cfg["_patterns"] = patterns
    for e in hunter.errors:
        print(f"  ! {e}", file=sys.stderr)

    # ---- write ----
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    md, csv_path = out / f"jobs-{today}.md", out / f"jobs-{today}.csv"
    write_markdown(md, matches, side, cfg, crit, neg, contacts, failures,
                   {"fetched": len(all_jobs), "companies": len({j.company for j in all_jobs})},
                   hunter.enabled and not args.no_contacts)
    write_csv(csv_path, matches + side, crit.fx_to_eur, neg)

    print(f"{len(matches)} matches ({sum(j.is_new for j in matches)} new)", file=sys.stderr)
    top_reasons = ", ".join(f"{r} ({n})" for r, n in reasons.most_common(4))
    if top_reasons:
        print(f"rejected mostly for: {top_reasons}", file=sys.stderr)
    print(md)
    print(csv_path)
    return 0
