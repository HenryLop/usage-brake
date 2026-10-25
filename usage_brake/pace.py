"""Pace and run-out projection from a small log of usage samples."""
import os
from datetime import timedelta

RESET_TOLERANCE_S = 600


class SampleLog:
    """CSV of t, 5h%, week%, 5h reset, week reset (epoch seconds). One line per sample, throttled."""

    def __init__(self, path, min_gap_s=300):
        self.path, self.min_gap_s = path, min_gap_s

    def load(self):
        rows = []
        try:
            with open(self.path, encoding="utf-8") as fh:
                for line in fh:
                    p = line.strip().split(",")
                    if len(p) == 5:
                        try:
                            rows.append(tuple(float(x) for x in p))
                        except ValueError:
                            pass
        except OSError:
            pass
        return rows

    def add(self, usage, now):
        rows = self.load()
        if rows and now.timestamp() - rows[-1][0] < self.min_gap_s:
            return False
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write("%d,%.2f,%.2f,%d,%d\n" % (now.timestamp(), usage.five_pct, usage.week_pct,
                                              usage.five_reset.timestamp(), usage.week_reset.timestamp()))
        return True


class Projection:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def project(rows, now, pct, reset, window_hours, column, lookback_h, min_span_h):
    """column: 1 for the 5-hour window, 2 for the weekly one (index into a sample row).

    Pace = growth since the oldest sample of this same window inside the lookback. Usage never goes
    down inside a window, so a drop (new plan, new window) ends the walk back."""
    t_now = now.timestamp()
    reset_col = 3 if column == 1 else 4
    hours_left = max(0.0, (reset - now).total_seconds() / 3600.0)
    elapsed = max(0.0, window_hours - hours_left)
    base, older = None, float("inf")
    for row in reversed(rows):
        if row[0] < t_now - lookback_h * 3600:
            break
        if abs(row[reset_col] - reset.timestamp()) > RESET_TOLERANCE_S:
            continue
        if row[column] > older + 1:
            break
        base, older = row, row[column]
    span = (t_now - base[0]) / 3600.0 if base else 0.0
    if base and span >= min_span_h:
        rate, source = max(0.0, (pct - base[column]) / span), "last %s" % hours_text(span)
    elif elapsed >= min_span_h:
        rate, source = pct / elapsed, "window average"
    else:
        rate, source = 0.0, "no history yet"
    remaining = max(0.0, 100.0 - pct)
    sustainable = remaining / hours_left if hours_left > 0 else 0.0
    runs_out_at = hours_to_empty = None
    runs_out = False
    if rate > 0.001:
        hours_to_empty = remaining / rate
        runs_out_at = now + timedelta(hours=hours_to_empty)
        runs_out = hours_to_empty < hours_left
    return Projection(pct=pct, rate=rate, source=source, sustainable=sustainable, hours_left=hours_left,
                      expected=100.0 * elapsed / window_hours, runs_out=runs_out, runs_out_at=runs_out_at,
                      hours_to_empty=hours_to_empty,
                      pause_hours=max(0.0, hours_left - hours_to_empty) if runs_out else 0.0,
                      at_reset=min(100.0, pct + rate * hours_left))


def hours_text(h):
    if h < 1:
        return "%d min" % round(h * 60)
    if h < 24:
        return "%.1f h" % h
    return "%d d %d h" % (h // 24, round(h % 24))
