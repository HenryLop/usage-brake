"""PreToolUse hook for Claude Code: keeps the agent's hands off the brake, and can stop it at a limit.

1. Tamper guard. A tool call that would switch the brake off is denied: running any usage_brake
   subcommand that changes it (override, stop, uninstall, off), editing the brake's state folder or
   any path in USAGE_BRAKE_PROTECT, or touching a scheduler entry named usage-brake.
2. Hard stop (optional). With "hard_stop_week_pct" or "hard_stop_five_pct" in config.json, every tool
   call is denied once the last reading is at or over that number, until the user runs
   `python -m usage_brake override <minutes>` in their own terminal.

It reads only the last reading the scheduled `check` wrote. It never calls a usage source.

This is a guard against an agent that follows its tool permissions. It is not a security boundary
against a process running as the same operating-system user that is set on getting around it.
"""
import json
import os
import re
import sys
import time

SHELL = ("Bash", "PowerShell")
WRITERS = ("Write", "Edit", "NotebookEdit", "MultiEdit")
READ_ONLY_SUBCOMMANDS = {"status", "statusline", "demo", "profiles"}


def home():
    return os.environ.get("USAGE_BRAKE_HOME") or os.path.join(os.path.expanduser("~"), ".usage-brake")


def _norm(p):
    return os.path.normcase(os.path.abspath(os.path.expanduser(p))).replace("\\", "/").rstrip("/")


def protected_paths():
    extra = [p for p in os.environ.get("USAGE_BRAKE_PROTECT", "").split(os.pathsep) if p]
    return [_norm(p) for p in [home()] + extra]


def _text_hits_path(text, paths):
    t = os.path.normcase(text).replace("\\", "/")
    return any(p in t for p in paths)


def tamper(tool, tool_input):
    """-> reason string if the call would loosen the brake, else None."""
    if os.environ.get("USAGE_BRAKE_GUARD_OFF"):
        return None
    paths = protected_paths()
    if tool in WRITERS:
        target = (tool_input or {}).get("file_path") or (tool_input or {}).get("notebook_path") or ""
        if target and any(_norm(target) == p or _norm(target).startswith(p + "/") for p in paths):
            return "that file belongs to the usage brake"
        return None
    if tool not in SHELL:
        return None
    cmd = (tool_input or {}).get("command", "")
    for m in re.finditer(r"usage[_-]brake(?:\.py)?\s+(?:-\S+\s+)*([a-z][\w-]*)", cmd, re.I):
        if m.group(1).lower() not in READ_ONLY_SUBCOMMANDS and m.group(1).lower() != "check":
            return "usage_brake %s changes the brake" % m.group(1)
    if re.search(r"(schtasks|crontab|launchctl|systemctl|at\s)\b.*usage[_-]brake|usage[_-]brake.*\b(schtasks|crontab|launchctl|systemctl)\b", cmd, re.I):
        return "the brake's scheduled job is off limits"
    if _text_hits_path(cmd, paths):
        return "the command touches the brake's state folder"
    return None


def hard_stop(now=None):
    """-> reason string if a configured hard limit is reached, else None."""
    now = time.time() if now is None else now
    try:
        with open(os.path.join(home(), "config.json"), encoding="utf-8") as fh:
            cfg = json.load(fh)
        with open(os.path.join(home(), "last-usage.json"), encoding="utf-8") as fh:
            last = json.load(fh)
    except (OSError, ValueError):
        return None
    try:
        with open(os.path.join(home(), "override-until"), encoding="utf-8") as fh:
            if float(fh.read().strip()) > now:
                return None
    except (OSError, ValueError):
        pass
    if now - last.get("read_at", 0) > 3600:
        return None
    for key, field, label in (("hard_stop_week_pct", "week_pct", "weekly"), ("hard_stop_five_pct", "five_pct", "5-hour")):
        limit = cfg.get(key)
        if limit is not None and last.get(field, 0) >= limit:
            return ("the %s plan usage is at %d%% (limit %d%%)" % (label, round(last[field]), limit))
    return None


def decide(event, now=None):
    tool, tool_input = event.get("tool_name", ""), event.get("tool_input")
    why = tamper(tool, tool_input)
    if why:
        reason = ("BLOCKED by the usage brake: %s. The brake is not yours to change. Carry on without it and, if it "
                  "is in your way, tell the user." % why)
    else:
        stop = hard_stop(now)
        if not stop:
            return None
        reason = ("BLOCKED by the usage brake: %s. Stop working now and tell the user. Only the user can lift this, "
                  "from their own terminal." % stop)
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def main():
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return
    out = decide(event)
    if out:
        print(json.dumps(out))


if __name__ == "__main__":
    main()
