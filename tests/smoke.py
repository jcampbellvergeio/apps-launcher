#!/usr/bin/env python3
"""End-to-end smoke test for the engine, on any platform.

Runs the real code paths -- process listing, port probing, spawning detached,
signalling, log capture, version resolution -- against a throwaway registry in
a temporary directory. Nothing touches the caller's own apps.json, logs or
autostart.

    python tests/smoke.py

Exit code 0 means every check passed. Used by CI on Linux, macOS and Windows,
and by anyone who wants to know whether this works on their box.
"""

import json
import os
import shutil
import socket
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import engine                                            # noqa: E402

FAILURES = []
CHECKS = 0


def check(label, condition, detail=""):
    global CHECKS
    CHECKS += 1
    if condition:
        print("  ok    %s" % label)
    else:
        print("  FAIL  %s%s" % (label, (" -- " + str(detail)) if detail else ""))
        FAILURES.append(label)
    return bool(condition)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


APP_SOURCE = """import http.server, socketserver, sys
print("demo app up", flush=True)
port = int(sys.argv[1])
with socketserver.TCPServer(("127.0.0.1", port), http.server.SimpleHTTPRequestHandler) as srv:
    srv.serve_forever()
"""


def main():
    tmp = tempfile.mkdtemp(prefix="applauncher-smoke-")
    port = free_port()
    print("platform: %s | psutil: %s | temp: %s"
          % (sys.platform, engine.psutil is not None, tmp))
    print()

    # An app to manage: its own folder, its own script, a real listening port.
    app_folder = os.path.join(tmp, "DemoApp")
    os.makedirs(app_folder)
    with open(os.path.join(app_folder, "serve.py"), "w", encoding="utf-8") as fh:
        fh.write(APP_SOURCE)
    with open(os.path.join(app_folder, "VERSION"), "w", encoding="utf-8") as fh:
        fh.write("9.9.9\n")

    registry = {"apps": [{
        "name": "demo",
        "dir": "DemoApp",
        "command": sys.executable,
        "args": ["serve.py", str(port)],
        "port": port,
        "url": "http://127.0.0.1:%d/" % port,
        "autostart": True,
        "description": "smoke test app",
        "match": "DemoApp.serve[.]py",
        "type": "app",
    }]}

    # Redirect every path the engine writes to, so the caller's own state is
    # untouched. This is why they are module-level names, not constants.
    engine.PARENT = tmp
    engine.APPS_JSON = os.path.join(tmp, "apps.json")
    engine.APPS_EXAMPLE = os.path.join(tmp, "apps.example.json")
    engine.LOG_DIR = os.path.join(tmp, "logs")
    engine.STATE_DIR = os.path.join(tmp, "state")
    os.makedirs(engine.LOG_DIR)
    os.makedirs(engine.STATE_DIR)
    with open(engine.APPS_JSON, "w", encoding="utf-8") as fh:
        json.dump(registry, fh, indent=2)

    cfg = engine.load_registry()
    entry = engine.find_entry(cfg, "demo")
    check("registry loads", entry is not None)

    # --- process listing: the signal everything else depends on -------------
    procs = engine.list_processes()
    check("process listing returns entries", len(procs) > 5, "got %d" % len(procs))
    check("this interpreter appears in the listing",
          any(str(os.getpid()) == str(pid) for pid, _ in procs))

    # --- before starting ---------------------------------------------------
    state = engine.app_state(entry, cfg)
    check("stopped app reports stopped", state["running"] is False, state)

    # --- start -------------------------------------------------------------
    ok, msg = engine.start_app(entry)
    check("start reports success", ok, msg)
    check("port answers after start", engine.port_is_open(port), "port %d" % port)

    state = engine.app_state(entry, engine.load_registry())
    check("running app identified by command-line match",
          state["running"] and state["via"] == "match", state)
    check("pid recorded in state dir",
          os.path.exists(os.path.join(engine.STATE_DIR, "demo.pid")))

    # --- idempotence -------------------------------------------------------
    ok2, msg2 = engine.start_app(entry)
    check("second start is a no-op", ok2 and "already running" in msg2, msg2)

    # --- log capture -------------------------------------------------------
    time.sleep(0.4)
    tail = engine.logs_tail("demo")
    check("stdout captured to logs/", "demo app up" in tail, tail[:120])

    # --- versions ----------------------------------------------------------
    engine.forget_version()
    version = engine.app_version(entry)
    check("version read from VERSION file",
          version["version"] == "9.9.9" and version["source"] == "VERSION", version)

    entry_with_cmd = dict(entry)
    entry_with_cmd["version_cmd"] = "%s -c \"print('cmd-1.2.3')\"" % sys.executable
    entry_with_cmd["name"] = "demo-cmd"
    version = engine.app_version(entry_with_cmd)
    check("version_cmd is honoured",
          version["version"] == "cmd-1.2.3" and version["source"] == "version_cmd",
          version)

    # --- statuses ----------------------------------------------------------
    rows = engine.statuses()
    check("statuses() shape", len(rows) == 1 and rows[0]["name"] == "demo", rows)

    # --- stop --------------------------------------------------------------
    ok, msg = engine.stop_app(entry)
    check("stop reports success", ok, msg)
    for _ in range(20):
        if not engine.port_is_open(port):
            break
        time.sleep(0.25)
    check("port is free after stop", not engine.port_is_open(port))
    state = engine.app_state(entry, engine.load_registry())
    check("stopped app reports stopped again", state["running"] is False, state)

    ok, msg = engine.stop_app(entry)
    check("stopping a stopped app is harmless", ok and "not running" in msg, msg)

    # --- a missing folder is reported, not raised ---------------------------
    ghost = dict(entry)
    ghost["name"] = "ghost"
    ghost["dir"] = "NoSuchFolder"
    ok, msg = engine.start_app(ghost)
    check("missing folder is refused cleanly",
          ok is False and "not found" in msg, msg)

    # --- autostart definition renders for this platform --------------------
    ok, rendered = engine.install_autostart(dry_run=True)
    check("autostart dry-run renders", ok and len(rendered) > 40, rendered[:120])
    if sys.platform.startswith("win"):
        check("windows: schtasks command", "schtasks" in rendered, rendered[:120])
    elif sys.platform == "darwin":
        check("macos: plist is well-formed XML", _valid_xml(rendered), rendered[:200])
        check("macos: agent has RunAtLoad", "RunAtLoad" in rendered)
    else:
        check("linux: systemd unit has an install target",
              "WantedBy=default.target" in rendered, rendered[:200])
        check("linux: start timeout disabled", "TimeoutStartSec=0" in rendered)

    shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("%d checks, %d failed" % (CHECKS, len(FAILURES)))
    for name in FAILURES:
        print("  failed: %s" % name)
    return 1 if FAILURES else 0


def _valid_xml(rendered):
    import xml.dom.minidom
    body = rendered.split("\n\n", 1)[1] if "\n\n" in rendered else rendered
    try:
        xml.dom.minidom.parseString(body)
        return True
    except Exception:
        return False


if __name__ == "__main__":
    sys.exit(main())
