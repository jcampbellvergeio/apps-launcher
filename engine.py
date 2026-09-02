"""App Launcher engine: the registry, liveness, and starting and stopping apps.

One implementation for Windows, Linux and macOS. Both front ends use it -- the
CLI (`devapps.py`) and the web UI (`app.py`) -- so they cannot disagree about
whether an app is running.

Everything platform-specific is isolated in the three sections marked
PLATFORM, and each has a documented reason for existing.

No third-party dependencies are required. `psutil` is used when it happens to be
installed, because it reads process command lines and socket owners far faster
than shelling out; without it there is a fallback per platform.
"""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from xml.sax.saxutils import escape as xml_escape

VERSION = "1.1.0"

ROOT = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(ROOT)           # relative `dir` values resolve against this
APPS_JSON = os.path.join(ROOT, "apps.json")
APPS_EXAMPLE = os.path.join(ROOT, "apps.example.json")
LOG_DIR = os.path.join(ROOT, "logs")
STATE_DIR = os.path.join(ROOT, "state")
ICON_DIR = os.path.join(ROOT, "static", "icons")

WINDOWS = sys.platform.startswith("win")
MACOS = sys.platform == "darwin"

TASK_NAME = "App Launcher at logon"
LEGACY_TASK_NAME = "DevApps at logon"    # pre-rename; install/uninstall clear it
SERVICE_NAME = "app-launcher"            # systemd --user unit
AGENT_LABEL = "io.github.apps-launcher"  # launchd LaunchAgent
LOGON_DELAY = 30                         # seconds; see install_autostart()

SELF_TYPE = "self"
SELF_NAME = "launcher"                   # fallback for a registry predating `type`

try:                                     # 3.11+; only used to read pyproject
    import tomllib
except Exception:                        # pragma: no cover
    tomllib = None

try:                                     # optional, and only ever a fast path
    import psutil
except Exception:                        # pragma: no cover - absence is normal
    psutil = None

# pid -> Popen for apps this process started. Only used to reap them: on
# POSIX a killed child stays a zombie until its parent waits, and a zombie
# still answers os.kill(pid, 0), so an unreaped child looks alive forever.
_spawned = {}

for _d in (LOG_DIR, STATE_DIR):
    os.makedirs(_d, exist_ok=True)


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #

def load_registry():
    # A fresh clone has only the example: seed from it rather than failing, so
    # the launcher comes up on first run.
    if not os.path.exists(APPS_JSON) and os.path.exists(APPS_EXAMPLE):
        shutil.copy2(APPS_EXAMPLE, APPS_JSON)
    with open(APPS_JSON, encoding="utf-8-sig") as fh:
        return json.load(fh)


def save_registry(cfg):
    """Write apps.json, keeping a one-deep backup.

    No BOM: PowerShell reads plain UTF-8 fine, and a BOM shows up as stray
    characters in anything that reads the file as text.
    """
    if os.path.exists(APPS_JSON):
        shutil.copy2(APPS_JSON, APPS_JSON + ".bak")
    tmp = APPS_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(cfg, fh, indent=4)
        fh.write("\n")
    os.replace(tmp, APPS_JSON)


def apps(cfg=None):
    return (cfg or load_registry()).get("apps", [])


def find_entry(cfg, name):
    return next((a for a in apps(cfg) if a.get("name") == name), None)


def entry_type(entry):
    return (entry.get("type") or "app").strip().lower()


def is_self_entry(entry):
    """True for the launcher itself. Type first, name only as a fallback so an
    older apps.json still behaves correctly."""
    return entry_type(entry) == SELF_TYPE or entry.get("name") == SELF_NAME


def app_dir(entry):
    d = entry.get("dir") or ""
    return d if os.path.isabs(d) else os.path.join(PARENT, d)


def rx(text):
    r"""Regex-quote a path fragment WITHOUT using backslashes.

    A backslash before a letter is an escape in every regex flavour involved
    (in .NET `\a` is the BEL character), so a pattern built with backslash
    escapes silently matches nothing. A plain `.` doubles as the path separator
    here, which conveniently matches both `\` and `/`, and a literal dot becomes
    `[.]`.
    """
    return text.replace(".", "[.]")


SAFE_FOR_RX = re.compile(r"^[A-Za-z0-9 _.\-]+$")


