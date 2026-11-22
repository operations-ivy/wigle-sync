"""Surveillance and tracking devices in a drive's .wiglecsv (#35).

The .wiglecsv has each Wi-Fi network's BSSID and SSID and each Bluetooth
device's address and name, but none of the advert bytes trackers are known
by (AirTags and the like need the .kismet database: a later step). So this
looks mostly at what a name or an address gives away; status-leds.py, which
asks Kismet itself, also passes a Bluetooth device's services and company ID:

- Flock Safety (wigle-sync#85; the signatures are the open-source detectors'
  flock-you (colonelpanichacks, simeononsecurity, bennjordan) and Ryan
  O'Horo's write-up, read 2026-11-22; nothing comes from Flock itself):
  - Flock's own IEEE MA-L B4:1E:52, in any address;
  - a camera's setup Wi-Fi, "Flock-" and its MAC's last 6 hex, or one named
    "Flock Camera net..." (newer cameras);
  - the battery packs' Bluetooth names: "FS Ext Battery", "Penguin-" and 10
    digits, or 10 bare digits with the packs' company ID 0x09C8 (Kismet only:
    the .wiglecsv has no company ID);
  - Flock's accessory service UUID (Kismet only).
  The cameras upload over LTE and keep Wi-Fi for setup, so most are silent:
  a zero is not proof there were none. Name substrings ("flock", "penguin",
  "raven") and the camera radios' Liteon OUIs match ordinary devices (seen
  in our own drives), so they count for nothing.
- Flock Raven gunshot detectors: two or more of the services 0x3100-0x3500
  (GPS, power, network, upload, error) in one advert (medium; Kismet only).
- ShotSpotter sensors: ShotSpotter's IEEE MA-L D4:11:D6 (high).
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
from collections.abc import Iterable
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
        Kind("flock", "Flock Safety device", "high"),
        Kind("raven", "Flock Raven gunshot detector", "medium"),
        Kind("shotspotter", "ShotSpotter sensor", "high"),
        Kind("axon", "Axon device (body camera?)", "high"),
        Kind("flipper", "Flipper Zero", "high"),
        Kind("skimmer", "Serial BT module (skimmer?)", "low"),
    )
}
FLOCK_OUI = "B4:1E:52"
FLOCK_SSID = re.compile(r"^Flock-[0-9A-Fa-f]{6}$|^Flock Camera net")
FLOCK_BATTERY = re.compile(r"^(FS Ext Battery|Penguin-[0-9]{10})$")
PENGUIN_SERIAL = re.compile(r"^[0-9]{10}$")  # newer Penguin firmware: with FLOCK_COMPANY only
FLOCK_COMPANY = "09c8"
FLOCK_SERVICE = "e8ccbb38-9532-46a8-9fe5-1814df172e6f"
RAVEN_SERVICES = {"3100", "3200", "3300", "3400", "3500"}
SHOTSPOTTER_OUI = "D4:11:D6"
AXON_OUI = "00:25:DF"
LED2_KINDS = frozenset({"flock", "raven", "shotspotter", "axon"})  # status-leds.py alerts for these
SKIMMER_NAMES = ("HC-05", "HC-06", "HC-08", "BT04-A", "RNBT-")


def globally_administered(mac: str) -> bool:
    """An address its maker assigned: the second-lowest bit of the first
    octet clear. Random and locally set addresses can start with anything."""
    try:
        return not int(mac[:2], 16) & 0x02
    except ValueError:
        return False


def kind_of(mac: str, name: str, kind: str, *, services: Iterable[str] = (), company: str | None = None) -> str | None:
    """The kind of one device, or None: a .wiglecsv row (Type WIFI, BT or BLE)
    or a Kismet record, which adds a Bluetooth device's service UUIDs (16-bit
    ones as 4 hex digits) and its company ID (4 hex digits)."""
    mac, name = mac.upper(), name or ""
    services = {s.lower() for s in services}
    if mac.startswith(AXON_OUI) and globally_administered(mac):
        return "axon"
    if mac.startswith(FLOCK_OUI):
        return "flock"
    if mac.startswith(SHOTSPOTTER_OUI):
        return "shotspotter"
    if kind == "WIFI":
        return "flock" if FLOCK_SSID.match(name) else None
    if FLOCK_BATTERY.match(name) or FLOCK_SERVICE in services:
        return "flock"
    if PENGUIN_SERIAL.match(name) and (company or "").lower() == FLOCK_COMPANY:
        return "flock"
    if len(services & RAVEN_SERVICES) >= 2:
        return "raven"
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
