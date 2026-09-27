import argparse
import json
import sys
import tomllib
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

from .contacts import Hunter
from .filters import Criteria, dedupe, evaluate
from .report import write_csv, write_markdown
from .sources import FETCHERS, SourceError

ROOT = Path(__file__).resolve().parent.parent


def parse_args(argv):
    p = argparse.ArgumentParser(prog="scout", description="Find matching jobs on company job boards.")
    p.add_argument("-c", "--config", default=str(ROOT / "config.toml"))
    p.add_argument("--only", action="append", metavar="COMPANY", help="restrict to these companies (repeatable)")
    p.add_argument("--location", action="append", help="override criteria.locations (repeatable)")
    p.add_argument("--min-salary", type=float, help="minimum annual salary in EUR")
    p.add_argument("--max-salary", type=float, help="maximum annual salary in EUR")
    p.add_argument("--require-salary", action="store_true", help="drop jobs that don't list a salary")
    p.add_argument("--max-age", type=int, metavar="DAYS", help="only jobs posted in the last N days")
    p.add_argument("--new-only", action="store_true", help="only jobs not seen in a previous run")
    p.add_argument("--no-contacts", action="store_true", help="skip Hunter.io email lookups")
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
    if not cfg.get("companies"):
        raise SystemExit(f"{path}: no [[companies]] configured")
    for c in cfg["companies"]:
        if c.get("ats") not in FETCHERS:
            raise SystemExit(f"{path}: company {c.get('name')!r} has unknown ats {c.get('ats')!r}; "
                             f"use one of {', '.join(FETCHERS)}")
    return cfg


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    cfg = load_config(args.config)
    crit = Criteria.from_dict(cfg.get("criteria", {}))
    if args.location:
        crit.locations = args.location
    if args.min_salary is not None:
        crit.salary_min_eur = args.min_salary
    if args.max_salary is not None:
        crit.salary_max_eur = args.max_salary
    if args.require_salary:
        crit.require_salary = True
    if args.max_age is not None:
        crit.max_age_days = args.max_age

    companies = cfg["companies"]
    if args.only:
        wanted = {o.lower() for o in args.only}
        companies = [c for c in companies if c["name"].lower() in wanted]
        if not companies:
            raise SystemExit(f"--only matched no configured company: {', '.join(args.only)}")

    # ---- fetch ----
    all_jobs, failures = [], []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(FETCHERS[c["ats"]], c["name"], c["token"]): c for c in companies}
        for fut in as_completed(futures):
            c = futures[fut]
            try:
                all_jobs.extend(fut.result())
            except SourceError as e:
                failures.append((c["name"], str(e)))
    print(f"fetched {len(all_jobs)} postings from {len(companies) - len(failures)}/{len(companies)} companies",
          file=sys.stderr)
    for name, err in failures:
        print(f"  ! {name}: {err}", file=sys.stderr)

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
        seen.setdefault(j.key, today)
    state_path.parent.mkdir(exist_ok=True)
    state_path.write_text(json.dumps(seen, indent=0, sort_keys=True))
    if args.new_only:
        matches = [j for j in matches if j.is_new]

    matches.sort(key=lambda j: (-j.score, j.company, j.title))
    if args.top:
        matches = matches[: args.top]

    # ---- contacts ----
    hunter = Hunter(ROOT / ".cache" / "hunter", ttl_days=cfg.get("hunter", {}).get("cache_days", 30))
    domains = {c["name"]: c.get("domain", "") for c in cfg["companies"]}
    contacts, patterns = {}, {}
    if not args.no_contacts:
        for company in dict.fromkeys(j.company for j in matches):
            contacts[company] = hunter.recruiters(domains.get(company, ""),
                                                  limit=cfg.get("hunter", {}).get("per_company", 5))
            patterns[company] = Hunter.pattern(domains.get(company, ""), hunter.cache_dir)
    cfg["_patterns"] = patterns
    for e in hunter.errors:
        print(f"  ! {e}", file=sys.stderr)

    # ---- write ----
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    md, csv_path = out / f"jobs-{today}.md", out / f"jobs-{today}.csv"
    write_markdown(md, matches, cfg, crit, contacts, failures,
                   {"fetched": len(all_jobs), "companies": len(companies) - len(failures)},
                   hunter.enabled and not args.no_contacts)
    write_csv(csv_path, matches, crit.fx_to_eur)

    print(f"{len(matches)} matches ({sum(j.is_new for j in matches)} new)", file=sys.stderr)
    top_reasons = ", ".join(f"{r} ({n})" for r, n in reasons.most_common(4))
    if top_reasons:
        print(f"rejected mostly for: {top_reasons}", file=sys.stderr)
    print(md)
    print(csv_path)
    return 0
