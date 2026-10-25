import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
HOME = tempfile.mkdtemp(prefix="usage-brake-test-")
os.environ["USAGE_BRAKE_HOME"] = HOME
os.environ.pop("USAGE_BRAKE_PROTECT", None)

from usage_brake import __main__ as cli  # noqa: E402
from usage_brake import brake, guard, pace, readout, sources  # noqa: E402

UTC = timezone.utc
START = datetime(2026, 1, 5, tzinfo=UTC)
SECRET = "sk-test-SECRET-token-value"


def api_json(five=40, week=60, now=START + timedelta(days=2)):
    return {"five_hour": {"utilization": five, "resets_at": (now + timedelta(hours=2)).isoformat()},
            "seven_day": {"utilization": week, "resets_at": (START + timedelta(days=7)).isoformat()}}


class FakeResponse:
    def __init__(self, body):
        self.body = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.body


class SourceTests(unittest.TestCase):
    def test_simulated_is_deterministic_and_monotonic(self):
        s = sources.SimulatedSource(START)
        last = 0.0
        for h in range(0, 168, 6):
            u = s.read(START + timedelta(hours=h))
            self.assertGreaterEqual(u.week_pct, last)
            last = u.week_pct
        self.assertEqual(repr(s.read(START + timedelta(hours=30))), repr(sources.SimulatedSource(START).read(START + timedelta(hours=30))))

    def test_simulated_week_runs_out_before_reset(self):
        self.assertGreaterEqual(sources.SimulatedSource(START).read(START + timedelta(days=5)).week_pct, 99)

    def test_simulated_five_hour_window_resets(self):
        s = sources.SimulatedSource(START)
        u = s.read(START + timedelta(days=1, hours=10))
        self.assertGreater(u.five_pct, 0)
        self.assertEqual(u.five_reset, START + timedelta(days=1, hours=13))
        self.assertEqual(s.read(START + timedelta(days=1, hours=3)).five_pct, 0)

    def test_file_source(self):
        p = os.path.join(HOME, "usage.json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(api_json(10, 20), fh)
        u = sources.FileSource(p).read()
        self.assertEqual((u.five_pct, u.week_pct), (10, 20))
        with self.assertRaises(sources.SourceError):
            sources.FileSource(os.path.join(HOME, "nope.json")).read()

    def test_file_source_with_missing_fields(self):
        p = os.path.join(HOME, "bad.json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump({"five_hour": {}}, fh)
        with self.assertRaises(sources.SourceError):
            sources.FileSource(p).read()

    def test_long_fraction_timestamps_parse(self):
        self.assertEqual(sources.parse_time("2026-01-05T10:00:00.123456789Z").microsecond, 123456)


class OAuthSourceTests(unittest.TestCase):
    def setUp(self):
        os.environ.pop("UB_TEST_TOKEN", None)
        self.calls = []

    def opener(self, body=None, error=None):
        def fn(req, timeout=10):
            self.calls.append(req)
            if error:
                raise error
            return FakeResponse(body)
        return fn

    def test_missing_token_names_the_variable_only(self):
        with self.assertRaises(sources.SourceError) as cm:
            sources.OAuthSource(token_env="UB_TEST_TOKEN", opener=self.opener(api_json())).read()
        self.assertIn("UB_TEST_TOKEN", str(cm.exception))
        self.assertEqual(self.calls, [])

    def test_token_goes_in_the_header_and_nowhere_else(self):
        os.environ["UB_TEST_TOKEN"] = SECRET
        src = sources.OAuthSource(token_env="UB_TEST_TOKEN", opener=self.opener(api_json(5, 7)),
                                  cache_path=os.path.join(HOME, "c1", "cache.json"))
        u = src.read()
        self.assertEqual(self.calls[0].get_header("Authorization"), "Bearer " + SECRET)
        self.assertNotIn(SECRET, repr(u) + repr(src.__dict__.get("cache_path")))
        with open(src.cache_path, encoding="utf-8") as fh:
            self.assertNotIn(SECRET, fh.read())

    def test_cache_serves_repeat_calls(self):
        os.environ["UB_TEST_TOKEN"] = SECRET
        src = sources.OAuthSource(token_env="UB_TEST_TOKEN", opener=self.opener(api_json()),
                                  cache_path=os.path.join(HOME, "c2", "cache.json"))
        src.read()
        src.read()
        self.assertEqual(len(self.calls), 1)

    def test_failure_serves_last_good_answer(self):
        os.environ["UB_TEST_TOKEN"] = SECRET
        cache = os.path.join(HOME, "c3", "cache.json")
        sources.OAuthSource(token_env="UB_TEST_TOKEN", opener=self.opener(api_json(33, 44)), cache_path=cache).read()
        old = time.time() - 3600
        os.utime(cache, (old, old))
        err = HTTPError("https://x", 429, "Too Many Requests", {}, None)
        u = sources.OAuthSource(token_env="UB_TEST_TOKEN", opener=self.opener(error=err), cache_path=cache).read()
        self.assertEqual(u.week_pct, 44)

    def test_failure_without_cache_does_not_leak_the_token(self):
        os.environ["UB_TEST_TOKEN"] = SECRET
        err = HTTPError("https://x/?t=" + SECRET, 401, "Unauthorized", {}, None)
        with self.assertRaises(sources.SourceError) as cm:
            sources.OAuthSource(token_env="UB_TEST_TOKEN", opener=self.opener(error=err),
                                cache_path=os.path.join(HOME, "c4", "cache.json")).read()
        self.assertNotIn(SECRET, str(cm.exception))
        self.assertIn("401", str(cm.exception))


def rows_for(pcts, reset, start, step_h=1.0, col_five=False):
    out = []
    for i, p in enumerate(pcts):
        t = start + timedelta(hours=i * step_h)
        out.append((t.timestamp(), p, p, reset.timestamp(), reset.timestamp()))
    return out


class PaceTests(unittest.TestCase):
    def test_sample_log_throttles(self):
        log = pace.SampleLog(os.path.join(HOME, "s1", "samples.csv"), min_gap_s=300)
        u = sources.SimulatedSource(START).read(START + timedelta(hours=10))
        self.assertTrue(log.add(u, START + timedelta(hours=10)))
        self.assertFalse(log.add(u, START + timedelta(hours=10, minutes=2)))
        self.assertTrue(log.add(u, START + timedelta(hours=10, minutes=6)))
        self.assertEqual(len(log.load()), 2)

    def test_rate_from_recent_samples(self):
        reset = START + timedelta(days=7)
        now = START + timedelta(days=2)
        rows = rows_for([40, 42, 44, 46, 48, 50], reset, now - timedelta(hours=5))
        p = pace.project(rows, now, 50, reset, 168, 2, lookback_h=24, min_span_h=1)
        self.assertAlmostEqual(p.rate, 2.0, places=2)
        self.assertTrue(p.runs_out)
        self.assertAlmostEqual(p.hours_to_empty, 25.0, places=1)
        self.assertAlmostEqual(p.pause_hours, 95.0, places=1)

    def test_slow_pace_reaches_reset(self):
        reset = START + timedelta(days=7)
        now = START + timedelta(days=2)
        rows = rows_for([40, 40.1, 40.2, 40.3], reset, now - timedelta(hours=3))
        p = pace.project(rows, now, 40.3, reset, 168, 2, lookback_h=24, min_span_h=1)
        self.assertFalse(p.runs_out)
        self.assertLess(p.at_reset, 100)

    def test_samples_from_another_window_are_ignored(self):
        reset = START + timedelta(days=7)
        now = START + timedelta(days=2)
        old = rows_for([90, 91], reset - timedelta(days=7), now - timedelta(hours=6))
        mine = rows_for([10, 11, 12], reset, now - timedelta(hours=2))
        p = pace.project(old + mine, now, 12, reset, 168, 2, lookback_h=24, min_span_h=1)
        self.assertAlmostEqual(p.rate, 1.0, places=1)

    def test_a_drop_ends_the_walk_back(self):
        reset = START + timedelta(days=7)
        now = START + timedelta(days=2)
        rows = rows_for([80, 5, 6, 7], reset, now - timedelta(hours=3))
        p = pace.project(rows, now, 8, reset, 168, 2, lookback_h=24, min_span_h=1)
        self.assertLess(p.rate, 2.0)

    def test_falls_back_to_window_average_then_to_nothing(self):
        reset = START + timedelta(days=7)
        p = pace.project([], START + timedelta(days=2), 30, reset, 168, 2, lookback_h=24, min_span_h=1)
        self.assertEqual(p.source, "window average")
        self.assertAlmostEqual(p.rate, 30 / 48.0, places=2)
        p = pace.project([], START + timedelta(minutes=10), 1, reset, 168, 2, lookback_h=24, min_span_h=1)
        self.assertEqual((p.rate, p.source), (0.0, "no history yet"))
        self.assertFalse(p.runs_out)


class BrakeTests(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(HOME, "b-%d" % time.time_ns(), "state.json")
        self.n = brake.ConsoleNotifier(io.StringIO())
        self.b = brake.Brake(self.path, self.n)

    def usage(self, week, now):
        return sources.Usage(10, now + timedelta(hours=3), week, START + timedelta(days=7), now)

    def test_allowance_is_spread_over_the_hours_left(self):
        now = START + timedelta(days=2, hours=12)  # 12 h left today, 108 h to reset
        r = self.b.check(self.usage(50, now), now, UTC)
        state = json.load(open(self.path, encoding="utf-8"))
        self.assertAlmostEqual(state["allowance"], 50 * 12 / 108.0, places=3)
        self.assertEqual(r["action"], "none")

    def test_levels_fire_once_each(self):
        now = START + timedelta(days=2, hours=12)
        self.b.check(self.usage(50, now), now, UTC)
        allowance = 50 * 12 / 108.0
        fired = []
        for frac in (0.5, 0.85, 0.9, 1.05, 1.1, 1.3, 1.6):
            r = self.b.check(self.usage(50 + allowance * frac, now), now, UTC)
            fired.append(r["action"])
        self.assertEqual(fired, ["none", "alarm", "none", "alarm", "none", "alarm", "alarm"])
        self.assertEqual([urgent for _, _, urgent in self.n.sent], [False, True, True, True])

    def test_new_day_recomputes_and_resets_the_level(self):
        now = START + timedelta(days=2, hours=12)
        self.b.check(self.usage(50, now), now, UTC)
        self.b.check(self.usage(58, now), now, UTC)
        nxt = START + timedelta(days=3, hours=1)
        self.b.check(self.usage(58, nxt), nxt, UTC)
        state = json.load(open(self.path, encoding="utf-8"))
        self.assertEqual((state["day"], state["level"], state["start_pct"]), ("2026-01-08", 0, 58))

    def test_usage_going_down_starts_over(self):
        now = START + timedelta(days=2, hours=12)
        self.b.check(self.usage(50, now), now, UTC)
        self.b.check(self.usage(20, now), now, UTC)
        self.assertEqual(json.load(open(self.path, encoding="utf-8"))["start_pct"], 20)

    def test_new_weekly_window_starts_over(self):
        now = START + timedelta(days=2, hours=12)
        self.b.check(self.usage(50, now), now, UTC)
        later = START + timedelta(days=7, hours=12)
        u = sources.Usage(0, later + timedelta(hours=3), 1, later + timedelta(days=7), later)
        self.b.check(u, later, UTC)
        self.assertEqual(json.load(open(self.path, encoding="utf-8"))["start_pct"], 1)

    def test_stale_reading_is_not_judged(self):
        now = START + timedelta(days=2, hours=12)
        u = sources.Usage(10, now, 95, START + timedelta(days=7), now - timedelta(minutes=90))
        r = self.b.check(u, now, UTC)
        self.assertEqual(r["action"], "skipped")
        self.assertEqual(self.n.sent, [])

    def test_no_alarm_when_the_allowance_is_tiny(self):
        now = START + timedelta(days=6, hours=23)
        r = self.b.check(self.usage(99.9, now), now, UTC)
        self.assertEqual(r["action"], "none")

    def test_failed_notifier_does_not_crash(self):
        class Broken:
            def send(self, *a, **k):
                return False
        b = brake.Brake(self.path, Broken())
        now = START + timedelta(days=2, hours=12)
        b.check(self.usage(50, now), now, UTC)
        self.assertEqual(b.check(self.usage(70, now), now, UTC)["action"], "none")


class ReadoutTests(unittest.TestCase):
    def test_line_shows_both_windows_and_runout(self):
        now = START + timedelta(days=2, hours=15)
        u = sources.SimulatedSource(START).read(now)
        rows = []
        t = START
        while t <= now:
            rows.append((t.timestamp(), 0, sources.SimulatedSource(START).read(t).week_pct, u.five_reset.timestamp(), u.week_reset.timestamp()))
            t += timedelta(hours=1)
        five, week = readout.analyse(u, rows, now)
        text = readout.line(u, five, week, now, UTC)
        self.assertIn("5h ", text)
        self.assertIn("week 61%", text)
        self.assertIn("before reset", text)

    def test_block_for_a_quiet_week(self):
        now = START + timedelta(days=1)
        u = sources.Usage(0, now + timedelta(hours=5), 3, START + timedelta(days=7), now)
        five, week = readout.analyse(u, [], now)
        text = readout.block(u, five, week, now, UTC)
        self.assertIn("Weekly", text)
        self.assertNotIn("BRAKE", text)


class GuardTests(unittest.TestCase):
    def setUp(self):
        for name in ("config.json", "last-usage.json", "override-until"):
            try:
                os.remove(os.path.join(HOME, name))
            except OSError:
                pass

    def deny(self, tool, **inp):
        return guard.decide({"tool_name": tool, "tool_input": inp})

    def test_changing_commands_are_denied(self):
        for cmd in ("python -m usage_brake override 600", "python3 -m usage_brake stop", "usage-brake uninstall",
                    "schtasks /Delete /TN usage-brake /F", "crontab -r # usage_brake", "rm -rf %s" % HOME,
                    "echo {} > %s/config.json" % HOME.replace("\\", "/")):
            self.assertIsNotNone(self.deny("Bash", command=cmd), cmd)

    def test_reading_and_ordinary_work_pass(self):
        for cmd in ("python -m usage_brake status", "python -m usage_brake statusline", "python -m usage_brake check",
                    "pytest -q", "ls -la", "git status"):
            self.assertIsNone(self.deny("Bash", command=cmd), cmd)

    def test_editing_the_state_folder_is_denied(self):
        self.assertIsNotNone(self.deny("Write", file_path=os.path.join(HOME, "config.json")))
        self.assertIsNotNone(self.deny("Edit", file_path=os.path.join(HOME, "sub", "x.json")))
        self.assertIsNone(self.deny("Write", file_path=os.path.join(tempfile.gettempdir(), "other.txt")))

    def test_extra_protected_paths(self):
        extra = tempfile.mkdtemp(prefix="ub-extra-")
        os.environ["USAGE_BRAKE_PROTECT"] = extra
        try:
            self.assertIsNotNone(self.deny("Write", file_path=os.path.join(extra, "usage_brake", "brake.py")))
            self.assertIsNotNone(self.deny("Bash", command="sed -i s/a/b/ %s/x.py" % extra.replace("\\", "/")))
        finally:
            os.environ.pop("USAGE_BRAKE_PROTECT")

    def write_state(self, week, age_s=0, cfg=None):
        with open(os.path.join(HOME, "config.json"), "w", encoding="utf-8") as fh:
            json.dump({"hard_stop_week_pct": 98} if cfg is None else cfg, fh)
        with open(os.path.join(HOME, "last-usage.json"), "w", encoding="utf-8") as fh:
            json.dump({"read_at": time.time() - age_s, "week_pct": week, "five_pct": 10}, fh)

    def test_hard_stop(self):
        self.write_state(99)
        out = self.deny("Bash", command="pytest -q")
        self.assertIn("Stop working", out["hookSpecificOutput"]["permissionDecisionReason"])
        self.write_state(90)
        self.assertIsNone(self.deny("Bash", command="pytest -q"))

    def test_hard_stop_needs_a_config_and_a_fresh_reading(self):
        self.write_state(99, cfg={})
        self.assertIsNone(self.deny("Bash", command="pytest -q"))
        self.write_state(99, age_s=7200)
        self.assertIsNone(self.deny("Bash", command="pytest -q"))

    def test_override_lifts_the_stop(self):
        self.write_state(99)
        with contextlib.redirect_stdout(io.StringIO()):
            cli.main(["override", "30"])
        self.assertIsNone(self.deny("Bash", command="pytest -q"))
        with open(os.path.join(HOME, "override-until"), "w", encoding="utf-8") as fh:
            fh.write(str(time.time() - 5))
        self.assertIsNotNone(self.deny("Bash", command="pytest -q"))

    def test_hook_process_survives_garbage(self):
        for stdin in ("not json", ""):
            p = subprocess.run([sys.executable, os.path.join(ROOT, "usage_brake", "guard.py")], input=stdin,
                               capture_output=True, text=True)
            self.assertEqual((p.stdout, p.returncode), ("", 0))


class CliTests(unittest.TestCase):
    def run_cli(self, *args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.main(list(args))
        return code, buf.getvalue()

    def test_status_with_simulated_source(self):
        code, out = self.run_cli("status", "--source", "sim", "--now", "2026-01-07T15:00:00Z", "--tz", "utc")
        self.assertEqual(code, 0)
        self.assertIn("Weekly", out)
        self.assertIn("61%", out)

    def test_statusline_is_one_line(self):
        code, out = self.run_cli("statusline", "--source", "sim", "--now", "2026-01-07T15:00:00Z", "--tz", "utc")
        self.assertEqual((code, len(out.strip().splitlines())), (0, 1))

    def test_check_writes_last_usage_for_the_guard(self):
        code, out = self.run_cli("check", "--source", "sim", "--now", "2026-01-07T15:00:00Z", "--tz", "utc", "--notify", "console")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.splitlines()[-1])["action"] in ("none", "alarm", "skipped"), True)
        self.assertTrue(os.path.exists(os.path.join(HOME, "last-usage.json")))

    def test_oauth_without_a_token_exits_with_a_message(self):
        os.environ.pop("USAGE_BRAKE_TOKEN", None)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code, _ = self.run_cli("status", "--source", "oauth")
        self.assertEqual(code, 1)
        self.assertIn("USAGE_BRAKE_TOKEN", err.getvalue())

    def test_unknown_command_prints_help(self):
        self.assertEqual(self.run_cli("nonsense")[0], 2)


if __name__ == "__main__":
    unittest.main()
