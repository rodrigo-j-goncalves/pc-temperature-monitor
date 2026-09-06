# Temperature Monitor

A Linux daemon that discovers every available temperature sensor (CPU, NVMe,
GPU, NICs, ...), samples them at a configurable interval (default: every 30
minutes), and stores the history in SQLite. A static Plotly dashboard
visualizes the data, and a live view can optionally be shown on a spare
virtual terminal.

```
.
├── monitor.py                          # main daemon (sampling loop, storage, tty3 display)
├── sensors.py                          # sensor discovery and reading (hwmon, nvidia-smi)
├── config.yaml                         # sampling interval, paths, tty3 settings
├── data/
│   ├── temperatures.db                 # primary store: tidy SQLite table (created at runtime)
│   ├── temperatures.csv                # wide CSV export, pandas/R/Excel-readable (generated)
│   └── latest.json                     # current values, updated every tick (generated)
├── web/
│   ├── index.html                      # Plotly dashboard
│   ├── app.js
│   └── style.css
├── systemd/
│   └── temperature-monitor.service
└── README.md
```

## Quick start (setting this up on a new machine)

These are the steps to go from a fresh clone/copy of this repo to a running,
boot-persistent daemon with a dashboard. Each step is explained in more
detail further down.

1. Copy or clone this repo to wherever you want it to live, e.g.
   `~/temperature-monitor`.
