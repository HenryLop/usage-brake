"""Command line.

    python -m usage_brake status        usage readout for a terminal
    python -m usage_brake statusline    the same in one line, for a status bar (Claude Code statusLine)
    python -m usage_brake check         one brake evaluation; run it from cron / launchd / Task Scheduler
    python -m usage_brake override 60   lift a hard stop for 60 minutes (run it in your own terminal)
    python -m usage_brake guard         PreToolUse hook (reads the hook JSON on stdin)

Options for status, statusline and check:
    --source oauth|file:<path>|sim      default: $USAGE_BRAKE_SOURCE or oauth
    --now <ISO time>                    only with --source sim
    --tz utc|local                      default local
    --notify desktop|console            check only
"""
import json
import os
import sys
import time
from datetime import datetime, timezone

from . import brake, guard, pace, readout, sources


def parse(argv):
    opts = {"source": os.environ.get("USAGE_BRAKE_SOURCE", "oauth"), "now": None, "tz": "local", "notify": "desktop"}
    rest, i = [], 0
    while i < len(argv):
        if argv[i].startswith("--") and argv[i][2:] in opts and i + 1 < len(argv):
            opts[argv[i][2:]] = argv[i + 1]
            i += 2
        else:
            rest.append(argv[i])
            i += 1
    return opts, rest


def make_source(spec, now):
    home = guard.home()
    if spec == "oauth":
        return sources.OAuthSource(cache_path=os.path.join(home, "usage-cache.json"))
    if spec.startswith("file:"):
        return sources.FileSource(spec[5:])
    if spec == "sim":
        return sources.SimulatedSource(datetime(2026, 1, 5, tzinfo=timezone.utc))
    raise SystemExit("unknown source %r (oauth, file:<path>, sim)" % spec)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    opts, rest = parse(argv)
    if not rest:
        print(__doc__)
        return 2
    cmd = rest[0]
    if cmd == "guard":
        guard.main()
        return 0
    home = guard.home()
    if cmd == "override":
        minutes = float(rest[1]) if len(rest) > 1 else 60
        os.makedirs(home, exist_ok=True)
        with open(os.path.join(home, "override-until"), "w", encoding="utf-8") as fh:
            fh.write(str(time.time() + minutes * 60))
        print("Hard stop lifted for %d minutes." % minutes)
        return 0
    if cmd not in ("status", "statusline", "check"):
        print(__doc__)
        return 2
    now = sources.parse_time(opts["now"]) if opts["now"] else datetime.now(timezone.utc)
    tz = timezone.utc if opts["tz"] == "utc" else (datetime.now().astimezone().tzinfo or timezone.utc)
    try:
        usage = make_source(opts["source"], now).read(now)
    except sources.SourceError as exc:
        print("usage-brake: %s" % exc, file=sys.stderr)
        return 1
    log = pace.SampleLog(os.path.join(home, "samples.csv"))
    if opts["source"] != "sim":
        log.add(usage, now)
    five, week = readout.analyse(usage, log.load(), now)
    if cmd == "status":
        print(readout.block(usage, five, week, now, tz))
    elif cmd == "statusline":
        print(readout.line(usage, five, week, now, tz))
    else:
        os.makedirs(home, exist_ok=True)
        with open(os.path.join(home, "last-usage.json"), "w", encoding="utf-8") as fh:
            json.dump({"read_at": usage.read_at.timestamp(), "week_pct": usage.week_pct, "five_pct": usage.five_pct}, fh)
        notifier = brake.ConsoleNotifier() if opts["notify"] == "console" else brake.desktop_notifier()
        result = brake.Brake(os.path.join(home, "brake-state.json"), notifier).check(usage, now, tz)
        print(json.dumps(result))
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
