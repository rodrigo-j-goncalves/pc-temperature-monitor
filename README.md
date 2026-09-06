# Temperature Monitor

A Linux daemon that discovers every available temperature sensor (CPU, NVMe,
GPU, NICs, ...), samples them at a configurable interval (default: every 30
minutes), and stores the history in SQLite. A static Plotly dashboard
visualizes the data.

```
.
├── monitor.py                          # main daemon (sampling loop, storage)
├── sensors.py                          # sensor discovery and reading (hwmon, nvidia-smi)
├── config_JC.yaml, config_HC.yaml      # one config per machine -- sampling interval, data_dir
├── data_JC/, data_HC/                  # one data dir per machine (created at runtime)
│   ├── temperatures.db                 # primary store: tidy SQLite table
│   ├── temperatures.csv                # wide CSV export, pandas/R/Excel-readable (generated)
│   └── latest.json                     # current values, updated every tick (generated)
├── web/
│   ├── index.html                      # Plotly dashboard
│   ├── app.js
│   └── style.css
├── systemd/
│   └── temperature-monitor.service
├── README.md
└── LICENSE
```

## Quick start (setting this up on a new machine)

These are the steps to go from a fresh clone/copy of this repo to a running,
boot-persistent daemon with a dashboard. Each step is explained in more
detail further down.

1. Copy or clone this repo to wherever you want it to live, e.g.
   `~/temperature-monitor`.
