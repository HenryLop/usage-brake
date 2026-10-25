"""The brake: a daily allowance of weekly points, and alarms when it is used up.

It runs from the operating system's scheduler (cron, launchd, Task Scheduler), not from inside
the agent session, and it never calls a model. Allowance for the day:

    allowance = points left in the week x (hours left today / hours left until the weekly reset)

It is recomputed on a new day, in a new weekly window, or if usage went down. Levels:
    1  80% of the allowance spent
    2  allowance used up
    3+ every further 25% over it
Each level fires once. A reading older than max_age_min is not judged.
"""
import json
import os
import platform
import subprocess
import sys
from datetime import timedelta



class ConsoleNotifier:
    def __init__(self, out=None):
        self.out = out or sys.stdout
        self.sent = []

    def send(self, title, text, urgent=False):
        self.sent.append((title, text, urgent))
        print("[%s] %s: %s" % ("ALARM" if urgent else "notice", title, text), file=self.out)
        return True


class CommandNotifier:
    """Runs a command with the title and text as the last two arguments, for example ['notify-send']."""

    def __init__(self, argv):
        self.argv = list(argv)

    def send(self, title, text, urgent=False):
        try:
            return subprocess.run(self.argv + [title, text], capture_output=True, timeout=20).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False


def desktop_notifier():
    """The platform's own notification command, or the console if there is none."""
    system = platform.system()
    if system == "Linux":
        return CommandNotifier(["notify-send"])
    if system == "Darwin":
        return _AppleScript()
    if system == "Windows":
        return _WindowsToast()
    return ConsoleNotifier()


class _AppleScript:
    def send(self, title, text, urgent=False):
        script = 'display notification %s with title %s' % (json.dumps(text), json.dumps(title))
        try:
            return subprocess.run(["osascript", "-e", script], capture_output=True, timeout=20).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False


class _WindowsToast:
    def send(self, title, text, urgent=False):
        ps = ("[void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime];"
              "$x=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent('ToastText02');"
              "$n=$x.GetElementsByTagName('text');$n.Item(0).AppendChild($x.CreateTextNode($args[0]))|Out-Null;"
              "$n.Item(1).AppendChild($x.CreateTextNode($args[1]))|Out-Null;"
              "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('usage-brake').Show("
              "[Windows.UI.Notifications.ToastNotification]::new($x))")
        try:
            return subprocess.run(["powershell", "-NoProfile", "-Command", ps, title, text],
                                  capture_output=True, timeout=30).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False


class Brake:
    def __init__(self, state_path, notifier, max_age_min=60):
        self.state_path, self.notifier, self.max_age_min = state_path, notifier, max_age_min

    def _load(self):
        try:
            with open(self.state_path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    def _save(self, s):
        os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
        with open(self.state_path, "w", encoding="utf-8") as fh:
            json.dump(s, fh)

    def check(self, usage, now, tz):
        """Evaluate once. Returns a dict describing what happened (for logs and tests)."""
        age = (now - usage.read_at).total_seconds() / 60.0
        if age > self.max_age_min:
            return {"action": "skipped", "why": "reading is %d min old" % age}
        hours_to_reset = (usage.week_reset - now).total_seconds() / 3600.0
        if hours_to_reset <= 0.1:
            return {"action": "skipped", "why": "weekly window is resetting"}
        local = now.astimezone(tz)
        today = local.date().isoformat()
        s = self._load()
        same_window = bool(s) and abs(s["reset"] - usage.week_reset.timestamp()) < 600
        if not s or not same_window or s["day"] != today or usage.week_pct < s["start_pct"]:
            end_of_day = min((local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0),
                             usage.week_reset.astimezone(tz))
            hours_today = (end_of_day - local).total_seconds() / 3600.0
            allowance = max(0.0, 100.0 - usage.week_pct) * hours_today / hours_to_reset
            s = {"day": today, "reset": usage.week_reset.timestamp(), "start_pct": usage.week_pct,
                 "allowance": allowance, "level": 0}
        if s["allowance"] < 0.5:
            self._save(s)
            return {"action": "none", "why": "allowance for today is almost nothing", "level": s["level"]}
        spent = usage.week_pct - s["start_pct"]
        frac = spent / s["allowance"]
        level = 0
        if frac >= 0.8:
            level = 1
        if frac >= 1.0:
            level = 2 + int((frac - 1.0) // 0.25)
        fired = False
        if level > s["level"]:
            pts = "%.1f of %.1f points" % (spent, s["allowance"])
            if level == 1:
                fired = self.notifier.send("Brake: little allowance left today",
                                           "Spent %s today. Week at %d%%. Slow down or close one session." % (pts, round(usage.week_pct)), False)
            else:
                title = ("BRAKE: today's allowance is used up" if level == 2
                         else "BRAKE: %d%% of today's allowance spent" % round(frac * 100))
                fired = self.notifier.send(title,
                                           "Spent %s, %.1f over. Week at %d%%, %d points left until the reset." % (
                                               pts, spent - s["allowance"], round(usage.week_pct), round(100 - usage.week_pct)), True)
            s["level"] = level
        self._save(s)
        return {"action": "alarm" if level > 0 and fired else "none", "level": level, "spent": spent,
                "allowance": s["allowance"], "fraction": frac}
