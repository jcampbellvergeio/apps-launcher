# App Launcher for Windows

Starts the local apps you otherwise launch by hand after every reboot, and gives
you a page to see and control them: one registry file, one PowerShell script, one
scheduled task, and a small Flask UI.

Needs PowerShell 5.1 and Python with Flask — nothing else to install.

## What it does

- **Registry** — `apps.json` lists your apps: folder, command, args, port, URL.
- **CLI** — `devapps.ps1 start|stop|restart|status|logs` over that registry.
- **Logon task** — one command registers it, and a reboot brings everything back.
- **Web UI** — a left menu of every app with live status, a management list, and
  each app embedded in a pane when you click it.

## Install

```powershell
git clone https://github.com/<you>/apps-launcher.git launcher
cd launcher
pip install flask
.\devapps.ps1 install      # run at logon, 30s after
python app.py              # then open http://127.0.0.1:5058/
```

`apps.json` is created from `apps.example.json` on first run. A relative `dir` is
resolved against the **parent** of this folder, so the natural layout is a
projects directory with `launcher/` inside it — or use absolute paths and put it
anywhere.

Double-clicking **`Start App Launcher.cmd`** starts every `autostart` app and
opens the page.

## The web UI

`http://127.0.0.1:5058/` — the launcher registers itself, so the logon task
brings the page up with everything else.

**Left menu**: every app with its icon and a live status dot. `‹` collapses it to
an icon rail (remembered per browser); drag the rows to reorder them, which also
sets the order the logon task starts them in.

**The launcher's own row** is a dashboard rather than an embedded page: `Start
all`, `Refresh`, `+ Register an app`, a running count, and a **List / Tiles**
switch over the apps themselves. Each list row carries the endpoint, live state,
a clickable **logon / manual** pill, and `Start`/`Restart`/`Stop`, `Edit`,
`Logs`, `Delete`.

**Any other app** opens embedded in a pane, with its own controls in the topbar
and an `Open ↗` for a real tab. The selection lives in the URL hash
(`#/app/<name>`), so a reload returns to it.

`/status` is the same data as a table, with the signal behind each verdict.

Bound to `127.0.0.1` only, deliberately: the page can start arbitrary commands,
so it must not be reachable from the network.

## CLI

```powershell
.\devapps.ps1 status              # what's up, and how that was determined
.\devapps.ps1 status -Json        # machine-readable (what the web UI calls)
.\devapps.ps1 start               # every app with autostart:true
.\devapps.ps1 start myapp         # one app, autostart flag ignored
.\devapps.ps1 restart myapp
.\devapps.ps1 stop
.\devapps.ps1 logs myapp          # tail stdout + stderr
.\devapps.ps1 install             # run at logon
.\devapps.ps1 uninstall
```

Starting is idempotent — an app already running is left alone, so `start` twice
never gives you two copies fighting over a port.

## Registry

```json
{
  "name": "myapp",
  "dir": "MyApp",                    // folder beside launcher/, or a full path
  "command": "python",
  "args": ["app.py"],                // expanded to a full path at launch
  "port": 5061,                      // null if it isn't a server
  "url": "http://127.0.0.1:5061/",
  "match": "MyApp.app[.]py",         // regex against the process command line
  "autostart": true,
  "type": "app"                      // "app", or "self" for the launcher itself
}
```

Order in the file is order in the UI and in the logon start sequence, so
reordering the menu rewrites this list.

`match` matters more than it looks. Write it with **no backslashes** — use `.`
for a path separator and `[.]` for a literal dot. A bare `\a` in a .NET regex is
the BEL character, not a literal backslash, so `MyApp\app\.py` never matches
anything. The form derives the pattern from the folder and script name, and
rejects a hand-written one containing a backslash.

## How liveness is decided

Three signals, most reliable first. The **Via** column tells you which one
answered, which is how much to trust it.

