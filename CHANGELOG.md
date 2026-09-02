# Changelog

## v1.2.0

**Display names.** An entry now has a `label` for humans and an `id` for
machines. Type "Sample Apps Launcher" and the id — `sample-apps-launcher` — is
derived for you, because the id is what filenames, the CLI and `/open/<id>` use
and nobody should have to hand-write a filename-safe string. The form previews
the id as you type, and a clashing name gets `-2` rather than a rejection. A
label can be changed freely; changing an id is still the rename path that moves
files and relaunches the app.

**Documents.** A `type: "file"` entry is a page to open rather than a process to
run — a generated dashboard, a report, a set of notes. It carries a *document*
badge instead of a status lamp, its version column is the date the file last
changed, and start/stop/logs refuse it by name instead of failing obscurely. Its
dot is green while the file is there and red once it isn't.

The launcher serves these itself at `/open/<id>`, which is the point rather than
a detail: a browser refuses a `file://` URL both in an iframe and as a link from
an http page, so a registered document could otherwise be listed and never
opened.

**A tidier menu.** Documents get their own **Files** group. Each heading carries
the `+` for its own kind and folds away, remembered per browser. The launcher no
longer lists itself among the apps it manages — its port, path, command and logs
are behind **Launcher settings**. The separate "Apps" nav item is gone: three
doors opened onto one room.

**Every row shows its path**, truncated from the left so the end — the part that
identifies it — stays visible.

**The status page is gone.** Its columns are all in the list rows now, so it was
a second place to read the same data and a second render path to keep in step.

### Fixes

- **Linux and macOS now actually work.** `python` and `python3` stand in for each
  other, so a registry written on Windows starts its apps on a distribution that
  ships only `python3` — previously every app failed with a bare
  `No such file or directory: 'python'`.
- **`stop` no longer reports failure on a process it killed.** On POSIX a killed
  child stays a zombie until its parent reaps it, and a zombie still answers
  `os.kill(pid, 0)`. This bit the web UI hardest, where start and stop share a
  process.
- **Windows `install` works unelevated again.** It had moved to `schtasks`,
  which fails with "Access is denied" on the task root for a normal user;
  registering a per-user login item should never need an admin prompt.
- **The Docker demo serves.** It bound loopback *inside* the container, where the
  port mapping could not reach it.
- An app identified by port rather than command line now carries an **external**
  badge: it runs, but the launcher is not capturing its output — previously
  invisible.
- The external badge no longer overlaps the autostart pill.

### Housekeeping

- Renamed to **Apps Launcher** throughout, including the scheduled task;
  `install` and `uninstall` clear both former task names so no machine ends up
  running two.
- `docs/sample.html` renders the README screenshot from the real CSS and JS with
  an invented fleet, so the image cannot drift from the code and no real paths
  can end up in it.
- `CHANGELOG.md`, and `tests/smoke.py` grew to 29 checks.

## v1.1.0

**Linux and macOS support.** The engine moved from PowerShell to Python
(`engine.py`), which the web UI imports directly instead of shelling out — one
implementation for all three platforms, so the CLI and the page cannot disagree
about whether an app is running. Platform-specific code is confined to three
marked sections: reading process command lines, finding a port's listener, and
starting and stopping detached.

**Autostart per platform**, all with the same deliberate 30-second delay: a
scheduled task on Windows, a `systemd --user` unit on Linux, a launchd
LaunchAgent on macOS. `install --dry-run` renders the definition without
touching anything.

**Version numbers** for the launcher and for each app, resolved from an explicit
source order — a registry literal, a configured `version_cmd`,
`VERSION`/`package.json`/`pyproject.toml`, then `git describe` — reporting which
source answered. Nothing is executed unless the registry opted in.

**An embedded viewer**: clicking an app shows it in a pane, with a server-side
check of `X-Frame-Options` and CSP first, so an app that refuses framing is
explained rather than shown as a blank frame.

- CI on Ubuntu, macOS and Windows, each with and without `psutil`, so the fast
  path and the per-platform fallbacks are both exercised.
- A Docker try-it image with two demo apps. Deliberately not a deployment: a
  container cannot manage host processes or install a host login item.
- Status sweeps list processes once instead of once per app: ~2s to ~0.5s, and
  milliseconds with `psutil`.
- `devapps.ps1` became a thin wrapper, so an existing scheduled task and typing
  habits keep working.

## v1.0.0

First release. A registry (`apps.json`), a PowerShell engine, a logon scheduled
task, and a Flask UI with a collapsible app menu, a management list, and each
app embedded in a pane.

Liveness is judged by command-line match first, then a listening port no other
registered app owns, then a recorded PID — and only for apps without a match
pattern, because Windows recycles PIDs.