2. Install the one non-stdlib dependency: `pip install pyyaml` (or your
   distro's `python3-yaml` package).
3. (Optional) Edit `config.yaml` — sampling interval, paths, tty3 display.
   See [Configuration](#configuration).
4. Edit `systemd/temperature-monitor.service`: replace both occurrences of
   the absolute path in `ExecStart` with the actual path where you put this
   repo (see [Installing as a systemd service](#installing-as-a-systemd-service)).
5. Install and enable the service (needs root):
   ```bash
   sudo cp systemd/temperature-monitor.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now temperature-monitor.service
   ```
6. Serve and open the dashboard:
   ```bash
   python3 -m http.server 8000
   ```
   then open `http://localhost:8000/web/` in a browser.

That's it — sensors are discovered automatically, so nothing needs to be
told which sensors exist on the new machine.

## How it works

- **Sensor discovery** (`sensors.py`) walks `/sys/class/hwmon` directly (no
  dependency on the `sensors` CLI) and separately probes `nvidia-smi` for
  NVIDIA GPUs, which aren't exposed under hwmon. Discovery re-runs
  periodically so sensors that appear or disappear at runtime (a hot-plugged
  NVMe drive, a GPU driver reload, ...) are picked up without restarting the
  daemon. A failed read for one sensor never crashes the tick — it's simply
  omitted. This is also why nothing needs to be reconfigured per-machine:
  the same `sensors.py` adapts to whatever hardware it finds.
- **Storage** (`monitor.py`) is a tidy/long SQLite table
  (`timestamp, sensor_id, value_c`), one row per sensor per tick. This is
  what makes "sensors appearing/disappearing" safe: there's no fixed set of
  columns to migrate.
- **CSV export** is a wide pivot of that table (one column per sensor),
  regenerated periodically. The header is the union of every sensor ever
  seen, so it only grows — a flaky sensor cycling on and off doesn't trigger
  repeated full rewrites.
- **`latest.json`** holds only the most recent reading per sensor, written
  atomically every tick, for the dashboard's current-value cards and the
  tty3 display.
- Timestamps are UTC internally, e.g. `2026-08-05T17:43:12.123456Z` —
  sortable as plain text and parsed natively by pandas/R. The web dashboard
  converts to your browser's local time for display only; the stored files
  stay UTC.
- The SQLite database grows without bound — there's no automatic pruning of
  old rows. At the default 30-minute interval with ~13 sensors this is
  small for years; if you lower the interval a lot (see below), watch
  `data/temperatures.db`'s size over time.

## Requirements

- Python 3.10+ (uses `sqlite3`, `csv`, `json`, `argparse` — all standard
  library).
- [PyYAML](https://pyyaml.org/) for `config.yaml` (`pip install pyyaml`, or
  your distro's `python3-yaml` package). This is the one non-stdlib
  dependency in the whole project.
- `nvidia-smi` on `PATH` if you have an NVIDIA GPU and want its temperature
  logged (optional — its absence is handled gracefully).

## Configuration

Edit `config.yaml`:

| Key | Meaning |
|---|---|
| `sample_interval_seconds` | How often sensors are read and written to SQLite. |
| `sensor_rescan_interval_seconds` | How often hwmon/nvidia-smi are re-probed for new/removed sensors. |
| `data_dir` | Directory (relative to `config.yaml`, or absolute) holding the DB, CSV, and JSON. |
| `sqlite_filename`, `csv_filename`, `latest_json_filename` | Output filenames inside `data_dir`. |
| `csv_export_interval_seconds` | How often new SQLite rows are appended to the CSV. |
| `tty_display.enabled` | Show a live text readout on a virtual terminal. |
| `tty_display.device` | Which tty device to write to (default `/dev/tty3`). |
| `tty_display.refresh_interval_seconds` | How often the tty view redraws. |
| `log_level` | Python logging level (`INFO`, `DEBUG`, ...). |

### Changing the sampling interval

1. Edit `sample_interval_seconds` in `config.yaml`. It's in seconds, e.g.:
   ```yaml
   sample_interval_seconds: 1800   # 30 minutes
   ```
   Other common values: `60` (1 min), `300` (5 min), `3600` (1 hour).
2. Apply it by restarting the service:
   ```bash
   sudo systemctl restart temperature-monitor.service
   ```
   (If running manually instead of as a service, just restart `monitor.py`.)
3. Optionally check it picked up cleanly:
   ```bash
   systemctl status temperature-monitor.service
   journalctl -u temperature-monitor.service -n 10
   ```

`sensor_rescan_interval_seconds` and `csv_export_interval_seconds` are
independent of the sampling interval and don't need to change — if they end
up shorter than `sample_interval_seconds`, that just means occasional
no-op checks (e.g. "any new rows to export?" → no), which is harmless.

## Running manually

```bash
python3 monitor.py --config config.yaml
```

Stop with Ctrl-C (or `SIGTERM`) — the daemon exports any pending CSV rows
before exiting.

If `tty_display.enabled` is `true`, the daemon must run as root (or with
write access to `/dev/tty3`) to draw on the console. Without that access it
logs one warning and disables the tty view for the rest of the run, rather
than crashing.

## Installing as a systemd service

The unit file in `systemd/temperature-monitor.service` has this project's
absolute path baked into `ExecStart` (systemd unit files don't expand `~` or
relative paths). **Before installing on a new machine or from a fresh
clone, edit both path occurrences in that file** to match where you put the
repo, e.g.:

```bash
sed -i 's|/path/to/temperature-monitor-logger|/actual/path/to/temperature-monitor-logger|g' systemd/temperature-monitor.service
```

(or just open the file and edit the two `ExecStart` paths by hand).

It runs as `root` (needed for `/dev/tty3` and, on some systems, certain
hwmon nodes), restarts automatically on crash, and sends its logs to the
journal.

```bash
sudo cp systemd/temperature-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now temperature-monitor.service
```

Check status and logs:

```bash
systemctl status temperature-monitor.service
journalctl -u temperature-monitor.service -f
```

If you use the tty3 live display, free that console from the login prompt
first so the daemon isn't fighting `getty` for it:

```bash
sudo systemctl disable --now getty@tty3.service
```

View the live display by switching to that virtual terminal with
`Ctrl+Alt+F3` (the exact key varies by desktop environment).

If you don't want/need the tty3 display, set `tty_display.enabled: false` in
`config.yaml` and you can instead run the service as an unprivileged user
(remove `User=root` from the unit file, or set it to your username) as long
as that user can read the relevant `/sys/class/hwmon/**` files, which is
normally world-readable. This also avoids `data/temperatures.db` ending up
root-owned, which otherwise means any write-class DB operation (manual
`VACUUM`, checkpoint, editing rows) needs `sudo` — plain reads work fine
either way.

## Running this on more than one machine

Each machine gets its own independent clone of this repo, its own local
`data/` directory (already gitignored, so per-machine data never collides in
git), and its own systemd unit installed with that machine's actual path
substituted in (see [Installing as a systemd
service](#installing-as-a-systemd-service) above). Nothing in `config.yaml`
itself is machine-specific — sensor discovery is automatic, so the same code
adapts to whatever hardware it finds.

To roll out a fix or feature to every machine: commit and push once from
wherever you're working, then on each other machine `git pull` and restart
the service.

## Web dashboard

The dashboard is static (Plotly loaded from a CDN) and only needs a file
server — it reads `data/temperatures.csv` and `data/latest.json` via
`fetch()`, which browsers block on `file://` URLs, so it must be served over
HTTP. From the repo root:

```bash
python3 -m http.server 8000
```

Then open `http://<host>:8000/web/` in a browser. This is independent of
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
by dragging, the range buttons (Last day / 3 days / 7 days / All data), a
From/To date picker for an arbitrary range, a light/dark theme toggle, and a
PNG export button. All times shown are your browser's local time (data is
stored in UTC). Current-value cards refresh every 5 s from `latest.json`;
the chart refetches the CSV every 30 s (new data only lands as often as
`csv_export_interval_seconds`, 60 s by default, so refreshing faster than
that wouldn't show anything new).

Gaps in the data (the daemon wasn't running, or every sensor failed to read
on a tick) are auto-detected from the timestamps themselves — no config
needed — and shown as a shaded "No data" region with the line broken across
it, rather than a misleading straight line interpolated across the outage.

## Reading the data outside the dashboard

```python
import pandas as pd
df = pd.read_csv("data/temperatures.csv", parse_dates=["timestamp"])
```

```r
df <- read.csv("data/temperatures.csv")
df$timestamp <- as.POSIXct(df$timestamp, format="%Y-%m-%dT%H:%M:%OSZ", tz="UTC")
```

Or query SQLite directly for large ranges instead of loading the whole CSV:

```python
import sqlite3
conn = sqlite3.connect("data/temperatures.db")
conn.execute("SELECT * FROM readings WHERE ts > ? ORDER BY ts", ("2026-08-05T00:00:00.000000Z",)).fetchall()
```

Excel opens `temperatures.csv` directly (standard comma-separated, UTF-8, one
header row).

## How to reset the database (start fresh)

This deletes all logged history — it's not reversible, so make sure you
don't want to keep it (e.g. `cp -r data/ data-backup/` first if unsure).

**Always stop the service before deleting.** `monitor.py` recreates
`data/` and initializes an empty DB/CSV/JSON automatically on startup, so
stop → delete → start gives a clean reset:

```bash
sudo systemctl stop temperature-monitor.service
rm -rf data/
sudo systemctl start temperature-monitor.service
```

Then confirm it came back up cleanly and is logging into the new, empty
files:

```bash
systemctl status temperature-monitor.service
ls data/
```

**Do not delete `data/` while the service keeps running**, without a
restart. The daemon's SQLite connection stays alive via its already-open
file handle even after the directory is removed, so DB writes can keep
silently going into an orphaned file, while every `latest.json`/CSV write
(which opens a fresh file handle each tick) starts failing — caught by the
daemon's error handling, so it won't crash, but it'll spam the journal with
errors and stop updating the CSV/JSON, and the DB is likely to end up
inconsistent. If this happens by accident, stop and restart the service
right after to get back to a clean state.

If you only want to clear the history but keep the current sensor set/config
untouched, deleting just `data/temperatures.db*` (and its `-wal`/`-shm`
files) plus `data/temperatures.csv` — leaving `latest.json` alone — has the
same effect; the service doesn't need any file to pre-exist.

## TODO after a remote install (SSH / RustDesk / VNC / ...)

If this was installed over a remote session, `tty_display` was likely left
disabled since the live console view can't be verified remotely (switching
virtual terminals only changes the display on the machine you're sitting
at, not the one you're remoted into). Once you have physical or KVM access
to the machine:

- [ ] Free tty3 from the login prompt: `sudo systemctl disable --now getty@tty3.service`.
- [ ] Set `tty_display.enabled: true` in `config.yaml`.
- [ ] Restart the service: `sudo systemctl restart temperature-monitor.service`.
- [ ] Switch to the console (`Ctrl+Alt+F3`) and confirm the live readout renders and updates correctly.
- [ ] If it looks wrong, or `journalctl -u temperature-monitor.service` shows tty-related warnings, set `tty_display.enabled` back to `false`.
- [ ] Decide whether tty3 is actually wanted at all — if not, switch the service to run as your own user instead of root (see the note at the end of [Installing as a systemd service](#installing-as-a-systemd-service)).
