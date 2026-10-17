#!/usr/bin/env python3
"""
cron-overlap-guard
==================
Static analysis for scheduled jobs. Parses crontab-format entries and
GitHub Actions `on.schedule` cron triggers, computes the next 50 run times
per job, and detects:

  (a) OVERLAP — two jobs scheduled within X minutes of each other
      (default 5, tunable with --window) that can stampede shared resources.
  (b) INTERVAL RISK — a job whose cron interval is shorter than its declared
      max duration (annotate expected duration per job in the input).
  (c) IDEMPOTENCY GAP — heuristic check of the job's script for idempotency
      signals (lock files, idempotency keys, --if-not-exists, ...).
      Jobs with none get flagged.

Exit code 1 when any OVERLAP is found (usable as a CI gate).

Only the Python standard library is used. In particular the GitHub Actions
workflow parsing is a deliberately minimal regex-based schedule extraction —
documented honestly in the README — not a full YAML parser.
"""

import argparse
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Cron field parsing (stdlib only)
# ---------------------------------------------------------------------------
FIELD_RANGES = {"minute": (0, 59), "hour": (0, 23),
                "dom": (1, 31), "month": (1, 12), "dow": (0, 6)}
MONTH_NAMES = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
               "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
DOW_NAMES = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}
SPECIALS = {"@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *",
            "@monthly": "0 0 1 * *", "@weekly": "0 0 * * 0",
            "@daily": "0 0 * * *", "@midnight": "0 0 * * *",
            "@hourly": "0 * * * *"}


def _token_value(tok, field):
    tok = tok.strip().lower()
    if field == "month" and tok in MONTH_NAMES:
        return MONTH_NAMES[tok]
    if field == "dow" and tok in DOW_NAMES:
        return DOW_NAMES[tok]
    if field == "dow" and tok == "7":
        return 0
    return int(tok)


def parse_field(field, text):
    lo, hi = FIELD_RANGES[field]
    values = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            step = int(step_s)
            if step < 1:
                raise ValueError(f"bad step in cron field: {text!r}")
        if part == "*":
            start, end = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            start, end = _token_value(a, field), _token_value(b, field)
        else:
            start = end = _token_value(part, field)
        if not (lo <= start <= hi and lo <= end <= hi):
            raise ValueError(f"cron field out of range: {text!r}")
        values.update(range(start, end + 1, step))
    if not values:
        raise ValueError(f"empty cron field: {text!r}")
    return values


def parse_cron(expr):
    expr = expr.strip()
    if expr in SPECIALS:
        expr = SPECIALS[expr]
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError(f"cron expression must have 5 fields, got: {expr!r}")
    fields = ("minute", "hour", "dom", "month", "dow")
    return {f: parse_field(f, p) for f, p in zip(fields, parts)}


def _next_ge(values, current):
    for v in sorted(values):
        if v >= current:
            return v
    return None


def dom_dow_match(fields, dt):
    dom_star = fields["dom"] == set(range(1, 32))
    dow_star = fields["dow"] == set(range(0, 7))
    dom_ok = dt.day in fields["dom"]
    # Python weekday(): Mon=0..Sun=6 ; cron dow: Sun=0..Sat=6
    cron_dow = (dt.weekday() + 1) % 7
    dow_ok = cron_dow in fields["dow"]
    if dom_star and dow_star:
        return True
    if dom_star:
        return dow_ok
    if dow_star:
        return dom_ok
    return dom_ok or dow_ok  # classic Vixie cron OR semantics


def next_runs(expr, start, n=50, max_years=2):
    """Return the next n datetimes matching expr, strictly after `start`.

    Naive datetimes (no tz handling — document your timezone). Runs are
    computed with a field-stepping search so rare schedules (e.g. yearly)
    don't take a minute-by-minute walk.
    """
    fields = parse_cron(expr)
    t = start.replace(second=0, microsecond=0) + timedelta(minutes=1)
    limit = start + timedelta(days=365 * max_years)
    runs = []
    guard = 0
    while len(runs) < n and t <= limit and guard < 200000:
        guard += 1
        # minute
        if t.minute not in fields["minute"]:
            nxt = _next_ge(fields["minute"], t.minute)
            if nxt is None:
                t = t.replace(minute=0) + timedelta(hours=1)
            else:
                t = t.replace(minute=nxt)
            continue
        # hour
        if t.hour not in fields["hour"]:
            nxt = _next_ge(fields["hour"], t.hour)
            if nxt is None:
                t = (t + timedelta(days=1)).replace(hour=0, minute=0)
            else:
                t = t.replace(hour=nxt, minute=min(fields["minute"]))
            continue
        # month
        if t.month not in fields["month"]:
            y, m = t.year, t.month
            for _ in range(13):
                m += 1
                if m > 12:
                    m, y = 1, y + 1
                if m in fields["month"]:
                    break
            t = t.replace(year=y, month=m, day=1, hour=0, minute=0)
            continue
        # day of month / day of week
        if not dom_dow_match(fields, t):
            t = (t + timedelta(days=1)).replace(hour=0, minute=0)
            continue
        # day may have rolled past month end (e.g. Feb 30) — datetime keeps
        # it valid since we only advance by real timedeltas; verify month.
        runs.append(t)
        t = t + timedelta(minutes=1)
    return runs


