#!/usr/bin/env sh
# Start every app with autostart:true, then open the page.
# Runs in your own session, so nothing supervises or reaps it.
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
"$here/devapps" start
url=http://127.0.0.1:5058/
if command -v xdg-open >/dev/null 2>&1; then xdg-open "$url"
elif command -v open >/dev/null 2>&1; then open "$url"
else echo "Open $url"; fi
