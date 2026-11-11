# cron-overlap-guard

Your `nightly-backup` fires at 02:00 and takes 90 minutes. Your `nightly-report` fires at 02:02 and hammers the same database. Nobody noticed until the 3 AM pages started. And that `*/5 * * * *` heartbeat whose script takes 10 minutes? It's been stacking overlapping instances for months.

`cron-overlap-guard` statically analyzes your scheduled jobs and finds three classes of problems **before** they page you:

- **OVERLAP** — two jobs scheduled within X minutes of each other (default 5, tunable) that can stampede shared resources.
- **INTERVAL RISK** — a job whose cron interval is shorter than its declared max duration (annotate expected duration per job).
- **IDEMPOTENCY GAP** — heuristic check of each job's script for idempotency signals (`flock`/lock files, idempotency keys, `--if-not-exists`, `ON CONFLICT`, ...). Jobs with none get flagged.

Exit code **1** on any overlap, so it works as a CI gate.

## 30-second quickstart

```bash
git clone https://github.com/Iceman2050/cron-overlap-guard
cd cron-overlap-guard
python3 guard.py --jobs demo-jobs.json --start 2026-10-17T12:00:00
```

No dependencies — Python 3.8+ standard library only.

Three input modes: `--jobs jobs.json` (a simple job list), `--crontab jobs.cron` (crontab lines annotated with `# name=..., duration=..., script=...` comments — still a valid crontab), or `--discover <dir>` (auto-finds `.github/workflows/*.yml` `on.schedule` triggers). Tune the overlap window with `--window 10`; `--json` for machine-readable output.

## Example output

Real output from running the quickstart above against the included `demo-jobs.json` fixture (3 jobs: a 90-min backup at 02:00, a report at 02:02, and a 5-minute heartbeat whose script takes 10):

```
================================================================
cron-overlap-guard report
================================================================
Jobs analyzed : 3
Overlap window: 5 minutes

OVERLAPS (1) — jobs firing within the window:
  ! nightly-backup  <>  nightly-report
      2026-10-18T02:00:00 vs 2026-10-18T02:02:00 (2.0 min apart)
      2026-10-19T02:00:00 vs 2026-10-19T02:02:00 (2.0 min apart)
      2026-10-20T02:00:00 vs 2026-10-20T02:02:00 (2.0 min apart)

INTERVAL RISKS (1) — interval shorter than declared duration:
  ! heartbeat: runs every 5.0 min but declares 10 min duration (*/5 * * * *)

IDEMPOTENCY GAPS (1) — no idempotency signals in script:
  ! heartbeat: no idempotency signals found in script
================================================================
Verdict: FAIL (exit 1)
```

## How it works

1. **Parses cron properly.** A hand-rolled parser handles `*/n` steps, ranges (`9-17`), stepped ranges (`9-17/2`), lists (`0,30`), month/day names, `@daily`/`@hourly`/etc. specials, and classic Vixie day-of-month/day-of-week OR semantics.
2. **Computes the next 50 run times per job** with a field-stepping search (no minute-by-minute walk, so even yearly schedules resolve instantly).
3. **Compares pairwise** for runs within the window, measures each job's minimum interval against its declared duration, and scans the job script for idempotency signals.

## What it does NOT do

- The GitHub Actions workflow discovery is a **deliberate, minimal regex-based schedule extraction** — it finds `- cron: '...'` lines inside `schedule:` blocks. It is not a YAML parser and will miss exotic formatting. This is documented in the code too.
- It uses **naive datetimes** (your local timezone as the machine sees it). It does not model DST transitions or per-job timezones.
- The idempotency check is a **heuristic, not a proof**: a signal match doesn't guarantee safe re-runs, and a flag doesn't guarantee danger — it's a prompt to look, not a verdict.
- It cannot know a job's real duration unless you annotate `duration_minutes`; unannotated jobs skip the interval-risk check.
- Overlap detection is about *scheduled proximity*, not actual resource contention — two jobs 2 minutes apart that touch nothing shared are a false positive by design (tune `--window`).

## Running the tests

```bash
python3 -m unittest discover -s tests -v
```

24 tests, all passing: the demo fixture asserts exactly 1 overlap pair (backup <> report), 1 interval risk and 1 idempotency gap (both heartbeat); cron parser unit tests cover `*/15`, ranges, stepped ranges, lists, names, specials, and invalid input; next-run tests cover weekday ranges, dom/dow OR semantics, and monthly schedules; plus crontab-annotation, workflow-discovery, and CLI exit-code tests.

## License

MIT — see [LICENSE](LICENSE).

## Built by Ingenious AI

Part of the [Ingenious AI](https://github.com/Iceman2050) security toolkit. Get early access to our platforms:

- [DisclosureGuard](https://wa.me/5016691053?text=Hi%20Ingenious%20AI%21%20I%20want%20early%20access%20to%20DisclosureGuard.)
- [ModelMinder](https://wa.me/5016691053?text=Hi%20Ingenious%20AI%21%20I%20want%20early%20access%20to%20ModelMinder.)
- [TermsWatch](https://wa.me/5016691053?text=Hi%20Ingenious%20AI%21%20I%20want%20early%20access%20to%20TermsWatch.)
- [Vibe-Code Audit](https://wa.me/5016691053?text=Hi%20Ingenious%20AI%21%20I%20want%20early%20access%20to%20Vibe-Code%20Audit.)
- [Rogue-Agent Defense](https://wa.me/5016691053?text=Hi%20Ingenious%20AI%21%20I%20want%20early%20access%20to%20Rogue-Agent%20Defense.)
- [BreachHunter](https://wa.me/5016691053?text=Hi%20Ingenious%20AI%21%20I%20want%20early%20access%20to%20BreachHunter.)
- [ChatBreaker](https://wa.me/5016691053?text=Hi%20Ingenious%20AI%21%20I%20want%20early%20access%20to%20ChatBreaker.)
- [Agent Flight Recorder](https://wa.me/5016691053?text=Hi%20Ingenious%20AI%21%20I%20want%20early%20access%20to%20Agent%20Flight%20Recorder.)
- [Agent Auth Gateway](https://wa.me/5016691053?text=Hi%20Ingenious%20AI%21%20I%20want%20early%20access%20to%20Agent%20Auth%20Gateway.)
- [DomainGuard](https://wa.me/5016691053?text=Hi%20Ingenious%20AI%21%20I%20want%20early%20access%20to%20DomainGuard.)
- [ContentCraft AI](https://wa.me/5016691053?text=Hi%20Ingenious%20AI%21%20I%20want%20early%20access%20to%20ContentCraft%20AI.)