# ---------------------------------------------------------------------------
# Job loading
# ---------------------------------------------------------------------------
CRON_LINE_RE = re.compile(
    r"^\s*(?P<expr>(?:@\w+|(?:\S+\s+){4}\S+))\s+#\s*name=(?P<name>[^,]+)"
    r"(?:,\s*duration=(?P<duration>\d+))?(?:,\s*script=(?P<script>\S+))?")


def load_jobs_file(path):
    """Load a JSON job list: {"jobs": [{"name", "schedule", "duration_minutes",
    "script" (inline text or path)}]}."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    jobs = data.get("jobs", data if isinstance(data, list) else [])
    out = []
    for j in jobs:
        script = j.get("script", "")
        script_path = Path(script) if script else None
        if script_path and script_path.is_file():
            script_text = script_path.read_text(encoding="utf-8", errors="ignore")
        else:
            script_text = script or ""
        out.append({"name": j["name"], "schedule": j["schedule"],
                    "duration_minutes": j.get("duration_minutes"),
                    "script": script_text, "source": str(path)})
    return out


def load_crontab_file(path):
    """Load annotated crontab lines, e.g.:
    0 2 * * *   # name=nightly-backup, duration=90, script=backup.sh
    Annotations are comments, so the file stays a valid crontab."""
    jobs = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        m = CRON_LINE_RE.match(line)
        if not m:
            continue
        jobs.append({"name": m.group("name").strip(),
                     "schedule": m.group("expr").strip(),
                     "duration_minutes": int(m.group("duration"))
                     if m.group("duration") else None,
                     "script": m.group("script") or "",
                     "source": str(path)})
    return jobs


# Minimal regex-based extraction of `on.schedule` cron triggers from GitHub
# Actions workflow files. This is NOT a YAML parser: it cannot understand
# anchors, multi-line structures, or quoted edge cases. It is honest about
# that — it finds `- cron: '<expr>'` lines that appear inside a schedule
# block and reports the file as the job's source.
SCHEDULE_CRON_RE = re.compile(
    r"schedule\s*:\s*\n(?P<block>(?:[ \t]+-[^\n]*\n?)+)", re.I)
CRON_ENTRY_RE = re.compile(r"-\s*cron\s*:\s*['\"](?P<expr>[^'\"]+)['\"]")


def discover_workflows(directory):
    """Auto-discover .github/workflows/*.yml schedule triggers under directory."""
    jobs = []
    wf_dir = Path(directory) / ".github" / "workflows"
    if not wf_dir.is_dir():
        return jobs
    for wf in sorted(wf_dir.glob("*.yml")) + sorted(wf_dir.glob("*.yaml")):
        text = wf.read_text(encoding="utf-8", errors="ignore")
        for block in SCHEDULE_CRON_RE.finditer(text):
            for entry in CRON_ENTRY_RE.finditer(block.group("block")):
                jobs.append({"name": f"{wf.stem} (scheduled)",
                             "schedule": entry.group("expr").strip(),
                             "duration_minutes": None,
                             "script": "",
                             "source": str(wf)})
    return jobs


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------
IDEMPOTENCY_SIGNALS = [
    "flock", ".lock", "lockfile", "idempotency", "idempotent",
    "--if-not-exists", "if_not_exists", "if-not-exists",
    "IF NOT EXISTS", "ON CONFLICT", "upsert", "setnx",
    "already exists", "skip-existing", "skip_existing",
]


def has_idempotency_signals(script_text):
    lowered = (script_text or "").lower()
    return [s for s in IDEMPOTENCY_SIGNALS if s.lower() in lowered]


def analyze(jobs, window=5, runs_per_job=50, start=None):
    """Return a findings dict with overlaps, interval risks, idempotency gaps."""
    start = start or datetime.now().replace(second=0, microsecond=0)
    schedules = {}
    for job in jobs:
        schedules[job["name"]] = next_runs(job["schedule"], start, n=runs_per_job)

    overlaps = []
    names = list(schedules)
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = names[i], names[j]
            hits = []
            for ra in schedules[a]:
                for rb in schedules[b]:
                    delta = abs((ra - rb).total_seconds()) / 60
                    if delta <= window:
                        hits.append((ra, rb, delta))
                        if len(hits) >= 5:
                            break
                if len(hits) >= 5:
                    break
            if hits:
                overlaps.append({"jobs": sorted([a, b]), "window_minutes": window,
                                 "examples": [{"a": ra.isoformat(), "b": rb.isoformat(),
                                               "apart_minutes": round(d, 2)}
                                              for ra, rb, d in hits]})

    interval_risks = []
    for job in jobs:
        dur = job.get("duration_minutes")
        if not dur:
            continue
        runs = schedules[job["name"]]
        if len(runs) < 2:
            continue
        min_gap = min((runs[k + 1] - runs[k]).total_seconds() / 60
                      for k in range(len(runs) - 1))
        if min_gap < dur:
            interval_risks.append({"job": job["name"], "schedule": job["schedule"],
                                   "min_interval_minutes": round(min_gap, 2),
                                   "declared_duration_minutes": dur})

    idempotency_gaps = []
    for job in jobs:
        if not job.get("script"):
            continue
        signals = has_idempotency_signals(job["script"])
        if not signals:
            idempotency_gaps.append({"job": job["name"],
                                     "note": "no idempotency signals found in script"})

    return {"overlaps": overlaps, "interval_risks": interval_risks,
            "idempotency_gaps": idempotency_gaps,
            "jobs_analyzed": len(jobs)}


def render_report(findings, window):
    L = []
    L.append("=" * 64)
    L.append("cron-overlap-guard report")
    L.append("=" * 64)
    L.append(f"Jobs analyzed : {findings['jobs_analyzed']}")
    L.append(f"Overlap window: {window} minutes")
    L.append("")
    L.append(f"OVERLAPS ({len(findings['overlaps'])}) — jobs firing within the window:")
    if findings["overlaps"]:
        for o in findings["overlaps"]:
            L.append(f"  ! {'  <>  '.join(o['jobs'])}")
            for e in o["examples"][:3]:
                L.append(f"      {e['a']} vs {e['b']} "
                         f"({e['apart_minutes']} min apart)")
    else:
        L.append("  none")
    L.append("")
    L.append(f"INTERVAL RISKS ({len(findings['interval_risks'])}) — interval shorter than declared duration:")
    if findings["interval_risks"]:
        for r in findings["interval_risks"]:
            L.append(f"  ! {r['job']}: runs every {r['min_interval_minutes']} min "
                     f"but declares {r['declared_duration_minutes']} min duration "
                     f"({r['schedule']})")
    else:
        L.append("  none")
    L.append("")
    L.append(f"IDEMPOTENCY GAPS ({len(findings['idempotency_gaps'])}) — no idempotency signals in script:")
    if findings["idempotency_gaps"]:
        for g in findings["idempotency_gaps"]:
            L.append(f"  ! {g['job']}: {g['note']}")
    else:
        L.append("  none")
    L.append("=" * 64)
    verdict = "FAIL" if findings["overlaps"] else "PASS"
    L.append(f"Verdict: {verdict} (exit {1 if findings['overlaps'] else 0})")
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Static analysis for scheduled jobs: overlap, interval "
                    "risk, and idempotency-gap detection.")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--jobs", help="JSON job list file")
    src.add_argument("--crontab", help="annotated crontab file "
                                      "(name=/duration=/script= in comments)")
    src.add_argument("--discover", metavar="DIR",
                     help="auto-discover .github/workflows/*.yml schedule "
                          "triggers under DIR")
    ap.add_argument("--window", type=int, default=5,
                    help="overlap window in minutes (default 5)")
    ap.add_argument("--json", action="store_true",
                    help="emit machine-readable JSON")
    ap.add_argument("--start", default=None,
                    help="analysis start time ISO format "
                         "(default: now); useful for reproducible runs")
    args = ap.parse_args(argv)

    if args.jobs:
        jobs = load_jobs_file(args.jobs)
    elif args.crontab:
        jobs = load_crontab_file(args.crontab)
    else:
        jobs = discover_workflows(args.discover)
    if not jobs:
        print("error: no jobs found in the given input", file=sys.stderr)
        return 2

    start = datetime.fromisoformat(args.start) if args.start else None
    findings = analyze(jobs, window=args.window, start=start)

    if args.json:
        print(json.dumps(findings, indent=2, default=str))
    else:
        print(render_report(findings, args.window))
    return 1 if findings["overlaps"] else 0


if __name__ == "__main__":
    sys.exit(main())
