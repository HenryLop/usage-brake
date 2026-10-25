"""One-command demo with a simulated usage feed. No account, no network, nothing outside a temp folder.

    python demo.py

A made-up work week is replayed hour by hour. You see the always-visible readout, the daily-allowance
alarms, and then the part that matters most: the agent trying to switch the brake off.
"""
import io
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
HOME = tempfile.mkdtemp(prefix="usage-brake-demo-")
os.environ["USAGE_BRAKE_HOME"] = HOME

from usage_brake import brake, guard, pace, readout, sources  # noqa: E402

UTC = timezone.utc
WEEK_START = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)  # a Monday, 00:00 UTC; all times below are UTC
SOURCE = sources.SimulatedSource(WEEK_START)
LOG = pace.SampleLog(os.path.join(HOME, "samples.csv"), min_gap_s=0)


def at(day, hour):
    return WEEK_START + timedelta(days=day, hours=hour)


def feed_samples(until):
    """The scheduler samples every 20 minutes; replay that up to `until`."""
    t = WEEK_START
    while t <= until:
        LOG.add(SOURCE.read(t), t)
        t += timedelta(minutes=20)


def show(title, now):
    usage = SOURCE.read(now)
    five, week = readout.analyse(usage, LOG.load(), now)
    print("\n== %s (%s UTC)" % (title, now.strftime("%a %H:%M")))
    print(readout.block(usage, five, week, now, UTC))
    print("status bar: " + readout.line(usage, five, week, now, UTC))


def replay_alarms(day):
    print("\n== The brake checks every hour on Wednesday (daily allowance)")
    notifier = brake.ConsoleNotifier(io.StringIO())
    b = brake.Brake(os.path.join(HOME, "brake-state.json"), notifier)
    for hour in range(0, 24):
        now = at(day, hour)
        r = b.check(SOURCE.read(now), now, UTC)
        if hour == 0:
            print("  %s  allowance for the day: %.1f points" % (now.strftime("%a %H:%M"), r.get("allowance", 0)))
        if r["action"] == "alarm":
            title, text, urgent = notifier.sent[-1]
            print("  %s  %s %s" % (now.strftime("%a %H:%M"), "ALARM" if urgent else "notice", title))
            print("             " + text)


def agent_tries():
    print("\n== The agent tries to switch the brake off")
    cases = [
        ("reads the budget", "Bash", {"command": "python -m usage_brake status"}),
        ("lifts the hard stop", "Bash", {"command": "python -m usage_brake override 600"}),
        ("deletes the scheduled job", "Bash", {"command": "schtasks /Delete /TN usage-brake /F"}),
        ("edits the brake's state", "Write", {"file_path": os.path.join(HOME, "config.json")}),
        ("does ordinary work", "Bash", {"command": "pytest -q"}),
    ]
    for label, tool, inp in cases:
        out = guard.decide({"tool_name": tool, "tool_input": inp})
        print("  %-28s %s" % (label, "DENIED" if out else "allowed"))


def hard_stop():
    print("\n== Optional hard stop at 98% of the week")
    with open(os.path.join(HOME, "config.json"), "w", encoding="utf-8") as fh:
        json.dump({"hard_stop_week_pct": 98}, fh)
    for label, day, hour in (("Wed 12:00", 2, 12), ("Fri 16:00", 4, 16)):
        now = at(day, hour)
        u = SOURCE.read(now)
        with open(os.path.join(HOME, "last-usage.json"), "w", encoding="utf-8") as fh:
            json.dump({"read_at": now.timestamp(), "week_pct": u.week_pct, "five_pct": u.five_pct}, fh)
        out = guard.decide({"tool_name": "Bash", "tool_input": {"command": "pytest -q"}}, now=now.timestamp())
        print("  %s week at %3d%%: next tool call %s" % (label, round(u.week_pct), "DENIED" if out else "allowed"))


def main():
    print("Usage brake demo. All times are a simulated week, in UTC.")
    feed_samples(at(1, 10))
    show("Tuesday morning", at(1, 10))
    feed_samples(at(2, 15))
    show("Wednesday afternoon: ahead of the line", at(2, 15))
    replay_alarms(2)
    agent_tries()
    hard_stop()
    print("\nState written to %s" % HOME)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