def derive_match(directory, args):
    """Build a command-line pattern: '<folder>.<script>', e.g. MyApp.app[.]py."""
    leaf = os.path.basename(os.path.normpath(directory))
    script = next((a for a in args if "." in a and not a.startswith("-")), None)
    if not script or not SAFE_FOR_RX.match(leaf) or not SAFE_FOR_RX.match(script):
        return ""
    pattern = "%s.%s" % (rx(leaf), rx(script))
    try:
        re.compile(pattern)
    except re.error:
        return ""
    return pattern


def rename_side_files(old, new, entry):
    """Move the files named after an app: its logs, its recorded PID, and its
    icon if the icon is the name-derived one.

    Returns a list of human-readable problems. A file that cannot be moved is
    reported rather than fatal -- the registry rename is the part that matters,
    and a stuck log file only costs the old output.
    """
    problems = []
    jobs = [(LOG_DIR, ".log"), (LOG_DIR, ".err.log"), (STATE_DIR, ".pid")]
    # An explicit `icon` in the registry points somewhere deliberate; only the
    # convention-named file follows the rename.
    if not entry.get("icon"):
        jobs.append((ICON_DIR, ".svg"))

    for directory, suffix in jobs:
        src = os.path.join(directory, old + suffix)
        if not os.path.exists(src):
            continue
        try:
            os.replace(src, os.path.join(directory, new + suffix))
        except OSError as exc:
            problems.append("could not move %s%s (%s)"
                            % (old, suffix, exc.__class__.__name__))
    return problems


# --------------------------------------------------------------------------- #
# versions
#
# There is no universal way to ask "what version is this app?", so the resolver
# walks an explicit order and reports which source answered. Nothing is executed
# unless the registry opted in with `version_cmd`: guessing at a command to run
# would be both unreliable and a way to run something unexpected.
# --------------------------------------------------------------------------- #

_version_cache = {}          # name -> (expires_at, {version, source})
VERSION_TTL = 300.0          # seconds; a version rarely changes under you


def _first_line(text):
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line[:60]
    return ""


def _version_from_files(workdir):
    for filename in ("VERSION", "VERSION.txt", "version.txt"):
        path = os.path.join(workdir, filename)
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    value = _first_line(fh.read())
                if value:
                    return value, filename
            except OSError:
                pass

    path = os.path.join(workdir, "package.json")
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8-sig") as fh:
                value = (json.load(fh) or {}).get("version")
            if value:
                return str(value)[:60], "package.json"
        except (OSError, ValueError):
            pass

    path = os.path.join(workdir, "pyproject.toml")
    if os.path.exists(path) and tomllib is not None:
        try:
            with open(path, "rb") as fh:
                data = tomllib.load(fh)
            value = ((data.get("project") or {}).get("version")
                     or ((data.get("tool") or {}).get("poetry") or {}).get("version"))
            if value:
                return str(value)[:60], "pyproject.toml"
        except (OSError, ValueError):
            pass
    return None, None


def _version_from_git(workdir):
    if not os.path.isdir(os.path.join(workdir, ".git")):
        return None, None
    if not shutil.which("git"):
        return None, None
    try:
        res = subprocess.run(
            ["git", "-C", workdir, "describe", "--tags", "--always", "--dirty"],
            capture_output=True, text=True, timeout=10,
            creationflags=0x08000000 if WINDOWS else 0)
    except (OSError, subprocess.SubprocessError):
        return None, None
    value = _first_line(res.stdout)
    return (value, "git describe") if value else (None, None)


def app_version(entry, use_cache=True):
    """{version, source} for one app. Empty version means "not determinable"."""
    name = entry.get("name") or ""
    if use_cache:
        cached = _version_cache.get(name)
        if cached and cached[0] > time.time():
            return cached[1]

    result = {"version": "", "source": ""}

    if is_self_entry(entry):
        result = {"version": VERSION, "source": "App Launcher"}
    elif entry.get("version"):
        # A literal in the registry: whatever you typed, used as-is.
        result = {"version": str(entry["version"])[:60], "source": "registry"}
    else:
        workdir = app_dir(entry)
        cmd = entry.get("version_cmd")
        if cmd and os.path.isdir(workdir):
            try:
                res = subprocess.run(
                    cmd, shell=True, cwd=workdir, capture_output=True, text=True,
                    timeout=10, stdin=subprocess.DEVNULL,
                    creationflags=0x08000000 if WINDOWS else 0)
                # Some tools print their version to stderr.
                value = _first_line(res.stdout) or _first_line(res.stderr)
                if value:
                    result = {"version": value, "source": "version_cmd"}
            except (OSError, subprocess.SubprocessError):
                pass
        if not result["version"] and os.path.isdir(workdir):
            value, source = _version_from_files(workdir)
            if not value:
                value, source = _version_from_git(workdir)
            if value:
                result = {"version": value, "source": source}

    _version_cache[name] = (time.time() + VERSION_TTL, result)
    return result


