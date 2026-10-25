"""Where usage numbers come from. Every source answers read(now) with a Usage.

SimulatedSource  deterministic, used by the demo and the tests.
FileSource       a JSON file you keep up to date yourself (same shape as the real API answer).
OAuthSource      the plan usage endpoint, with the token taken from an environment variable.
                 It is never used by the demo or the tests, and the token is never printed or stored.
"""
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from urllib import request as urlrequest

UTC = timezone.utc
FIVE_HOURS = 5.0
WEEK_HOURS = 168.0


class Usage:
    """Percent used and the reset time of the 5-hour and the weekly window."""

    def __init__(self, five_pct, five_reset, week_pct, week_reset, read_at):
        self.five_pct, self.five_reset = float(five_pct), five_reset
        self.week_pct, self.week_reset = float(week_pct), week_reset
        self.read_at = read_at

    def __repr__(self):
        return "Usage(5h=%.1f%%, week=%.1f%%, read_at=%s)" % (self.five_pct, self.week_pct, self.read_at.isoformat())


class SourceError(Exception):
    pass


def parse_time(text):
    text = re.sub(r"(\.\d{6})\d+", r"\1", text.replace("Z", "+00:00"))  # Python 3.8 takes at most 6 fraction digits
    d = datetime.fromisoformat(text)
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def usage_from_api_json(data, read_at):
    """Shape: {"five_hour": {"utilization": 42, "resets_at": "..."}, "seven_day": {...}}"""
    try:
        f, w = data["five_hour"], data["seven_day"]
        return Usage(f["utilization"], parse_time(f["resets_at"]), w["utilization"], parse_time(w["resets_at"]), read_at)
    except (KeyError, TypeError, ValueError) as exc:
        raise SourceError("usage answer is missing a field: %s" % exc)


class FileSource:
    def __init__(self, path):
        self.path = path

    def read(self, now=None):
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            return usage_from_api_json(data, datetime.fromtimestamp(os.path.getmtime(self.path), UTC))
        except (OSError, ValueError) as exc:
            raise SourceError("cannot read %s: %s" % (self.path, exc))


class OAuthSource:
    """GET the usage endpoint. The token comes from the environment variable named in token_env.

    The endpoint is not part of any documented API and may change or answer 429 when asked often, so
    the last good answer is cached for cache_minutes and served again when a call fails."""

    URL = "https://api.anthropic.com/api/oauth/usage"

    def __init__(self, token_env="USAGE_BRAKE_TOKEN", cache_path=None, cache_minutes=5, opener=None, url=None):
        self.token_env, self.cache_path, self.cache_minutes = token_env, cache_path, cache_minutes
        self.opener = opener or urlrequest.urlopen
        self.url = url or self.URL

    def _cached(self):
        if not self.cache_path or not os.path.exists(self.cache_path):
            return None, None
        with open(self.cache_path, encoding="utf-8") as fh:
            return json.load(fh), os.path.getmtime(self.cache_path)

    def read(self, now=None):
        data, mtime = self._cached()
        if data is not None and time.time() - mtime < self.cache_minutes * 60:
            return usage_from_api_json(data, datetime.fromtimestamp(mtime, UTC))
        token = os.environ.get(self.token_env)
        if not token:
            if data is not None:
                return usage_from_api_json(data, datetime.fromtimestamp(mtime, UTC))
            raise SourceError("set the environment variable %s to your plan token" % self.token_env)
        req = urlrequest.Request(self.url, headers={"Authorization": "Bearer " + token, "Accept": "application/json",
                                                    "anthropic-beta": "oauth-2025-04-20"})
        try:
            with self.opener(req, timeout=10) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # network, 401, 429, bad JSON: serve the last good answer if there is one
            reason = getattr(exc, "code", None) or type(exc).__name__
            if data is not None:
                return usage_from_api_json(data, datetime.fromtimestamp(mtime, UTC))
            raise SourceError("usage request failed (%s)" % reason)
        if self.cache_path:
            os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
            with open(self.cache_path, "w", encoding="utf-8") as fh:
                json.dump(body, fh)
        return usage_from_api_json(body, datetime.now(UTC))


class SimulatedSource:
    """A made-up week of work. Weekdays are heavy enough to run out before the reset.

    The week starts at week_start. Usage grows by rate(hour, weekday) points per hour of the weekly
    window. The 5-hour window opens with the first use after the previous one ended and shows
    5h% = 6 x the weekly points spent inside it (a 5-hour window that consumed 1/6 of the week is full)."""

    def __init__(self, week_start, day_rates=(2.0, 2.4), off_rate=0.05, weekend_factor=0.3):
        self.week_start = week_start
        self.day_rates, self.off_rate, self.weekend_factor = day_rates, off_rate, weekend_factor

    def rate(self, t):
        hours = (t - self.week_start).total_seconds() / 3600.0
        day, hour = int(hours // 24) % 7, int(hours % 24)
        if 8 <= hour < 13:
            r = self.day_rates[0]
        elif 13 <= hour < 18:
            r = self.day_rates[1]
        else:
            r = self.off_rate
        return r * (self.weekend_factor if day >= 5 else 1.0)

    def _walk(self, now):
        """-> (weekly points at now, 5h window start, weekly points at that start)."""
        t, week, win_start, win_base = self.week_start, 0.0, None, 0.0
        step = timedelta(minutes=1)
        while t < now:
            if win_start is not None and t >= win_start + timedelta(hours=FIVE_HOURS):
                win_start = None
            r = self.rate(t)
            if r > 0.5 and win_start is None:  # real work opens a window; background noise does not
                win_start, win_base = t, week
            week += r / 60.0
            t += step
        return week, win_start, win_base

    def read(self, now):
        week, win_start, win_base = self._walk(now)
        if win_start is None:
            five_pct, five_reset = 0.0, now + timedelta(hours=FIVE_HOURS)
        else:
            five_pct = min(100.0, 6.0 * (week - win_base))
            five_reset = win_start + timedelta(hours=FIVE_HOURS)
        return Usage(five_pct, five_reset, min(100.0, week), self.week_start + timedelta(hours=WEEK_HOURS), now)
