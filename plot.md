## Web dashboard

The dashboard is static (Plotly loaded from a CDN) and only needs a file
server — it reads `data/temperatures.csv` and `data/latest.json` via
`fetch()`, which browsers block on `file://` URLs, so it must be served over
HTTP. From the repo root:

`python3 -m http.server 8000`

Then open `http://localhost:8000/web/` in a browser. This is independent of
the daemon (which keeps logging regardless) and of the `tty_display`
setting — the dashboard is an ordinary webpage, no console/tty switching
involved.

Two things that trip people up:

- **This file server is not a systemd service and does not survive a
  reboot.** Unlike `temperature-monitor.service`, nothing restarts it
  automatically — after every reboot, re-run the `python3 -m http.server
  8000` command above before the dashboard URL will load.
- **`localhost` refers to wherever the *browser process* is running, not
  wherever your eyes/keyboard physically are.** If you're viewing this
  machine through a remote desktop tool (RustDesk, VNC, ...), a browser
  opened *inside that remote session* runs on the remote machine, so
  `localhost:8000` correctly reaches the server there. But a browser
  running natively on the machine you're physically sitting at is a
  different `localhost` — in that case use the target machine's actual LAN
  IP (`ip -4 addr show`) instead, e.g. `http://<your-ip-address>:8000/web/`, and
  make sure the two machines are actually on the same network (or a VPN
  bridging them).

Features: one trace per sensor (click a legend entry to hide/show it), zoom
by dragging, the range buttons (Last minute / 10 min / hour / day / All
data), a light/dark theme toggle, and a PNG export button. All times shown
are your browser's local time (data is stored in UTC). Current-value cards
refresh every 5 s from `latest.json`; the chart refetches the CSV every 30 s
(new data only lands as often as `csv_export_interval_seconds`, 60 s by
default, so refreshing faster than that wouldn't show anything new).
