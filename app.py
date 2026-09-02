"""App Launcher -- web front end.

A row per registered app: open it, start/stop/restart it, see whether it is
actually listening, register a new one. Everything about the registry, liveness
and launching lives in engine.py, which the CLI uses too -- so the page and the
command line cannot disagree about whether an app is running.

Localhost only, by design: this process can start arbitrary commands.
"""

import os
import re
import shlex
import ssl
import threading
import time
import urllib.error
import urllib.request

from flask import Flask, jsonify, render_template, request

import engine

# Paths and registry behaviour come from the engine; these are just local names
# for them so the request handlers read cleanly.
ROOT = engine.ROOT
DEV_ROOT = engine.PARENT
LOG_DIR = engine.LOG_DIR
ICON_DIR = engine.ICON_DIR
load_registry = engine.load_registry
save_registry = engine.save_registry
find_entry = engine.find_entry
is_self_entry = engine.is_self_entry
app_dir = engine.app_dir
derive_match = engine.derive_match
rename_side_files = engine.rename_side_files

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")

# NOT 5060: browsers refuse that one outright, see UNSAFE_PORTS.
PORT = int(os.environ.get("LAUNCHER_PORT") or 5058)

# Ports Chrome/Firefox refuse to load over http, with ERR_UNSAFE_PORT and no
# hint that the port is the problem -- the server answers curl perfectly.
# Registering an app here is almost always a mistake worth flagging. Chrome's
# blocked list, trimmed to what a dev app might plausibly pick.
UNSAFE_PORTS = {
    1, 7, 9, 11, 13, 15, 17, 19, 20, 21, 22, 23, 25, 37, 42, 43, 53, 69, 77,
    79, 87, 95, 101, 102, 103, 104, 109, 110, 111, 113, 115, 117, 119, 123,
    135, 137, 138, 139, 143, 161, 179, 389, 427, 465, 512, 513, 514, 515, 526,
    530, 531, 532, 540, 548, 554, 556, 563, 587, 601, 636, 989, 990, 993, 995,
    1719, 1720, 1723, 2049, 3659, 4045, 4190, 5060, 5061, 6000, 6566, 6665,
    6666, 6667, 6668, 6669, 6679, 6697, 10080,
}

app = Flask(__name__)
# This is a dev tool that gets edited while it runs; without this, Jinja caches
# every template for the life of the process (debug=False turns auto-reload off)
# and a template edit silently does nothing until a restart.
app.config["TEMPLATES_AUTO_RELOAD"] = True

_status_lock = threading.Lock()
_status_cache = {"at": 0.0, "rows": []}


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #

def icon_for(entry):
    """URL of an app's icon.

    An explicit `icon` in the registry wins; otherwise a file named after the
    app; otherwise the generic one. Named-after-the-app means dropping
    `static/icons/<name>.svg` in place is all it takes to give a newly
    registered app its own artwork -- no code, no registry edit.
    """
    named = entry.get("icon") or (entry.get("name") or "") + ".svg"
    candidate = os.path.basename(named)
    if os.path.exists(os.path.join(ICON_DIR, candidate)):
        return "/static/icons/" + candidate
    return "/static/icons/default.svg"


def read_status(max_age=1.5):
    """Liveness for every app, from the engine.

    Cached briefly: on Windows without psutil the sweep shells out to
    PowerShell and costs a second or two, so several tabs polling at once would
    otherwise be wasteful.
    """
    with _status_lock:
        if time.time() - _status_cache["at"] < max_age:
            return _status_cache["rows"]
    rows = engine.statuses()
    with _status_lock:
        _status_cache["at"] = time.time()
        _status_cache["rows"] = rows
    return rows


def invalidate_status():
    with _status_lock:
        _status_cache["at"] = 0.0


def app_view():
    """Registry entries merged with live status, ready for the page."""
    cfg = load_registry()
    by_name = {r.get("name"): r for r in read_status()}
    view = []
    for entry in cfg.get("apps", []):
        name = entry.get("name")
        row = by_name.get(name, {})
        directory = app_dir(entry)
        view.append({
            "name": name,
            "description": entry.get("description") or "",
            "icon": icon_for(entry),
            "dir": entry.get("dir"),
            "path": directory,
            "path_exists": os.path.isdir(directory),
            "command": entry.get("command"),
            "args": entry.get("args") or [],
            "port": entry.get("port"),
            "url": entry.get("url"),
            "match": entry.get("match") or "",
            "autostart": bool(entry.get("autostart")),
            "note": entry.get("note") or "",
            "running": row.get("status") == "running",
            "pid": row.get("pid"),
            "via": row.get("via") or "none",
            "type": engine.entry_type(entry),
            "is_self": is_self_entry(entry),
            "has_logs": os.path.exists(os.path.join(LOG_DIR, name + ".log")),
        })
    return view


