"""Tests for cron-overlap-guard (stdlib unittest only)."""
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from guard import (analyze, discover_workflows, dom_dow_match, has_idempotency_signals,
                   load_crontab_file, load_jobs_file, next_runs, parse_cron)

REPO = Path(__file__).resolve().parents[1]
DEMO_JOBS = REPO / "demo-jobs.json"
START = datetime(2026, 10, 17, 12, 0, 0)


class TestCronParser(unittest.TestCase):
    def test_every_five_minutes(self):
        f = parse_cron("*/5 * * * *")
        self.assertEqual(sorted(f["minute"]), list(range(0, 60, 5)))

    def test_step(self):
        self.assertEqual(sorted(parse_cron("*/15 * * * *")["minute"]),
                         [0, 15, 30, 45])

    def test_range(self):
        self.assertEqual(sorted(parse_cron("0 9-17 * * *")["hour"]),
                         list(range(9, 18)))

    def test_range_with_step(self):
        self.assertEqual(sorted(parse_cron("0 9-17/2 * * *")["hour"]),
                         [9, 11, 13, 15, 17])

    def test_list(self):
        self.assertEqual(sorted(parse_cron("0,30 8,20 * * *")["minute"]), [0, 30])
        self.assertEqual(sorted(parse_cron("0,30 8,20 * * *")["hour"]), [8, 20])

    def test_names(self):
        f = parse_cron("0 0 * jan mon")
        self.assertEqual(f["month"], {1})
        self.assertEqual(f["dow"], {1})

    def test_special(self):
        self.assertEqual(parse_cron("@daily"), parse_cron("0 0 * * *"))
        self.assertEqual(parse_cron("@hourly"), parse_cron("0 * * * *"))

    def test_bad_expression(self):
        with self.assertRaises(ValueError):
            parse_cron("* * * *")          # 4 fields
        with self.assertRaises(ValueError):
            parse_cron("61 * * * *")       # minute out of range
        with self.assertRaises(ValueError):
            parse_cron("*/0 * * * *")      # zero step


class TestNextRuns(unittest.TestCase):
    def test_every_five(self):
        runs = next_runs("*/5 * * * *", START, n=3)
        self.assertEqual([r.strftime("%H:%M") for r in runs],
                         ["12:05", "12:10", "12:15"])

    def test_daily(self):
        runs = next_runs("0 2 * * *", START, n=2)
        self.assertEqual(runs[0], datetime(2026, 10, 18, 2, 0))
        self.assertEqual(runs[1], datetime(2026, 10, 19, 2, 0))

    def test_weekday_range(self):
        # 09:00 on weekdays; start Saturday -> first run is Monday
        runs = next_runs("0 9 * * 1-5", datetime(2026, 10, 17, 12, 0), n=1)
        self.assertEqual(runs[0], datetime(2026, 10, 19, 9, 0))

    def test_dom_dow_or_semantics(self):
        # 1st of month OR Sunday
        runs = next_runs("0 0 1 * 0", datetime(2026, 10, 17, 12, 0), n=3)
        self.assertEqual(runs[0], datetime(2026, 10, 18, 0, 0))   # Sunday
        self.assertEqual(runs[1], datetime(2026, 10, 25, 0, 0))   # Sunday
        self.assertEqual(runs[2], datetime(2026, 11, 1, 0, 0))    # 1st AND Sunday

    def test_monthly(self):
        runs = next_runs("30 4 1 * *", START, n=1)
        self.assertEqual(runs[0], datetime(2026, 11, 1, 4, 30))

    def test_fifty_runs_fast(self):
        import time
        t0 = time.time()
        runs = next_runs("17 3 * * *", START, n=50)
        self.assertEqual(len(runs), 50)
        self.assertLess(time.time() - t0, 5.0)


