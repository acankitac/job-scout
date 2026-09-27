# job-scout

**A personal job-search tool that checks dozens of company job boards at once, filters them
against your criteria, ranks what's left against your skills, and tells you who to contact.**

One command, about fifteen seconds, and you get a ranked report of every open role that fits,
with direct application links, salary where the company lists it, and the recruiters behind it.

Illustrative run:

```
$ ./scout.py
fetched 3120 postings from 12/12 companies
collapsed 6 per-country duplicate postings
31 matches (31 new)
reports/jobs-2026-09-27.md
```

| | Score | Company | Role | Location | Salary |
|---|---|---|---|---|---|
| 🆕 | 28 | Company A | Senior Backend Engineer (Java), Metadata Platform | Munich, Germany | not listed |
| 🆕 | 22 | Company B | Senior Software Engineer, Backend | Berlin, Germany | not listed |
| 🆕 | 22 | Company C | Senior Platform Engineer, Event Streaming (Kafka) | Berlin, Germany | not listed |
| 🆕 | 19 | Company D | Senior Backend Engineer | Germany; Portugal; France; ... | EUR 90k – 160k |

## Features

- **Any company on Greenhouse, Ashby or Lever.** Most tech companies hire through one of
  these three; adding one takes four lines of config.
- **Filters that match how job hunting actually works:** city, remote-within-region, salary
  range (converted to EUR), and role type.
- **Experience fit from the job description, not the title.** "Staff" and "Lead" mean different
  things at different companies, so years of experience are read from the description; when it
  doesn't say, the company's typical experience for that level is researched.
- **Salary estimates for roles that don't list pay.** From the company's own published bands
  where possible, otherwise by web research on trusted salary sites. Every role is judged by
  what you could reach **with negotiation**, and 🤝 marks the ones that need it.
- **Ranking against your own skills.** Weighted keywords, doubled when they appear in the title.
- **Only what's new.** Every run remembers what you've already seen; `--new-only` shows the rest.
- **Recruiter contacts.** Emails and titles via [Hunter.io](https://hunter.io), ranked with
  recruiters first, plus ready-made LinkedIn searches for each company's recruiters and
  engineering managers.
- **De-duplication.** One role posted in five countries shows up once.
- **Zero dependencies for the core.** Standard-library Python 3.11+. Research runs on Amazon
  Bedrock (needs `boto3`) or the Anthropic API, with automatic fallback between them.

## Quick start

```bash
git clone https://github.com/acankitac/job-scout.git
cd job-scout
cp config.example.toml config.toml   # then add your companies and criteria
./scout.py
open reports/jobs-*.md
```

`config.toml` is git-ignored, so your target list stays on your machine.
**[INSTRUCTIONS.md](INSTRUCTIONS.md)** walks through setup, every option, and common recipes.

## How it works

```
config.toml ──► fetch ──► filter ──► de-duplicate ──► experience ──► salary ────────────► contacts ──► report
                Greenhouse  location   one role,          from the JD,    listed range        Hunter.io   .md
                Ashby       role type  many postings      else researched or estimate:        LinkedIn    .csv
                Lever       keywords                      for the level   company bands,      links
                                                                          web research,
                                                                          + negotiation
```

Most tech companies publish their open roles through an applicant tracking system, and the
three big ones (Greenhouse, Ashby, Lever) expose an **official public JSON feed** of every
listed job. job-scout reads those feeds directly. That means structured data, salary ranges
when companies provide them, and nothing that breaks when a careers page is redesigned.

Companies with their own career sites and no public feed can be listed in the config and
appear in the report as pre-filled search links.

## Responsible use

- **LinkedIn is never scraped.** Its terms prohibit automated access, and it restricts accounts
  that do it. The report gives you LinkedIn search links to open yourself instead.
- **Web research only searches trusted salary and career-data sites** plus the company's own domain, and
  every estimate in the report links its sources.
- **Recruiter data is personal data.** Hunter.io results are cached locally, git-ignored, and
  meant for your own applications only.
- **Polite by default.** One request per company per run, with retries and back-off.

## Project layout

```
scout.py              entry point
config.example.toml   template; copy to config.toml (git-ignored)
job_scout/
  sources.py          Greenhouse / Ashby / Lever fetchers
  filters.py          criteria, scoring, de-duplication
  salary.py           salary judgement, company-band estimates, negotiation headroom
  experience.py       experience fit from the JD or researched level expectations
  research.py         salary and experience web research (Claude + web search, trusted sites only)
  llm.py              research backends: Amazon Bedrock, Anthropic API, with fallback
  textutil.py         salary and experience parsing from description text
  contacts.py         Hunter.io lookup and LinkedIn links
  report.py           Markdown and CSV output
tests/                unit tests (python3 -m unittest discover -s tests -t .)
```
