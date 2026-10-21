"""Surveillance and tracking devices in a drive's .wiglecsv (#35).

The .wiglecsv has each Wi-Fi network's BSSID and SSID and each Bluetooth
device's address and name, but none of the advert bytes trackers are known
by (AirTags and the like need the .kismet database: a later step). So this
looks only for what a name or an address gives away:

- Flock Safety ALPR cameras: Wi-Fi SSIDs "Flock-" and a hex serial (medium:
  from public reporting on the cameras, not a published spec).
- Axon devices (body cameras, Signal sidearm sensors): Axon Enterprise's
  IEEE MA-L 00:25:DF in a globally administered address (high).
- Flipper Zero: Bluetooth name "Flipper <name>", its default (high).
- Bluetooth serial modules (HC-05/06/08 and the like), as found in card
  skimmers but also in hobby kit (low: logged, never alerted).

Which kinds to look for follows SquachWatch-CYD's list; no data is copied
from it. overwatch's home scanner (overwatch/watch.py) uses the same name
and OUI rules for the Bluetooth ones.

Privacy: scan() returns counts per kind, nothing else. Addresses are only
compared, and counted once per drive; names, places and times never leave
it."""

from __future__ import annotations

import csv
import io
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Kind:
    key: str
    label: str
    confidence: str  # high, medium, low: low ones are logged, never alerted


KINDS = {
    k.key: k
    for k in (
        Kind("flock", "Flock ALPR camera", "medium"),
        Kind("axon", "Axon device (body camera?)", "high"),
        Kind("flipper", "Flipper Zero", "high"),
        Kind("skimmer", "Serial BT module (skimmer?)", "low"),
    )
}
FLOCK_SSID = re.compile(r"^Flock-[0-9A-Fa-f]{4,}$")
AXON_OUI = "00:25:DF"
SKIMMER_NAMES = ("HC-05", "HC-06", "HC-08", "BT04-A", "RNBT-")


def globally_administered(mac: str) -> bool:
    """An address its maker assigned: the second-lowest bit of the first
    octet clear. Random and locally set addresses can start with anything."""
    try:
        return not int(mac[:2], 16) & 0x02
    except ValueError:
        return False


def kind_of(mac: str, name: str, kind: str) -> str | None:
    """The kind of one .wiglecsv row (Type WIFI, BT or BLE), or None."""
    mac, name = mac.upper(), name or ""
    if mac.startswith(AXON_OUI) and globally_administered(mac):
        return "axon"
    if kind == "WIFI":
        return "flock" if FLOCK_SSID.match(name) else None
    if name.startswith("Flipper "):
        return "flipper"
    if name.startswith(SKIMMER_NAMES):
        return "skimmer"
    return None


def scan_text(text: str) -> Counter:
    """Devices per kind in a .wiglecsv's text, each address counted once."""
    lines = text.splitlines()
    if len(lines) < 3:
        return Counter()
    seen: dict[str, set[str]] = {}
    for row in csv.DictReader(io.StringIO("\n".join(lines[1:]))):
        mac = (row.get("MAC") or "").strip()
        kind = kind_of(mac, (row.get("SSID") or "").strip(), (row.get("Type") or "WIFI").strip().upper())
        if kind:
            seen.setdefault(kind, set()).add(mac.upper())
    return Counter({k: len(v) for k, v in seen.items()})


def scan(path: Path) -> Counter:
    """Devices per kind in a .wiglecsv file; anything else counts nothing."""
    if path.suffix != ".wiglecsv":
        return Counter()
    return scan_text(path.read_text(errors="replace"))


def alerting(found: Counter) -> Counter:
    """Only the kinds worth an alert (high and medium confidence)."""
    return Counter({k: n for k, n in found.items() if KINDS[k].confidence != "low" and n})