2. Install the one non-stdlib dependency: `pip install pyyaml` (or your
   distro's `python3-yaml` package).
3. Pick (or create) this machine's config file, e.g. `config_HC.yaml` — see
   [Configuration](#configuration) and [Running this on more than one
   machine](#running-this-on-more-than-one-machine). The only thing that
   normally needs to differ between machines is `data_dir`.
4. Edit `systemd/temperature-monitor.service`: replace the absolute path in
   `ExecStart` with the actual path where you put this repo, and
   `config_<MACHINE>.yaml` with the config file from step 3 (see [Installing
   as a systemd service](#installing-as-a-systemd-service)).
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
   then open `http://localhost:8000/web/?data_dir=data_HC` in a browser
   (substitute this machine's actual `data_dir`).

That's it — sensors are discovered automatically, so nothing needs to be
told which sensors exist on the new machine.

## How it works

- **Sensor discovery** (`sensors.py`) walks `/sys/class/hwmon` directly (no
  dependency on the `sensors` CLI) and separately probes `nvidia-smi` for
  NVIDIA GPUs, which aren't exposed under hwmon. Discovery re-runs
  periodically so sensors that appear or disappear at runtime (a hot-plugged
  NVMe drive, a GPU driver reload, ...) are picked up without restarting the
  daemon. A failed read for one sensor never crashes the tick — it's simply
  omitted. This is also why `sensors.py` itself never needs per-machine
  changes: it adapts to whatever hardware it finds. The only thing that
  differs between machines is which config file (and `data_dir`) you point
  the daemon at — see [Running this on more than one
  machine](#running-this-on-more-than-one-machine).
- **Storage** (`monitor.py`) is a tidy/long SQLite table
  (`timestamp, sensor_id, value_c`), one row per sensor per tick. This is
  what makes "sensors appearing/disappearing" safe: there's no fixed set of
  columns to migrate.
- **CSV export** is a wide pivot of that table (one column per sensor),
  regenerated periodically. The header is the union of every sensor ever
  seen, so it only grows — a flaky sensor cycling on and off doesn't trigger
  repeated full rewrites.
- **`latest.json`** holds only the most recent reading per sensor, written
  atomically every tick, for the dashboard's current-value cards.
- Timestamps are UTC internally, e.g. `2026-08-05T17:43:12.123456Z` —
  sortable as plain text and parsed natively by pandas/R. The web dashboard
  converts to your browser's local time for display only; the stored files
  stay UTC.
- The SQLite database grows without bound — there's no automatic pruning of
  old rows. At the default 30-minute interval with ~13 sensors this is
  small for years; if you lower the interval a lot (see below), watch
  `data_<machine>/temperatures.db`'s size over time.

## Requirements

- Python 3.10+ (uses `sqlite3`, `csv`, `json`, `argparse` — all standard
  library).
- [PyYAML](https://pyyaml.org/) for the YAML config files (`pip install
  pyyaml`, or your distro's `python3-yaml` package). This is the one
  non-stdlib dependency in the whole project.
- `nvidia-smi` on `PATH` if you have an NVIDIA GPU and want its temperature
  logged (optional — its absence is handled gracefully).

## Configuration

Each machine has its own config file (`config_JC.yaml`, `config_HC.yaml`,
...) rather than one shared `config.yaml` — see [Running this on more than
one machine](#running-this-on-more-than-one-machine) for why. Edit the one
for the machine you're on:

| Key | Meaning |
|---|---|
| `sample_interval_seconds` | How often sensors are read and written to SQLite. |
| `sensor_rescan_interval_seconds` | How often hwmon/nvidia-smi are re-probed for new/removed sensors. |
| `data_dir` | Directory (relative to the config file, or absolute) holding the DB, CSV, and JSON — set differently per machine, e.g. `data_JC`, `data_HC`. |
| `sqlite_filename`, `csv_filename`, `latest_json_filename` | Output filenames inside `data_dir`. |
| `csv_export_interval_seconds` | How often new SQLite rows are appended to the CSV. |
| `log_level` | Python logging level (`INFO`, `DEBUG`, ...). |

### Changing the sampling interval

1. Edit `sample_interval_seconds` in this machine's config file. It's in
   seconds, e.g.:
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
python3 monitor.py --config config_HC.yaml   # or whichever config is this machine's
```

Stop with Ctrl-C (or `SIGTERM`) — the daemon exports any pending CSV rows
before exiting.

## Installing as a systemd service

The unit file in `systemd/temperature-monitor.service` has this project's
absolute path, and a `config_<MACHINE>.yaml` placeholder, baked into
`ExecStart` (systemd unit files don't expand `~` or relative paths, and
can't take an argument at install time). **Before installing on a new
machine or from a fresh clone, edit `ExecStart`** to match where you put the
repo and which config file is this machine's, e.g.:

```bash
sed -i \
  -e 's|/path/to/temperature-monitor-logger|/actual/path/to/temperature-monitor-logger|g' \
  -e 's|config_<MACHINE>.yaml|config_HC.yaml|' \
  systemd/temperature-monitor.service
```

(or just open the file and edit the `ExecStart` line by hand).

It runs as `root` (needed on some systems for certain hwmon nodes),
restarts automatically on crash, and sends its logs to the journal.

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

You can instead run the service as an unprivileged user (remove `User=root`
from the unit file, or set it to your username) as long as that user can
read the relevant `/sys/class/hwmon/**` files, which is normally
world-readable. This also avoids `data_<machine>/temperatures.db` ending up
root-owned, which otherwise means any write-class DB operation (manual
`VACUUM`, checkpoint, editing rows) needs `sudo` — plain reads work fine
either way.

## Running this on more than one machine

Each machine gets its own independent clone of this repo, its own config
file (`config_JC.yaml`, `config_HC.yaml`, ...) with its own `data_dir`
(`data_JC`, `data_HC`, ...), and its own systemd unit installed with that
machine's actual path and config filename substituted in (see [Installing as
a systemd service](#installing-as-a-systemd-service) above).

**Why per-machine config files instead of one shared `config.yaml`:** if you
sync/rsync this project folder between machines (e.g. via a NAS), a single
`config.yaml` with `data_dir: data` would drag whichever machine's
`data_dir` setting happens to be in the synced copy along with it — which is
exactly how one machine ended up pointed at another's data in practice. Two
separate config files (each with its own `data_dir`) can be synced back and
forth freely without ever fighting over which machine's data a shared
`data`/ directory belongs to. Everything else in the config is normally
identical between machines and only needs to change if you actually want
different behavior (e.g. a different sampling interval) on that machine.

To roll out a fix or feature to every machine: commit and push once from
wherever you're working, then on each other machine `git pull` and restart
the service. The dashboard needs the right `data_dir` query parameter per
machine too — see [Web dashboard](#web-dashboard).

## Web dashboard

The dashboard is static (Plotly loaded from a CDN) and only needs a file
server — it reads `<data_dir>/temperatures.csv` and `<data_dir>/latest.json`
via `fetch()`, which browsers block on `file://` URLs, so it must be served
over HTTP.

**Quickest way:** run `./plot_now.sh` from the repo root. It starts the
server if it isn't already running and opens the dashboard in your default
browser, auto-detecting whether this machine has `data_HC/` or `data_JC/`.

Or do it by hand, from the repo root:

```bash
python3 -m http.server 8000
```

Then open `http://<host>:8000/web/?data_dir=data_HC` in a browser,
substituting the `data_dir` this machine's config file actually uses
(defaults to plain `data` if the parameter is omitted). This is independent
of the daemon, which keeps logging regardless — the dashboard is an
ordinary webpage.

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

(paths below assume `data_dir` is `data_HC` — substitute this machine's
actual `data_dir`)

```python
import pandas as pd
df = pd.read_csv("data_HC/temperatures.csv", parse_dates=["timestamp"])
```

```r
df <- read.csv("data_HC/temperatures.csv")
df$timestamp <- as.POSIXct(df$timestamp, format="%Y-%m-%dT%H:%M:%OSZ", tz="UTC")
```

Or query SQLite directly for large ranges instead of loading the whole CSV:

```python
import sqlite3
conn = sqlite3.connect("data_HC/temperatures.db")
conn.execute("SELECT * FROM readings WHERE ts > ? ORDER BY ts", ("2026-08-05T00:00:00.000000Z",)).fetchall()
```

Excel opens `temperatures.csv` directly (standard comma-separated, UTF-8, one
header row).

## How to reset the database (start fresh)

This deletes all logged history — it's not reversible, so make sure you
don't want to keep it (e.g. `cp -r data_HC/ data_HC-backup/` first if
unsure). Substitute this machine's actual `data_dir` for `data_HC` below.

**Always stop the service before deleting.** `monitor.py` recreates
`data_dir` and initializes an empty DB/CSV/JSON automatically on startup, so
stop → delete → start gives a clean reset:

```bash
sudo systemctl stop temperature-monitor.service
rm -rf data_HC/
sudo systemctl start temperature-monitor.service
```

Then confirm it came back up cleanly and is logging into the new, empty
files:

```bash
systemctl status temperature-monitor.service
ls data_HC/
```

**Do not delete `data_dir` while the service keeps running**, without a
restart. The daemon's SQLite connection stays alive via its already-open
file handle even after the directory is removed, so DB writes can keep
silently going into an orphaned file, while every `latest.json`/CSV write
(which opens a fresh file handle each tick) starts failing — caught by the
daemon's error handling, so it won't crash, but it'll spam the journal with
errors and stop updating the CSV/JSON, and the DB is likely to end up
inconsistent. If this happens by accident, stop and restart the service
right after to get back to a clean state.

If you only want to clear the history but keep the current sensor set/config
untouched, deleting just `data_HC/temperatures.db*` (and its `-wal`/`-shm`
files) plus `data_HC/temperatures.csv` — leaving `latest.json` alone — has
the same effect; the service doesn't need any file to pre-exist.

## License and credits

- MIT — see [LICENSE](LICENSE). Author: Rodrigo J. Gonçalves
- I _claudeveloped_ this (Claude Sonnet 5, Anthropic)