# --------------------------------------------------------------------------- #
# pages
# --------------------------------------------------------------------------- #

@app.route("/")
def index():
    return render_template("index.html", apps=app_view(), page="apps")


@app.route("/status")
def status_page():
    return render_template("status.html", apps=app_view(), page="status")


# --------------------------------------------------------------------------- #
# api
# --------------------------------------------------------------------------- #

@app.route("/api/status")
def api_status():
    return jsonify({"apps": app_view(), "at": time.time()})


@app.route("/api/action/<action>", methods=["POST"])
@app.route("/api/action/<action>/<name>", methods=["POST"])
def api_action(action, name=None):
    if action not in ("start", "stop", "restart"):
        return jsonify({"ok": False, "error": "unknown action"}), 400

    cfg = load_registry()
    entry = find_entry(cfg, name) if name is not None else None
    if name is not None and not entry:
        return jsonify({"ok": False, "error": "no app named '%s'" % name}), 404
    if entry and is_self_entry(entry) and action in ("stop", "restart"):
        return jsonify({"ok": False, "error":
                        "the launcher cannot %s itself -- use the CLI" % action}), 400

    # No name means the fleet: the apps marked autostart, exactly as the login
    # task does it. A named app is acted on regardless of that flag.
    targets = [entry] if entry else [a for a in engine.apps(cfg) if a.get("autostart")]

    lines, ok_all = [], True
    for target in targets:
        if action in ("stop", "restart"):
            ok, msg = engine.stop_app(target)
            lines.append("%-16s %s" % (target["name"], msg))
            ok_all = ok_all and ok
        if action in ("start", "restart"):
            ok, msg = engine.start_app(target)
            lines.append("%-16s %s" % (target["name"], msg))
            ok_all = ok_all and ok

    invalidate_status()
    return jsonify({"ok": ok_all, "output": "\n".join(lines)})


@app.route("/api/framable/<name>")
def api_framable(name):
    """Can this app be embedded in an iframe?

    Asked before showing the viewer, because the two things that stop an app
    rendering in a frame are both invisible from the browser side: the app
    sending X-Frame-Options / CSP frame-ancestors, and a self-signed
    certificate (which can only be accepted in a real tab). Reporting the
    reason beats showing a blank frame.
    """
    entry = find_entry(load_registry(), name)
    if not entry:
        return jsonify({"ok": False, "error": "unknown app"}), 404
    url = entry.get("url")
    if not url:
        return jsonify({"ok": True, "framable": False, "reason": "no URL registered"})

    # A self-signed cert is fine to ignore here: we only want the headers, and
    # the verdict we return is about framing, not about trusting the app.
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=3, context=ctx) as res:
            headers = {k.lower(): v for k, v in res.headers.items()}
            https_used = res.url.startswith("https:")
    except urllib.error.HTTPError as exc:      # 4xx/5xx still carry headers
        headers = {k.lower(): v for k, v in exc.headers.items()}
        https_used = url.startswith("https:")
    except Exception as exc:                   # down, refused, TLS failure
        return jsonify({"ok": True, "framable": False,
                        "reason": "could not be reached (%s)" % type(exc).__name__})

    xfo = (headers.get("x-frame-options") or "").strip().lower()
    csp = (headers.get("content-security-policy") or "").lower()
    if xfo in ("deny", "sameorigin"):
        return jsonify({"ok": True, "framable": False,
                        "reason": "the app sends X-Frame-Options: %s" % xfo.upper()})
    if "frame-ancestors" in csp and "frame-ancestors 'self'" not in csp:
        return jsonify({"ok": True, "framable": False,
                        "reason": "the app's Content-Security-Policy forbids framing "
                                  "(frame-ancestors)"})
    if https_used or url.startswith("https:"):
        # Served fine for us, but the browser judges the certificate itself and
        # cannot prompt inside a frame.
        return jsonify({"ok": True, "framable": True, "warn":
                        "served over HTTPS -- if the certificate is self-signed the "
                        "frame will stay blank, since a certificate can only be "
                        "accepted in a real tab"})
    return jsonify({"ok": True, "framable": True})


@app.route("/api/logs/<name>")
def api_logs(name):
    if not find_entry(load_registry(), name):
        return jsonify({"ok": False, "error": "unknown app"}), 404
    return jsonify({"ok": True, "text": engine.logs_tail(name)})