def forget_version(name=None):
    """Drop cached versions -- after a restart or an edit, the answer may differ."""
    if name is None:
        _version_cache.clear()
    else:
        _version_cache.pop(name, None)


def versions(cfg=None):
    return {e.get("name"): app_version(e) for e in apps(cfg or load_registry())}


# --------------------------------------------------------------------------- #
# PLATFORM 1: reading process command lines
#
# The command line is the only reliable way to identify an app, because a
# wrapper process can exit right after spawning the real one, which makes a
# recorded PID worthless within milliseconds.
# --------------------------------------------------------------------------- #

def _processes_psutil():
    out = []
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            argv = proc.info.get("cmdline") or []
        except Exception:
            continue
        if argv:
            out.append((proc.info["pid"], " ".join(argv)))
    return out


def _processes_posix():
    # `ps` is in POSIX and present on both Linux and macOS. `args` last, since
    # it is the field that contains spaces.
    try:
        res = subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True,
                             text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return []
    out = []
    for line in res.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        pid, _, argv = line.partition(" ")
        if pid.isdigit() and argv.strip():
            out.append((int(pid), argv.strip()))
    return out


def _processes_windows():
    # Without psutil there is no cheap way to read another process's command
    # line on Windows: tasklist doesn't report it and wmic is deprecated, so
    # this asks PowerShell. It costs a second or two -- install psutil to skip
    # this path entirely.
    script = ("Get-CimInstance Win32_Process | "
              "Where-Object { $_.CommandLine } | "
              "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }")
    try:
        res = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-Command", script],
            capture_output=True, text=True, timeout=60,
            creationflags=0x08000000)          # CREATE_NO_WINDOW
    except (OSError, subprocess.SubprocessError):
        return []
    out = []
    for line in res.stdout.splitlines():
        pid, _, cmd = line.partition("\t")
        if pid.strip().isdigit() and cmd.strip():
            out.append((int(pid.strip()), cmd.strip()))
    return out


def list_processes():
    """[(pid, command line)] for every process we can see."""
    if psutil is not None:
        try:
            return _processes_psutil()
        except Exception:
            pass
    return _processes_windows() if WINDOWS else _processes_posix()


# --------------------------------------------------------------------------- #
# PLATFORM 2: who is listening on a port
# --------------------------------------------------------------------------- #

def port_is_open(port, host="127.0.0.1", timeout=0.4):
    """Is something accepting connections there?

    A plain connect works on every platform and needs no privileges, which is
    why it is the primary check rather than enumerating sockets: on macOS,
    listing another process's sockets requires root.
    """
    if not port:
        return False
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def port_owner(port):
    """PID listening on `port`, or None if that can't be determined.

    None does not mean nothing is listening -- see port_is_open(). Identity is
    a bonus here; the port check only needs to know that something answers.
    """
    if not port:
        return None
    port = int(port)

    if psutil is not None:
        try:
            for conn in psutil.net_connections(kind="inet"):
                if (conn.status == psutil.CONN_LISTEN and conn.laddr
                        and conn.laddr.port == port and conn.pid):
                    return conn.pid
        except Exception:
            pass          # macOS raises AccessDenied without root; fall through

    if WINDOWS:
        script = ("(Get-NetTCPConnection -LocalPort %d -State Listen "
                  "-ErrorAction SilentlyContinue | "
                  "Select-Object -First 1).OwningProcess" % port)
        try:
            res = subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-Command", script],
                capture_output=True, text=True, timeout=30,
                creationflags=0x08000000)
            value = res.stdout.strip()
            return int(value) if value.isdigit() else None
        except (OSError, subprocess.SubprocessError, ValueError):
            return None

    # lsof is the portable POSIX answer and is usually present; ss covers a
    # stripped-down Linux box.
    for cmd in (["lsof", "-nP", "-iTCP:%d" % port, "-sTCP:LISTEN", "-t"],
                ["ss", "-lntpH", "sport = :%d" % port]):
        if not shutil.which(cmd[0]):
            continue
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            continue
        if cmd[0] == "lsof":
            first = res.stdout.split()
            if first and first[0].isdigit():
                return int(first[0])
        else:
            hit = re.search(r"pid=(\d+)", res.stdout)
            if hit:
                return int(hit.group(1))
    return None


