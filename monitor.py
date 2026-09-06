"""Temperature monitoring daemon.

Reads sensors (see sensors.py) once per configured interval, stores every
reading in a tidy/long SQLite table, mirrors the current values into a small
JSON file for the dashboard/live display, and periodically exports the
accumulated data into a wide-format CSV that pandas/R/Excel can open directly.
"""

from __future__ import annotations

import argparse
import csv
import logging
import signal
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

import sensors

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Config / logging
# --------------------------------------------------------------------------

def load_config(path: Path) -> dict:
    with path.open() as f:
        return yaml.safe_load(f)


def setup_logging(level_name: str) -> None:
    level = getattr(logging, str(level_name).upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _now_iso() -> str:
    """Current UTC time as a fixed-width, lexicographically sortable string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


# --------------------------------------------------------------------------
# SQLite storage (tidy/long format: one row per sensor per tick)
# --------------------------------------------------------------------------

def init_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS readings (
            ts TEXT NOT NULL,
            sensor_id TEXT NOT NULL,
            value_c REAL NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_readings_ts ON readings(ts)")
    conn.commit()
    return conn


def insert_readings(conn: sqlite3.Connection, ts: str, readings: dict[str, float]) -> None:
    if not readings:
        return
    conn.executemany(
        "INSERT INTO readings (ts, sensor_id, value_c) VALUES (?, ?, ?)",
        [(ts, sensor_id, value) for sensor_id, value in readings.items()],
    )
    conn.commit()


# --------------------------------------------------------------------------
# latest.json (atomic write so readers never see a half-written file)
# --------------------------------------------------------------------------

def write_latest_json(path: Path, ts: str, readings: dict[str, float], sensor_labels: dict[str, str]) -> None:
    import json

    payload = {
        "timestamp": ts,
        "sensors": {
            sensor_id: {"label": sensor_labels.get(sensor_id, sensor_id), "value_c": value}
            for sensor_id, value in readings.items()
        },
    }
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2))
    tmp_path.replace(path)


# --------------------------------------------------------------------------
# CSV export: wide pivot of the tidy SQLite table.
#
# The header is the sorted set of every sensor_id ever seen in the DB, so it
# only ever grows (a sensor going offline doesn't shrink it, which avoids
# repeated rewrites if a flaky sensor cycles in and out). New rows are
# appended in the common case; the file is fully rewritten only when the
# column set itself changes (a brand new sensor appears).
# --------------------------------------------------------------------------

def _tail_line(path: Path) -> str | None:
    """Return the last non-empty line of a text file, or None if empty/missing."""
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size == 0:
        return None
    chunk = 4096
    data = b""
    with path.open("rb") as f:
        pos = size
        while pos > 0:
            read_size = min(chunk, pos)
            pos -= read_size
            f.seek(pos)
            data = f.read(read_size) + data
            if data.count(b"\n") >= 2 or pos == 0:
                break
    lines = data.splitlines()
    return lines[-1].decode("utf-8") if lines else None


def _read_csv_header(path: Path) -> list[str] | None:
    if not path.exists():
        return None
    with path.open(newline="") as f:
        try:
            return next(csv.reader(f))
        except StopIteration:
            return None


def _read_last_exported_ts(path: Path) -> str | None:
    line = _tail_line(path)
    if not line:
        return None
    try:
        row = next(csv.reader([line]))
    except StopIteration:
        return None
    if not row or row[0] == "timestamp":  # tail hit the header, no data rows yet
        return None
    return row[0]


def _pivot_rows(cursor: sqlite3.Cursor):
    """Yield (ts, {sensor_id: value}) for consecutive DB rows sharing one ts.

    Assumes the cursor is ordered by ts.
    """
    current_ts = None
    current_values: dict[str, float] = {}
    for ts, sensor_id, value in cursor:
        if ts != current_ts:
            if current_ts is not None:
                yield current_ts, current_values
            current_ts = ts
            current_values = {}
        current_values[sensor_id] = value
    if current_ts is not None:
        yield current_ts, current_values


def _rewrite_csv_full(conn: sqlite3.Connection, csv_path: Path, sensor_ids: list[str]) -> str | None:
    tmp_path = csv_path.with_suffix(csv_path.suffix + ".tmp")
    cur = conn.execute("SELECT ts, sensor_id, value_c FROM readings ORDER BY ts, sensor_id")
    last_ts = None
    with tmp_path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", *sensor_ids])
        for ts, values in _pivot_rows(cur):
            writer.writerow([ts, *(values.get(sid, "") for sid in sensor_ids)])
            last_ts = ts
    tmp_path.replace(csv_path)
    return last_ts


def _append_csv_rows(
    conn: sqlite3.Connection, csv_path: Path, sensor_ids: list[str], since_ts: str | None
) -> str | None:
    if since_ts:
        cur = conn.execute(
            "SELECT ts, sensor_id, value_c FROM readings WHERE ts > ? ORDER BY ts, sensor_id",
            (since_ts,),
        )
    else:
        cur = conn.execute("SELECT ts, sensor_id, value_c FROM readings ORDER BY ts, sensor_id")
    last_ts = since_ts
    with csv_path.open("a", newline="") as f:
        writer = csv.writer(f)
        for ts, values in _pivot_rows(cur):
            writer.writerow([ts, *(values.get(sid, "") for sid in sensor_ids)])
            last_ts = ts
    return last_ts


def export_csv(conn: sqlite3.Connection, csv_path: Path, last_exported_ts: str | None) -> str | None:
    all_sensor_ids = [
        row[0] for row in conn.execute("SELECT DISTINCT sensor_id FROM readings ORDER BY sensor_id")
    ]
    if not all_sensor_ids:
        return last_exported_ts

    header = ["timestamp", *all_sensor_ids]
    if _read_csv_header(csv_path) != header:
        logger.info("Sensor set changed; rewriting %s", csv_path)
        new_ts = _rewrite_csv_full(conn, csv_path, all_sensor_ids)
    else:
        new_ts = _append_csv_rows(conn, csv_path, all_sensor_ids, since_ts=last_exported_ts)
    return new_ts if new_ts is not None else last_exported_ts


# --------------------------------------------------------------------------
# Live tty3 display
# --------------------------------------------------------------------------

class TtyDisplay:
    def __init__(self, device_path: str):
        self.device_path = device_path
        self._fh = None
        self._disabled = False

    def _ensure_open(self) -> None:
        if self._fh is not None or self._disabled:
            return
        try:
            self._fh = open(self.device_path, "w")
        except OSError as exc:
            logger.warning("Cannot open %s for live display, disabling it: %s", self.device_path, exc)
            self._disabled = True

    def render(self, ts: str, readings: dict[str, float], sensor_labels: dict[str, str]) -> None:
        if self._disabled:
            return
        self._ensure_open()
        if self._fh is None:
            return
        lines = [f"Temperature Monitor -- {ts}", "=" * 60]
        for sensor_id in sorted(readings):
            label = sensor_labels.get(sensor_id, sensor_id)
            lines.append(f"{label:<40} {readings[sensor_id]:6.1f} C")
        if not readings:
            lines.append("(no sensor readings this tick)")
        text = "\033[H\033[J" + "\n".join(lines) + "\n"
        try:
            self._fh.write(text)
            self._fh.flush()
        except OSError as exc:
            logger.warning("Lost access to %s, disabling live display: %s", self.device_path, exc)
            self._disabled = True
            self._fh = None

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Temperature monitoring daemon")
    parser.add_argument(
        "--config", type=Path, default=Path(__file__).resolve().parent / "config.yaml"
    )
    args = parser.parse_args()

    config = load_config(args.config)
    setup_logging(config.get("log_level", "INFO"))

    project_root = args.config.resolve().parent
    data_dir = Path(config["data_dir"])
    if not data_dir.is_absolute():
        data_dir = project_root / data_dir
    data_dir.mkdir(parents=True, exist_ok=True)

    db_path = data_dir / config["sqlite_filename"]
    csv_path = data_dir / config["csv_filename"]
    latest_json_path = data_dir / config["latest_json_filename"]

    sample_interval = float(config["sample_interval_seconds"])
    rescan_interval = float(config["sensor_rescan_interval_seconds"])
    csv_export_interval = float(config["csv_export_interval_seconds"])

    tty_cfg = config.get("tty_display", {}) or {}
    tty_display = TtyDisplay(tty_cfg["device"]) if tty_cfg.get("enabled") else None
    tty_refresh_interval = float(tty_cfg.get("refresh_interval_seconds", sample_interval))

    conn = init_db(db_path)

    stop_event = threading.Event()

    def _handle_stop(signum, frame) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    logger.info("Discovering sensors...")
    sensor_list = sensors.discover_sensors()
    sensor_labels = {s.sensor_id: s.label for s in sensor_list}
    logger.info("Found %d sensors: %s", len(sensor_list), ", ".join(sensor_labels) or "(none)")

    last_rescan = time.monotonic()
    last_csv_export = time.monotonic()
    last_tty_render = 0.0
    last_exported_ts = _read_last_exported_ts(csv_path)

    next_tick = time.monotonic()
    try:
        while not stop_event.is_set():
            now_mono = time.monotonic()
            try:
                ts = _now_iso()
                readings = sensors.read_sensors(sensor_list)
                if not readings:
                    logger.warning(
                        "No sensor readings collected this tick (%d sensors tracked)",
                        len(sensor_list),
                    )
                insert_readings(conn, ts, readings)
                write_latest_json(latest_json_path, ts, readings, sensor_labels)

                if tty_display and (now_mono - last_tty_render) >= tty_refresh_interval:
                    tty_display.render(ts, readings, sensor_labels)
                    last_tty_render = now_mono

                if (now_mono - last_rescan) >= rescan_interval:
                    sensor_list = sensors.discover_sensors()
                    sensor_labels = {s.sensor_id: s.label for s in sensor_list}
                    last_rescan = now_mono

                if (now_mono - last_csv_export) >= csv_export_interval:
                    last_exported_ts = export_csv(conn, csv_path, last_exported_ts)
                    last_csv_export = now_mono
            except Exception:
                logger.exception("Unexpected error in monitor loop tick; continuing")

            next_tick += sample_interval
            sleep_time = next_tick - time.monotonic()
            if sleep_time > 0:
                stop_event.wait(sleep_time)  # wakes immediately on SIGTERM/SIGINT
            else:
                next_tick = time.monotonic()  # fell behind; resync instead of busy-looping
    finally:
        logger.info("Shutting down, exporting final CSV batch...")
        try:
            export_csv(conn, csv_path, last_exported_ts)
        except Exception:
            logger.exception("Final CSV export failed")
        if tty_display:
            tty_display.close()
        conn.close()


if __name__ == "__main__":
    main()
