"""Hourly electricity prices (per kWh) from a live or local source.

GRIDSHIFT_TARIFF:
  esios  - Spain PVPC, public REE/ESIOS endpoint, no token (default)
  tou    - any time-of-use plan, defined in GRIDSHIFT_TOU ("0-7:0.12,7-16:0.22,16-21:0.45,21-24:0.22")
  sample - deterministic Spain-like curve, for offline demos and tests
"""

import json
import math
import os
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

UTC = ZoneInfo("UTC")


@dataclass(frozen=True)
class Hour:
    start: datetime  # timezone-aware
    price: float  # currency per kWh

    @property
    def end(self) -> datetime:
        return self.start + timedelta(hours=1)


def _hours_from_local_midnight(d: date, tz: ZoneInfo, prices: list[float]) -> list[Hour]:
    # Local midnight -> UTC, then whole hours: correct on 23h/25h DST days.
    t0 = datetime(d.year, d.month, d.day, tzinfo=tz).astimezone(UTC)
    return [Hour((t0 + timedelta(hours=i)).astimezone(tz), p) for i, p in enumerate(prices)]


def _hours_in_day(d: date, tz: ZoneInfo) -> int:
    t0 = datetime(d.year, d.month, d.day, tzinfo=tz)
    t1 = t0 + timedelta(days=1)
    return int((t1.astimezone(UTC) - t0.astimezone(UTC)).total_seconds() // 3600)


class EsiosPublic:
    """Spain PVPC. Same public file and parsing as aiopvpc (Home Assistant's PVPC integration)."""

    URL = "https://api.esios.ree.es/archives/70/download_json?locale=es&date={d:%Y-%m-%d}"
    name = "Spain PVPC (REE e·sios)"
    currency = "EUR"

    def __init__(self, tz: ZoneInfo, zone: str = "PCB"):
        self.tz, self.zone, self._cache = tz, zone, {}

    def day(self, d: date) -> list[Hour] | None:
        if d in self._cache:
            return self._cache[d]
        req = urllib.request.Request(self.URL.format(d=d), headers={"User-Agent": "Mozilla/5.0 GridShift"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.load(r)
        rows = data.get("PVPC") or []
        if not rows:
            return None  # tomorrow is published around 20:15 Spanish time
        prices = [round(float(row[self.zone].replace(",", ".")) / 1000, 5) for row in rows]  # €/MWh -> €/kWh
        hours = _hours_from_local_midnight(d, self.tz, prices)
        self._cache[d] = hours
        return hours


class TimeOfUse:
    name = "Time-of-use plan"

    def __init__(self, tz: ZoneInfo, schedule: str, currency: str):
        self.tz, self.currency = tz, currency
        self.rate = [0.0] * 24
        for part in schedule.split(","):
            span, price = part.split(":")
            a, b = (int(x) for x in span.split("-"))
            for h in range(a, b):
                self.rate[h] = float(price)

    def day(self, d: date) -> list[Hour]:
        hours = _hours_from_local_midnight(d, self.tz, [0.0] * _hours_in_day(d, self.tz))
        return [Hour(h.start, self.rate[h.start.hour]) for h in hours]


class Sample:
    """Typical Spanish autumn day: cheap night and solar midday, evening peak."""

    name = "Sample prices (offline)"
    currency = "EUR"

    def __init__(self, tz: ZoneInfo):
        self.tz = tz

    def day(self, d: date) -> list[Hour]:
        hours = _hours_from_local_midnight(d, self.tz, [0.0] * _hours_in_day(d, self.tz))
        out = []
        for h in hours:
            x = h.start.hour
            p = 0.13
            p += 0.05 * math.exp(-((x - 9) ** 2) / 3)  # morning ramp
            p -= 0.06 * math.exp(-((x - 14.5) ** 2) / 5)  # solar dip
            p += 0.12 * math.exp(-((x - 20.5) ** 2) / 3)  # evening peak
            p -= 0.03 * math.exp(-((x - 4) ** 2) / 6)  # night valley
            p += 0.01 * math.sin(d.toordinal() + x)  # day-to-day variation
            out.append(Hour(h.start, round(p, 5)))
        return out


def make_provider():
    tz = ZoneInfo(os.getenv("GRIDSHIFT_TZ", "Europe/Madrid"))
    kind = os.getenv("GRIDSHIFT_TARIFF", "esios")
    if kind == "tou":
        schedule = os.getenv("GRIDSHIFT_TOU", "0-7:0.12,7-16:0.22,16-21:0.45,21-24:0.22")
        return TimeOfUse(tz, schedule, os.getenv("GRIDSHIFT_CURRENCY", "USD"))
    if kind == "sample":
        return Sample(tz)
    return EsiosPublic(tz)


class Prices:
    """Today + tomorrow (when published). Falls back to sample data, flagged in `source`."""

    def __init__(self, provider=None):
        self.provider = provider or make_provider()
        self.tz = self.provider.tz
        self.currency = self.provider.currency
        self._fallback = Sample(self.tz)

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def horizon(self, now: datetime) -> tuple[list[Hour], bool, str]:
        """Hours from the start of today until the end of the last published day."""
        d0, d1 = now.date(), now.date() + timedelta(days=1)
        try:
            today = self.provider.day(d0) or []
        except Exception:
            fb = self._fallback
            return fb.day(d0) + fb.day(d1), True, "sample prices (live source unavailable)"
        try:
            tomorrow = self.provider.day(d1)
        except Exception:
            tomorrow = None  # not published yet
        return today + (tomorrow or []), tomorrow is not None, self.provider.name