class TestDemoFixture(unittest.TestCase):
    """Fixture: 3 jobs — nightly-backup (02:00) and nightly-report (02:02)
    overlap within the 5-min window; heartbeat (*/5, 10-min duration) has an
    interval risk and no idempotency signals."""

    def setUp(self):
        self.jobs = load_jobs_file(str(DEMO_JOBS))
        self.findings = analyze(self.jobs, window=5, start=START)

    def test_three_jobs_loaded(self):
        self.assertEqual(len(self.jobs), 3)

    def test_overlap_detected(self):
        self.assertEqual(len(self.findings["overlaps"]), 1)
        self.assertEqual(self.findings["overlaps"][0]["jobs"],
                         ["nightly-backup", "nightly-report"])

    def test_no_extra_overlaps(self):
        pairs = [tuple(o["jobs"]) for o in self.findings["overlaps"]]
        self.assertNotIn(("heartbeat", "nightly-backup"), pairs)

    def test_interval_risk(self):
        risks = {r["job"] for r in self.findings["interval_risks"]}
        self.assertIn("heartbeat", risks)
        self.assertNotIn("nightly-backup", risks)
        hb = next(r for r in self.findings["interval_risks"]
                  if r["job"] == "heartbeat")
        self.assertEqual(hb["min_interval_minutes"], 5.0)

    def test_idempotency_gap(self):
        gaps = {g["job"] for g in self.findings["idempotency_gaps"]}
        self.assertEqual(gaps, {"heartbeat"})

    def test_idempotency_signals_recognized(self):
        self.assertTrue(has_idempotency_signals("flock -n /tmp/x.lock cmd"))
        self.assertTrue(has_idempotency_signals("INSERT ... ON CONFLICT DO NOTHING"))
        self.assertTrue(has_idempotency_signals("mkdir -p out && cmd --if-not-exists"))
        self.assertFalse(has_idempotency_signals("curl -s https://example.com/ping"))


class TestInputs(unittest.TestCase):
    def test_crontab_annotations(self):
        with tempfile.NamedTemporaryFile("w", suffix=".cron", delete=False) as f:
            f.write("0 2 * * *  # name=backup, duration=90\n")
            f.write("*/10 * * * *  # name=poller\n")
            name = f.name
        jobs = load_crontab_file(name)
        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0]["duration_minutes"], 90)
        self.assertIsNone(jobs[1]["duration_minutes"])

    def test_workflow_discovery(self):
        with tempfile.TemporaryDirectory() as d:
            wf = Path(d) / ".github" / "workflows"
            wf.mkdir(parents=True)
            (wf / "nightly.yml").write_text(
                "name: nightly\non:\n  schedule:\n"
                "    - cron: '0 3 * * *'\n    - cron: \"30 4 * * 1\"\n"
                "jobs:\n  run:\n    runs-on: ubuntu-latest\n")
            jobs = discover_workflows(d)
            self.assertEqual(len(jobs), 2)
            self.assertEqual({j["schedule"] for j in jobs},
                             {"0 3 * * *", "30 4 * * 1"})


class TestCli(unittest.TestCase):
    def test_exit_1_on_overlap(self):
        proc = subprocess.run(
            [sys.executable, str(REPO / "guard.py"),
             "--jobs", str(DEMO_JOBS), "--start", "2026-10-17T12:00:00"],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("OVERLAPS (1)", proc.stdout)
        self.assertIn("nightly-backup", proc.stdout)
        self.assertIn("INTERVAL RISKS (1)", proc.stdout)
        self.assertIn("IDEMPOTENCY GAPS (1)", proc.stdout)

    def test_exit_0_when_clean(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"jobs": [
                {"name": "a", "schedule": "0 2 * * *", "duration_minutes": 30,
                 "script": "flock /tmp/a.lock do-a"},
                {"name": "b", "schedule": "0 4 * * *", "duration_minutes": 30,
                 "script": "do-b --if-not-exists"},
            ]}, f)
            name = f.name
        proc = subprocess.run(
            [sys.executable, str(REPO / "guard.py"),
             "--jobs", name, "--start", "2026-10-17T12:00:00"],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("Verdict: PASS", proc.stdout)


if __name__ == "__main__":
    unittest.main()
