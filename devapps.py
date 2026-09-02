#!/usr/bin/env python3
"""Apps Launcher CLI -- start, stop and check the apps listed in apps.json.

Runs on Windows, Linux and macOS. All the work is in engine.py, which the web
UI imports directly, so the two front ends cannot disagree.

  devapps.py status                 what's up, and how that was determined
  devapps.py status --json          machine-readable
  devapps.py start                  every app with autostart:true
  devapps.py start myapp            one app, autostart flag ignored
  devapps.py start --all            every registered app
  devapps.py restart myapp
  devapps.py stop
  devapps.py logs myapp
  devapps.py install                run at login from now on
  devapps.py install --dry-run      show what would be registered
  devapps.py uninstall
"""

import argparse
import json
import sys
import time

import engine


def selected(name, cfg):
    entries = engine.apps(cfg)
    if not name:
        return entries
    hit = engine.find_entry(cfg, name)
    if not hit:
        sys.exit("No app named '%s' in apps.json" % name)
    return [hit]


def print_status(cfg, name=None, as_json=False):
    rows = [r for r in engine.statuses(cfg) if not name or r["name"] == name]
    if as_json:
        print(json.dumps(rows))
        return
    if not rows:
        print("No apps registered.")
        return
    widths = {
        "name": max(3, *(len(r["name"] or "") for r in rows)),
        "via": max(3, *(len(r["via"] or "") for r in rows)),
    }
    head = ("%-*s  %-7s  %-6s  %-*s  %-5s  %-9s  %s"
            % (widths["name"], "App", "Status", "PID", widths["via"], "Via",
               "Port", "Autostart", "Url"))
    print(head)
    print("-" * min(len(head), 100))
    for r in rows:
        print("%-*s  %-7s  %-6s  %-*s  %-5s  %-9s  %s"
              % (widths["name"], r["name"], r["status"],
                 r["pid"] if r["pid"] else "", widths["via"], r["via"],
                 r["port"] if r["port"] else "", r["autostart"], r["url"] or ""))


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="devapps.py", description="Start and manage local apps.")
    parser.add_argument("action", nargs="?", default="status",
                        choices=["status", "start", "stop", "restart", "logs",
                                 "install", "uninstall"])
    parser.add_argument("name", nargs="?", help="a single app; default is all")
    parser.add_argument("--all", action="store_true",
                        help="start/restart apps marked autostart:false too")
    parser.add_argument("--json", action="store_true",
                        help="status as JSON")
    parser.add_argument("--dry-run", action="store_true",
                        help="install: print the definition instead of applying it")
    args = parser.parse_args(argv)

    if args.action == "install":
        ok, msg = engine.install_autostart(dry_run=args.dry_run)
        print(msg)
        return 0 if ok else 1

    if args.action == "uninstall":
        ok, msg = engine.uninstall_autostart()
        print(msg)
        return 0 if ok else 1

    cfg = engine.load_registry()

    if args.action == "status":
        print_status(cfg, args.name, args.json)
        return 0

    if args.action == "logs":
        for entry in selected(args.name, cfg):
            print("=== %s ===" % entry["name"])
            print(engine.logs_tail(entry["name"]))
        return 0

    # An explicitly named app is acted on regardless of its autostart flag.
    def wanted(entry):
        return bool(args.name) or args.all or entry.get("autostart")

    if args.action in ("stop", "restart"):
        for entry in selected(args.name, cfg):
            if args.action == "restart" and not wanted(entry):
                continue
            ok, msg = engine.stop_app(entry)
            print("  %-16s %s" % (entry["name"], msg))

    if args.action in ("start", "restart"):
        if args.action == "restart":
            time.sleep(0.7)
        print("Starting apps..." if args.action == "start" else "")
        for entry in selected(args.name, cfg):
            if not wanted(entry):
                continue
            ok, msg = engine.start_app(entry)
            print("  %-16s %s" % (entry["name"], msg))

    print()
    print_status(cfg, args.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