# --------------------------------------------------------------------------- #
# liveness
# --------------------------------------------------------------------------- #

def _matching_pids(pattern, processes):
    if not pattern:
        return []
    try:
        rex = re.compile(pattern)
    except re.error:
        return []
    return [pid for pid, cmd in processes if rex.search(cmd)]


def claimed_pids(cfg, except_name, processes):
    """PIDs already accounted for by some OTHER registered app.

    Without this, an app that binds a second port -- an HTTP->HTTPS redirect
    listener, say -- gets credited to whichever app is registered on that port.
    Every matching PID counts, not just the first: the extra listener often
    runs in a second process.
    """
    claimed = []
    for other in apps(cfg):
        if other.get("name") == except_name:
            continue
        claimed.extend(_matching_pids(other.get("match"), processes))
    return claimed


def recorded_pid(entry):
    """The weakest signal, and the only one that can name the WRONG process.

    PIDs are recycled, so a stale file can point at something unrelated: that
    misreports the app as running and, worse, makes `start` skip it as already
    up. An app with a `match` pattern never needs this step -- we launch with
    full paths precisely so the command line is identifiable, so if the match
    found nothing the app is not running and the file is stale.
    """
    path = os.path.join(STATE_DIR, entry.get("name", "") + ".pid")
    if not os.path.exists(path):
        return None
    if entry.get("match"):
        os.remove(path)
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            pid = int((fh.read() or "").strip())
    except (OSError, ValueError):
        return None
    if pid_alive(pid):
        return pid
    os.remove(path)
    return None


def _is_zombie(pid):
    """Has the process exited but not yet been reaped?

    A zombie is gone for every practical purpose -- but it still has a PID
    entry, so os.kill(pid, 0) succeeds and it reads as alive. Windows has no
    equivalent state.
    """
    if WINDOWS:
        return False
    if psutil is not None:
        try:
            return psutil.Process(int(pid)).status() == psutil.STATUS_ZOMBIE
        except Exception:
            return False
    try:                              # Linux: field 3 of /proc/<pid>/stat
        with open("/proc/%d/stat" % int(pid), encoding="utf-8") as fh:
            fields = fh.read().rsplit(") ", 1)[-1].split()
        return bool(fields) and fields[0] == "Z"
    except OSError:
        pass
    try:                              # macOS and anything else with ps
        res = subprocess.run(["ps", "-o", "state=", "-p", str(int(pid))],
                             capture_output=True, text=True, timeout=5)
        return res.stdout.strip().startswith("Z")
    except (OSError, subprocess.SubprocessError, ValueError):
        return False


def _reap(pid):
    """Collect a child we started, so it stops being a zombie."""
    proc = _spawned.pop(int(pid), None)
    if proc is not None:
        try:
            proc.wait(timeout=5)
            return
        except Exception:
            pass
    if not WINDOWS:
        try:
            os.waitpid(int(pid), os.WNOHANG)
        except (ChildProcessError, OSError, ValueError):
            pass                      # not our child, so someone else reaps it


def pid_alive(pid):
    if not pid:
        return False
    if psutil is not None:
        try:
            proc = psutil.Process(int(pid))
            return proc.status() != psutil.STATUS_ZOMBIE
        except Exception:
            return False
    if WINDOWS:
        return any(p == int(pid) for p, _ in list_processes())
    try:
        os.kill(int(pid), 0)          # signal 0 only tests for existence
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                   # exists, owned by someone else
    except (OSError, ValueError):
        return False
    return not _is_zombie(pid)