1. **Command line match** (`match`) — authoritative. Survives a launcher process
   that exits after spawning the real one: a `.vbs` or `.cmd` wrapper does
   exactly that, which makes a recorded PID worthless within milliseconds.
2. **Listening port**, but only if that listener isn't another registered app's
   process. An app that binds a second port — an HTTP→HTTPS redirect listener,
   say — would otherwise be credited to whatever app is registered on it.
3. **Recorded PID**, and only for an app with no `match` pattern. Windows
   recycles PIDs, so a stale pid file can name an unrelated live process, report
   a dead app as running, and then make `start` skip it as already up.

`Via: port` on an app you thought the launcher started means it was started some
other way — or its `match` is wrong.

The web UI doesn't reimplement any of this: it shells out to
`devapps.ps1 status -Json`, so the page and the CLI can't disagree.

## Editing and renaming

**Edit** reopens the registration form with current values. Everything is
editable including the **name**; only `type` is fixed.

**Renaming relaunches the app**, because the name isn't just a label:
`logs/<name>.log`, `logs/<name>.err.log`, `state/<name>.pid` and the
name-derived `static/icons/<name>.svg` all follow it. A running app is stopped,
the files are moved, the registry is rewritten, and it starts again under the new
name. A file that can't be moved is reported rather than aborting the rename. The
launcher itself can't stop itself to be relaunched, so its rename updates the
registry and files only.

**Delete** unregisters an app: it leaves `apps.json` (a `.bak` is kept), the
app's own folder and files are untouched, and a running process keeps running.

## Icons

`static/icons/<name>.svg`, resolved as: an explicit `"icon"` in the registry, then
a file named after the app, then `default.svg`. **Giving an app its own artwork is
just dropping `static/icons/<its-name>.svg` in place** — no code, no registry
edit. A few are included to copy: `inbox`, `gauge`, `braces`, `calendar`.

## Logs

`logs/<app>.log` and `logs/<app>.err.log`, overwritten on each start; **Logs**
shows the tail of both. Flask writes its request log to stderr, so `.err.log` is
usually the interesting one.

**The launcher can only capture what it started itself.** An app you started by
hand has its output in that shell — those are the ones showing `Via: port`. Logs
is offered anyway and says which case it is; restart it from here and capture
begins.

## Notes worth knowing

**Browsers refuse "unsafe" ports.** The UI first ran on 5060 (SIP), which Chrome
and Firefox reject with `ERR_UNSAFE_PORT` while the server is perfectly healthy —
and `curl` connects fine, so only a browser catches it. Hence 5058. The form
warns when a port you pick is on that list.

**Not every app can be embedded.** Before framing one, the page asks
`/api/framable/<name>`, which reads the app's own headers: `X-Frame-Options` or a
CSP `frame-ancestors` means the browser refuses the frame *silently*, so the pane
names the header and offers a new tab instead. HTTPS apps get a warning too — a
self-signed certificate can only be accepted in a real tab, never in a frame.

**Don't capture a pipe when shelling out to the script.** An app it starts
inherits the PowerShell child's handles, so a captured stdout pipe never reaches
EOF while that app lives: `start` hangs forever instead of returning, and
`subprocess`'s timeout doesn't save you either, because its timeout path
re-blocks on the same pipe. `run_script()` writes to temp files and waits on
process exit only.

**This is not a service manager** — no restart-on-crash, no dependency ordering.
These are local apps on one machine; a supervisor would be more moving parts than
the problem justifies.

## Layout

| Path | What |
|---|---|
| `devapps.ps1` | the engine: status / start / stop / restart / logs / install / uninstall |
| `app.py` | the Flask UI; shells out to `devapps.ps1` for everything |
| `templates/`, `static/` | the page; list, tiles and menu are rendered by `static/app.js` |
| `apps.example.json` | the starting registry, copied to `apps.json` on first run |
| `logs/`, `state/` | captured output and recorded PIDs (gitignored) |
