"""Temperature sensor discovery and reading.

Sources supported:
  - hwmon (sysfs): CPU (k10temp/coretemp), NVMe, GPU (amdgpu), NICs, etc.
  - nvidia-smi: NVIDIA GPUs, which are not exposed under hwmon.

Sensors can appear or disappear at runtime (unplugged NVMe, driver reload,
etc.). Discovery is meant to be re-run periodically by the caller; reading
never raises for a single failed sensor.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

HWMON_ROOT = Path("/sys/class/hwmon")
PCI_BDF_RE = re.compile(r"[0-9a-f]{4}:([0-9a-f]{2}):([0-9a-f]{2})\.[0-9a-f]")
NVIDIA_SMI_TIMEOUT_S = 5


@dataclass(frozen=True)
class Sensor:
    sensor_id: str  # stable identifier, e.g. "k10temp-pci-00c3/Tccd1"
    label: str  # human-readable label, e.g. "Tccd1"
    source: str  # "hwmon" or "nvidia_smi"
    path: Path | None = None  # sysfs *_input path, for hwmon sensors
    gpu_index: int | None = None  # GPU index, for nvidia_smi sensors


def _sanitize(text: str) -> str:
    return re.sub(r"\s+", "_", text.strip())


def _chip_suffix(hwmon_dir: Path) -> str:
    """Build a unique suffix for a chip from its underlying device path.

    Several chips can share the same driver name (e.g. two NVMe drives are
    both called "nvme"), so the bare name is not a safe identifier. We mimic
    lm-sensors' own "chip-pci-bbdd" convention using the PCI bus/device
    numbers of the closest PCI ancestor, falling back to the hwmon index.
    """
    try:
        device_path = str((hwmon_dir / "device").resolve())
    except OSError:
        return hwmon_dir.name
    matches = list(PCI_BDF_RE.finditer(device_path))
    if not matches:
        return hwmon_dir.name
    bus, dev = matches[-1].group(1), matches[-1].group(2)
    return f"pci-{bus}{dev}"


def discover_hwmon_sensors() -> list[Sensor]:
    sensors: list[Sensor] = []
    if not HWMON_ROOT.is_dir():
        return sensors
    for hwmon_dir in sorted(HWMON_ROOT.glob("hwmon*")):
        try:
            chip_name = (hwmon_dir / "name").read_text().strip()
        except OSError:
            continue
        chip_id = f"{_sanitize(chip_name)}-{_chip_suffix(hwmon_dir)}"
        for input_path in sorted(hwmon_dir.glob("temp*_input")):
            label_path = input_path.with_name(
                input_path.name.replace("_input", "_label")
            )
            try:
                label = label_path.read_text().strip()
            except OSError:
                label = input_path.name.replace("_input", "")
            sensor_id = f"{chip_id}/{_sanitize(label)}"
            sensors.append(
                Sensor(sensor_id=sensor_id, label=label, source="hwmon", path=input_path)
            )
    return sensors


def discover_nvidia_sensors() -> list[Sensor]:
    sensors: list[Sensor] = []
    if shutil.which("nvidia-smi") is None:
        return sensors
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,name", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=NVIDIA_SMI_TIMEOUT_S,
            check=True,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("nvidia-smi discovery failed: %s", exc)
        return sensors
    for line in result.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2:
            continue
        try:
            index = int(parts[0])
        except ValueError:
            continue
        name = parts[1]
        sensors.append(
            Sensor(
                sensor_id=f"nvidia_smi-gpu{index}/temperature",
                label=f"{name} (GPU {index})",
                source="nvidia_smi",
                gpu_index=index,
            )
        )
    return sensors


def discover_sensors() -> list[Sensor]:
    """Discover all currently available temperature sensors."""
    sensors = discover_hwmon_sensors()
    sensors.extend(discover_nvidia_sensors())
    if not sensors:
        logger.warning("No temperature sensors discovered.")
    return sensors


def _read_hwmon(sensor: Sensor) -> float | None:
    try:
        millidegrees = int(sensor.path.read_text().strip())
    except (OSError, ValueError) as exc:
        logger.warning("Failed to read %s: %s", sensor.sensor_id, exc)
        return None
    return millidegrees / 1000.0


def _read_nvidia(sensors: list[Sensor]) -> dict[str, float]:
    """Read all nvidia-smi sensors in a single subprocess call."""
    readings: dict[str, float] = {}
    if not sensors:
        return readings
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=NVIDIA_SMI_TIMEOUT_S,
            check=True,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("nvidia-smi read failed: %s", exc)
        return readings
    by_index = {s.gpu_index: s.sensor_id for s in sensors}
    for line in result.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2:
            continue
        try:
            index, temp_c = int(parts[0]), float(parts[1])
        except ValueError:
            continue
        sensor_id = by_index.get(index)
        if sensor_id is not None:
            readings[sensor_id] = temp_c
    return readings


def read_sensors(sensors: list[Sensor]) -> dict[str, float]:
    """Read current values for the given sensors.

    A sensor that fails to read (removed, driver error, ...) is simply
    omitted from the result rather than raising.
    """
    readings: dict[str, float] = {}
    nvidia_sensors = [s for s in sensors if s.source == "nvidia_smi"]
    readings.update(_read_nvidia(nvidia_sensors))
    for sensor in sensors:
        if sensor.source != "hwmon":
            continue
        value = _read_hwmon(sensor)
        if value is not None:
            readings[sensor.sensor_id] = value
    return readings