def app_state(entry, cfg=None, processes=None):
    """{running, pid, via} for one app. `via` says which signal answered."""
    cfg = cfg or load_registry()
    processes = list_processes() if processes is None else processes

    # 1. Command line -- authoritative.
    hits = _matching_pids(entry.get("match"), processes)
    if hits:
        return {"running": True, "pid": hits[0], "via": "match"}

    # 2. Listening port, unless that listener is another registered app.
    port = entry.get("port")
    if port and port_is_open(port):
        owner = port_owner(port)
        if owner is None or owner not in claimed_pids(cfg, entry.get("name"), processes):
            return {"running": True, "pid": owner, "via": "port"}

    # 3. Recorded PID, for an app with no match pattern.
    pid = recorded_pid(entry)
    if pid:
        return {"running": True, "pid": pid, "via": "pid"}

    return {"running": False, "pid": None, "via": "none"}


def statuses(cfg=None):
    cfg = cfg or load_registry()
    processes = list_processes()
    rows = []
    for entry in apps(cfg):
        state = app_state(entry, cfg, processes)
        rows.append({
            "name": entry.get("name"),
            "status": "running" if state["running"] else "stopped",
            "pid": state["pid"],
            "via": state["via"],
            "port": entry.get("port"),
            "autostart": bool(entry.get("autostart")),
            "url": entry.get("url"),
        })
    return rows


# --------------------------------------------------------------------------- #
# PLATFORM 3: starting and stopping
# --------------------------------------------------------------------------- #

def _launch_argv(entry, workdir):
    """The argv to run, with script arguments expanded to absolute paths.

    Expansion matters: the command line is what identifies the app later, and a
    bare `app.py` is not identifiable.
    """
    command = entry.get("command") or sys.executable
    argv = [command]
    for arg in entry.get("args") or []:
        candidate = os.path.join(workdir, str(arg))
        argv.append(candidate if os.path.exists(candidate) else str(arg))

    resolved = shutil.which(command)
    if resolved:
        argv[0] = resolved
        # A Windows .cmd/.bat is a script, not an image: it needs an interpreter.
        if WINDOWS and resolved.lower().endswith((".cmd", ".bat")):
            argv = ["cmd.exe", "/c"] + argv
    return argv


def start_app(entry):
    """Start one app. Returns (ok, message).

    Idempotent: an app already running is left alone, so calling this twice
    never produces two copies fighting over a port.
    """
    name = entry.get("name")
    state = app_state(entry)
    if state["running"]:
        return True, "already running (pid %s)" % state["pid"]

    workdir = app_dir(entry)
    if not os.path.isdir(workdir):
        return False, "folder not found: %s" % workdir

    out_path = os.path.join(LOG_DIR, name + ".log")
    err_path = os.path.join(LOG_DIR, name + ".err.log")
    kwargs = {"cwd": workdir, "stdin": subprocess.DEVNULL}
    if WINDOWS:
        # DETACHED_PROCESS: no console of its own, and it outlives us.
        kwargs["creationflags"] = 0x00000008 | 0x00000200   # + NEW_PROCESS_GROUP
    else:
        # Its own session, so it survives this process and can be signalled as
        # a group (a dev server's reloader child included).
        kwargs["start_new_session"] = True

    try:
        with open(out_path, "w", encoding="utf-8") as out, \
                open(err_path, "w", encoding="utf-8") as err:
            proc = subprocess.Popen(_launch_argv(entry, workdir),
                                    stdout=out, stderr=err, **kwargs)
    except OSError as exc:
        return False, "could not start: %s" % exc

    _spawned[proc.pid] = proc
    with open(os.path.join(STATE_DIR, name + ".pid"), "w", encoding="utf-8") as fh:
        fh.write(str(proc.pid))

    port = entry.get("port")
    if not port:
        return True, "started (pid %d)" % proc.pid

    # Confirm it actually bound rather than dying on startup: a crash loop
    # should be reported here, not discovered later in a browser.
    for _ in range(20):
        time.sleep(0.5)
        if port_is_open(port):
            return True, "started (pid %d) -> %s" % (proc.pid, entry.get("url") or port)
        if proc.poll() is not None:
            return False, ("exited immediately (code %s) -- see logs/%s.err.log"
                           % (proc.returncode, name))
    return False, "started but nothing on port %s -- see logs/%s.err.log" % (port, name)