class Invalid(ValueError):
    """A rejected field, with the HTTP status to answer with."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def parse_fields(data, cfg, current=None):
    """Validate and normalise registry fields from the add/edit form.

    Shared by both so the two can't drift -- an edit has to be as strict as an
    add. `current` is the entry being edited; its own name and port are not
    treated as clashes with itself. Returns (fields, warning).
    """
    existing = cfg.get("apps", [])
    fields = {}

    if current is None:
        name = (data.get("name") or "").strip().lower()
        if not NAME_RE.match(name):
            raise Invalid("name must be lowercase letters, digits, . _ - (max 32)")
        if any(a.get("name") == name for a in existing):
            raise Invalid("'%s' is already registered" % name, 409)
        fields["name"] = name
    else:
        # Renaming would orphan logs/<name>.log and state/<name>.pid, so the
        # name is fixed once registered.
        name = current.get("name")

    raw_dir = (data.get("dir") or "").strip().strip('"')
    if not raw_dir:
        raise Invalid("path is required")
    # An absolute path inside dev/ is stored relative, so the registry stays
    # readable and portable; anywhere else is kept as given.
    if os.path.isabs(raw_dir):
        resolved = os.path.normpath(raw_dir)
        try:
            rel = os.path.relpath(resolved, DEV_ROOT)
        except ValueError:            # different drive
            rel = None
        stored = rel if rel and not rel.startswith("..") else resolved
    else:
        stored = raw_dir.replace("/", os.sep)
        resolved = os.path.normpath(os.path.join(DEV_ROOT, stored))
    if not os.path.isdir(resolved):
        raise Invalid("not a folder: %s" % resolved)
    fields["dir"] = stored

    fields["command"] = (data.get("command") or "python").strip()

    raw_args = data.get("args")
    if isinstance(raw_args, list):
        args = [str(a) for a in raw_args]
    else:
        try:
            args = shlex.split(raw_args or "", posix=False)
        except ValueError as exc:
            raise Invalid("could not parse args: %s" % exc)
        args = [a.strip('"') for a in args]
    fields["args"] = args

    port = data.get("port")
    if port in ("", None):
        port = None
    else:
        try:
            port = int(port)
        except (TypeError, ValueError):
            raise Invalid("port must be a number")
        if not 1 <= port <= 65535:
            raise Invalid("port out of range")
        clash = next((a for a in existing
                      if a.get("port") == port and a.get("name") != name), None)
        if clash:
            raise Invalid("port %d is already registered to '%s'"
                          % (port, clash["name"]), 409)
    fields["port"] = port

    url = (data.get("url") or "").strip()
    if not url and port:
        url = "http://127.0.0.1:%d/" % port
    fields["url"] = url or None

    match = (data.get("match") or "").strip()
    if match:
        if "\\" in match:
            raise Invalid("match cannot contain backslashes -- use . for the path "
                          "separator and [.] for a literal dot")
        try:
            re.compile(match)
        except re.error as exc:
            raise Invalid("bad match regex: %s" % exc)
    else:
        match = derive_match(stored, args)
    fields["match"] = match

    fields["description"] = (data.get("description") or "").strip()
    fields["autostart"] = bool(data.get("autostart", True))

    warning = None
    if port in UNSAFE_PORTS:
        # Not fatal: a non-browser app is free to use it. But say so now rather
        # than let a blank tab get blamed on the app.
        warning = ("port %d is on the browsers' blocked list -- Chrome will refuse "
                   "it with ERR_UNSAFE_PORT even though the app is running" % port)
    elif not match and not port:
        warning = ("no match pattern and no port: status for '%s' falls back to the "
                   "recorded PID, which is unreliable" % name)
    elif not match:
        warning = ("could not derive a match pattern; status for '%s' relies on the "
                   "port alone" % name)
    return fields, warning


@app.route("/api/apps", methods=["POST"])
def api_add():
    cfg = load_registry()
    try:
        fields, warning = parse_fields(request.get_json(silent=True) or {}, cfg)
    except Invalid as exc:
        return jsonify({"ok": False, "error": str(exc)}), exc.status

    entry = {
        "name": fields["name"],
        "dir": fields["dir"],
        "command": fields["command"],
        "args": fields["args"],
        "port": fields["port"],
        "url": fields["url"],
        "autostart": fields["autostart"],
        "description": fields["description"],
        "match": fields["match"],
        "type": "app",
    }
    cfg.setdefault("apps", []).append(entry)
    save_registry(cfg)
    invalidate_status()
    return jsonify({"ok": True, "app": entry, "warning": warning})


@app.route("/api/apps/<name>", methods=["PUT"])
def api_update(name):
    """Edit a registered app in place, including renaming it.

    A rename is more than a label change: `logs/<name>.log`, `state/<name>.pid`
    and the name-derived icon all follow the app, so a running app is stopped
    first, the files are moved, and it is relaunched under the new name. The
    `type` stays put -- it is what marks the launcher itself.
    """
    cfg = load_registry()
    entry = find_entry(cfg, name)
    if not entry:
        return jsonify({"ok": False, "error": "unknown app"}), 404

    data = request.get_json(silent=True) or {}
    try:
        fields, warning = parse_fields(data, cfg, current=entry)
    except Invalid as exc:
        return jsonify({"ok": False, "error": str(exc)}), exc.status

    wanted = (data.get("name") or "").strip().lower()
    renaming = bool(wanted) and wanted != name
    if renaming:
        if not NAME_RE.match(wanted):
            return jsonify({"ok": False, "error":
                            "name must be lowercase letters, digits, . _ - "
                            "(max 32)"}), 400
        if find_entry(cfg, wanted):
            return jsonify({"ok": False,
                            "error": "'%s' is already registered" % wanted}), 409

    notes = []
    relaunch = False
    if renaming:
        # The launcher cannot stop itself to be relaunched, so its rename is
        # registry-and-files only; its own log file follows on the next restart.
        was_running = any(r.get("name") == name and r.get("status") == "running"
                          for r in read_status())
        relaunch = was_running and not is_self_entry(entry)
        if relaunch:
            ok, msg = engine.stop_app(entry)
            if not ok:
                return jsonify({"ok": False, "error":
                                "could not stop %s to rename it: %s"
                                % (name, msg)}), 500

        problems = rename_side_files(name, wanted, entry)
        notes.extend(problems)
        entry["name"] = wanted

    entry.update(fields)
    save_registry(cfg)
    invalidate_status()

    final = entry["name"]
    if relaunch:
        ok, msg = engine.start_app(entry)
        if ok:
            notes.append("relaunched as %s" % final)
        else:
            notes.append("renamed, but %s did not come back up: %s" % (final, msg))
    elif renaming and is_self_entry(entry):
        notes.append("restart the launcher for its own log files to use the new name")
    elif renaming:
        notes.append("renamed (it was not running)")
    elif not is_self_entry(entry):
        notes.append("restart %s for the change to take effect" % final)

    return jsonify({"ok": True, "app": entry, "warning": warning,
                    "renamed_from": name if renaming else None,
                    "note": "; ".join(notes) or None})


@app.route("/api/apps/<name>", methods=["DELETE"])
def api_remove(name):
    cfg = load_registry()
    entry = find_entry(cfg, name)
    if entry and is_self_entry(entry):
        return jsonify({"ok": False, "error": "the launcher cannot unregister itself"}), 400
    remaining = [a for a in cfg.get("apps", []) if a.get("name") != name]
    if len(remaining) == len(cfg.get("apps", [])):
        return jsonify({"ok": False, "error": "unknown app"}), 404
    cfg["apps"] = remaining
    save_registry(cfg)
    invalidate_status()
    # Unregistering does not stop a running process -- say so rather than
    # leaving an orphan the UI can no longer see.
    return jsonify({"ok": True, "note": "removed from the registry; any running "
                                        "process was left alone"})


@app.route("/api/apps/order", methods=["POST"])
def api_order():
    """Reorder the registry.

    Registry order IS menu and grid order -- there is no separate sort key to
    drift out of step with the file. Requires the full list so a stale page
    can't silently drop an app it never knew about.
    """
    data = request.get_json(silent=True) or {}
    order = data.get("order")
    if not isinstance(order, list):
        return jsonify({"ok": False, "error": "order must be a list of names"}), 400

    cfg = load_registry()
    existing = {a.get("name"): a for a in cfg.get("apps", [])}

    # Tolerant on purpose. Refusing anything but an exact permutation turned a
    # duplicated row -- which a mid-drag re-render can produce -- into a flat
    # rejection of the whole reorder. Unknown and repeated names are ignored,
    # and anything the caller didn't mention keeps its current relative place
    # at the end, so a stale page can reorder but never drop an app.
    ranked, seen = [], set()
    for raw in order:
        name = str(raw)
        if name in existing and name not in seen:
            seen.add(name)
            ranked.append(existing[name])
    missing = [a for a in cfg.get("apps", []) if a.get("name") not in seen]
    if not ranked:
        return jsonify({"ok": False, "error":
                        "none of those names are registered"}), 409

    cfg["apps"] = ranked + missing
    save_registry(cfg)
    invalidate_status()
    return jsonify({"ok": True, "order": [a.get("name") for a in cfg["apps"]]})


@app.route("/api/apps/<name>/autostart", methods=["POST"])
def api_autostart(name):
    data = request.get_json(silent=True) or {}
    cfg = load_registry()
    entry = find_entry(cfg, name)
    if not entry:
        return jsonify({"ok": False, "error": "unknown app"}), 404
    entry["autostart"] = bool(data.get("autostart"))
    save_registry(cfg)
    return jsonify({"ok": True, "autostart": entry["autostart"]})


if __name__ == "__main__":
    # 127.0.0.1 only: this endpoint starts processes, so it must not be
    # reachable from the network.
    app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True)
