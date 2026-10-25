"""The always-visible readout: 5-hour and weekly percent, reset time, run-out projection."""
from datetime import timezone

from . import pace
from .sources import FIVE_HOURS, WEEK_HOURS


def when(t, now, tz):
    t, now = t.astimezone(tz), now.astimezone(tz)
    if t.date() == now.date():
        return "today " + t.strftime("%H:%M")
    if (t.date() - now.date()).days == 1:
        return "tomorrow " + t.strftime("%H:%M")
    return t.strftime("%a %d %b %H:%M")


def analyse(usage, rows, now):
    five = pace.project(rows, now, usage.five_pct, usage.five_reset, FIVE_HOURS, 1, lookback_h=5, min_span_h=0.25)
    week = pace.project(rows, now, usage.week_pct, usage.week_reset, WEEK_HOURS, 2, lookback_h=24, min_span_h=1)
    return five, week


def line(usage, five, week, now, tz=timezone.utc):
    """One line for a status bar."""
    tail = ""
    if week.runs_out:
        tail = " | out %s, %s before reset" % (when(week.runs_out_at, now, tz), pace.hours_text(week.pause_hours))
    elif five.runs_out:
        tail = " | 5h out %s" % when(five.runs_out_at, now, tz)
    return "5h %d%% (resets %s) | week %d%% (resets %s)%s" % (
        round(usage.five_pct), when(usage.five_reset, now, tz), round(usage.week_pct), when(usage.week_reset, now, tz), tail)


def block(usage, five, week, now, tz=timezone.utc):
    """Several lines for a terminal."""
    ahead = week.pct - week.expected
    out = [
        "Weekly   %3d%%  resets %s (in %s)" % (round(usage.week_pct), when(usage.week_reset, now, tz), pace.hours_text(week.hours_left)),
        "5-hour   %3d%%  resets %s (in %s)" % (round(usage.five_pct), when(usage.five_reset, now, tz), pace.hours_text(five.hours_left)),
        "Line     an even spread would be at %d%% by now; you are %d points %s it." % (
            round(week.expected), round(abs(ahead)), "above" if ahead >= 0 else "below"),
        "Pace     %.2f %%/h (%s). Sustainable until reset: %.2f %%/h." % (week.rate, week.source, week.sustainable),
    ]
    if week.runs_out:
        out.append("BRAKE    at this pace the plan runs out %s, %s before the reset. Pause about %s to reach it." % (
            when(week.runs_out_at, now, tz), pace.hours_text(week.hours_left - week.hours_to_empty), pace.hours_text(week.pause_hours)))
    elif week.rate > 0.001:
        out.append("OK       at this pace you reach the reset at about %d%%." % round(week.at_reset))
    else:
        out.append("OK       no use at the moment; the line catches up on its own.")
    if five.runs_out:
        out.append("5-hour   at this pace the 5-hour window fills %s, before it resets." % when(five.runs_out_at, now, tz))
    return "\n".join(out)