def _terminate(pid):
    if psutil is not None:
        try:
            proc = psutil.Process(int(pid))
            targets = proc.children(recursive=True) + [proc]
            for target in targets:
                try:
                    target.terminate()
                except Exception:
                    pass
            _, alive = psutil.wait_procs(targets, timeout=5)
            for target in alive:
                try:
                    target.kill()
                except Exception:
                    pass
            _spawned.pop(int(pid), None)
            return True
        except Exception:
            pass

    if WINDOWS:
        res = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                             capture_output=True, text=True,
                             creationflags=0x08000000)
        _reap(pid)
        return res.returncode == 0

    import signal
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            # The process group, so a reloader child goes with it. start_app
            # made the app a group leader for exactly this.
            os.killpg(os.getpgid(int(pid)), sig)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                os.kill(int(pid), sig)
            except (ProcessLookupError, OSError):
                return True
        for _ in range(10):
            _reap(pid)                # else our own child lingers as a zombie
            if not pid_alive(pid):
                return True
            time.sleep(0.3)
    _reap(pid)
    return not pid_alive(pid)


def stop_app(entry):
    """Stop one app. Returns (ok, message)."""
    name = entry.get("name")
    state = app_state(entry)
    if not state["running"]:
        return True, "not running"
    pid = state["pid"]
    if not pid:
        return False, ("something is listening on port %s but its process could "
                       "not be identified -- stop it where it was started"
                       % entry.get("port"))
    ok = _terminate(pid)
    try:
        os.remove(os.path.join(STATE_DIR, name + ".pid"))
    except OSError:
        pass
    return (True, "stopped (pid %s)" % pid) if ok else (False, "could not stop pid %s" % pid)


def logs_tail(name, limit=8000):
    """Tail of both streams, or an explanation of why there is nothing."""
    chunks = []
    for suffix in (".log", ".err.log"):
        path = os.path.join(LOG_DIR, name + suffix)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8", errors="replace") as fh:
            tail = fh.read()[-limit:]
        if tail.strip():
            chunks.append("--- %s%s ---\n%s" % (name, suffix, tail))
    if chunks:
        return "\n\n".join(chunks)

    cfg = load_registry()
    entry = find_entry(cfg, name)
    if not entry:
        return "No such app."
    state = app_state(entry, cfg)
    if state["running"] and state["via"] != "match":
        return ("No captured output for %s.\n\n"
                "It is running, but not from a process the launcher started -- "
                "status found it by %s, not by matching a command line it "
                "launched. Its output went to whatever shell started it.\n\n"
                "Restart it here and the launcher will capture stdout and stderr "
                "to logs/%s.log and logs/%s.err.log."
                % (name, "its listening port" if state["via"] == "port"
                   else "a recorded PID", name, name))
    if state["running"]:
        return ("No output captured for %s yet -- it has written nothing to "
                "stdout or stderr since it started." % name)
    return ("No log files for %s.\n\nIt is not running, and the launcher has not "
            "started it since the last time the logs were cleared. Logs appear at "
            "logs/%s.log once it is started from here." % (name, name))


# --------------------------------------------------------------------------- #
# autostart at login
# --------------------------------------------------------------------------- #

def _start_command():
    """The command line that starts every autostart app."""
    return [sys.executable, os.path.join(ROOT, "devapps.py"), "start"]


SYSTEMD_UNIT = """[Unit]
Description=App Launcher: start local apps at login
After=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
TimeoutStartSec=0
# The delay is deliberate: an app that polls something on startup can lose its
# first DNS lookup to a network stack that is not up yet.
ExecStartPre=/bin/sleep {delay}
ExecStart={command}
WorkingDirectory={root}

[Install]
WantedBy=default.target
"""

LAUNCHD_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/sh</string>
    <string>-c</string>
    <!-- launchd has no start delay of its own, and the wait is deliberate: an
         app that polls something on startup can lose its first DNS lookup to a
         network stack that is not up yet. -->
    <string>sleep {delay}; exec {command}</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>WorkingDirectory</key>
  <string>{root}</string>
  <key>StandardOutPath</key>
  <string>{log}</string>
  <key>StandardErrorPath</key>
  <string>{errlog}</string>
</dict>
</plist>
"""


def autostart_paths():
    """Where this platform's autostart definition lives."""
    home = os.path.expanduser("~")
    if WINDOWS:
        return {"kind": "scheduled task", "path": TASK_NAME}
    if MACOS:
        return {"kind": "launchd agent",
                "path": os.path.join(home, "Library", "LaunchAgents",
                                     AGENT_LABEL + ".plist")}
    return {"kind": "systemd user unit",
            "path": os.path.join(home, ".config", "systemd", "user",
                                 SERVICE_NAME + ".service")}


def _quote(argv):
    return " ".join('"%s"' % a if " " in a else a for a in argv)


def install_autostart(dry_run=False):
    """Register 'start every autostart app' to run at login.

    Returns (ok, message). `dry_run` renders the definition without touching
    the system, which is how the POSIX output can be reviewed from anywhere.
    """
    target = autostart_paths()
    argv = _start_command()

    if WINDOWS:
        # schtasks rather than PowerShell, so the engine has one less dependency
        # on a shell. /DELAY needs the ONLOGON schedule.
        cmd = ["schtasks", "/Create", "/TN", TASK_NAME,
               "/TR", _quote(argv), "/SC", "ONLOGON",
               # schtasks /DELAY takes mmmm:ss.
               "/DELAY", "%04d:%02d" % (LOGON_DELAY // 60, LOGON_DELAY % 60),
               "/RL", "LIMITED", "/F"]
        if dry_run:
            return True, " ".join(cmd)
        res = subprocess.run(cmd, capture_output=True, text=True,
                             creationflags=0x08000000)
        if res.returncode != 0:
            return False, (res.stderr or res.stdout).strip()
        subprocess.run(["schtasks", "/Delete", "/TN", LEGACY_TASK_NAME, "/F"],
                       capture_output=True, text=True, creationflags=0x08000000)
        return True, "registered scheduled task '%s'" % TASK_NAME

    if MACOS:
        body = LAUNCHD_PLIST.format(
            label=xml_escape(AGENT_LABEL), delay=LOGON_DELAY,
            command=xml_escape(_quote(argv)), root=xml_escape(ROOT),
            log=xml_escape(os.path.join(LOG_DIR, "autostart.log")),
            errlog=xml_escape(os.path.join(LOG_DIR, "autostart.err.log")))
    else:
        body = SYSTEMD_UNIT.format(delay=LOGON_DELAY, command=_quote(argv), root=ROOT)

    if dry_run:
        return True, "%s\n\n%s" % (target["path"], body)

    os.makedirs(os.path.dirname(target["path"]), exist_ok=True)
    with open(target["path"], "w", encoding="utf-8", newline="\n") as fh:
        fh.write(body)

    if MACOS:
        subprocess.run(["launchctl", "unload", target["path"]],
                       capture_output=True, text=True)
        res = subprocess.run(["launchctl", "load", "-w", target["path"]],
                             capture_output=True, text=True)
        if res.returncode != 0:
            return False, (res.stderr or res.stdout).strip()
        return True, "loaded launchd agent %s" % target["path"]

    subprocess.run(["systemctl", "--user", "daemon-reload"],
                   capture_output=True, text=True)
    res = subprocess.run(["systemctl", "--user", "enable", "--now",
                          SERVICE_NAME + ".service"], capture_output=True, text=True)
    if res.returncode != 0:
        return False, ((res.stderr or res.stdout).strip()
                       + "\n(a headless box may need: loginctl enable-linger $USER)")
    return True, "enabled systemd user unit %s" % target["path"]


def uninstall_autostart():
    target = autostart_paths()
    if WINDOWS:
        removed = []
        for task in (TASK_NAME, LEGACY_TASK_NAME):
            res = subprocess.run(["schtasks", "/Delete", "/TN", task, "/F"],
                                 capture_output=True, text=True,
                                 creationflags=0x08000000)
            if res.returncode == 0:
                removed.append(task)
        return True, ("removed " + ", ".join("'%s'" % t for t in removed)
                      if removed else "no scheduled task to remove")

    if not os.path.exists(target["path"]):
        return True, "no %s to remove" % target["kind"]
    if MACOS:
        subprocess.run(["launchctl", "unload", "-w", target["path"]],
                       capture_output=True, text=True)
    else:
        subprocess.run(["systemctl", "--user", "disable", "--now",
                        SERVICE_NAME + ".service"], capture_output=True, text=True)
    os.remove(target["path"])
    return True, "removed %s" % target["path"]
