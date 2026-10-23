"""Cost math, scheduling and voice phrasing. Pure functions over a list of Hour."""

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

from .prices import Hour

STEP = timedelta(minutes=15)


# ---------- time parsing ----------

_CLOCK = re.compile(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$")


def parse_time(text: str, now: datetime) -> tuple[datetime, bool] | None:
    """'' -> None. Accepts '07:00', '7am', '7:30 pm', 'noon', 'midnight', 'tomorrow 6am', ISO.
    Returns (datetime, explicit_day)."""
    s = text.strip().lower()
    if not s or s == "now":
        return None
    try:
        dt = datetime.fromisoformat(text.strip())
        return (dt if dt.tzinfo else dt.replace(tzinfo=now.tzinfo)), True
    except ValueError:
        pass
    day = now.date()
    explicit = s.startswith("tomorrow")
    if explicit:
        s = s.removeprefix("tomorrow").replace("at", "").strip()
        day += timedelta(days=1)
    s = {"noon": "12:00", "midday": "12:00", "midnight": "0:00"}.get(s, s)
    m = _CLOCK.match(s.replace(".", ""))
    if not m:
        raise ValueError(f"Could not understand time '{text}'. Use 24h 'HH:MM', e.g. '07:00'.")
    h, mi, ampm = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ampm == "pm" and h < 12:
        h += 12
    if ampm == "am" and h == 12:
        h = 0
    if h > 23 or mi > 59:
        raise ValueError(f"Invalid time '{text}'.")
    return datetime(day.year, day.month, day.day, h, mi, tzinfo=now.tzinfo), explicit


def resolve_window(earliest: str, deadline: str, now: datetime, hours: list[Hour]) -> tuple[datetime, datetime]:
    """Clock times roll forward to their next occurrence. Default deadline: 24 h or the end of known prices."""
    horizon_end = hours[-1].end if hours else now
    p = parse_time(earliest, now)
    start = now if p is None else p[0]
    if p and not p[1] and start < now - timedelta(minutes=1):
        start += timedelta(days=1)
    start = max(start, now)
    p = parse_time(deadline, now)
    if p is None:
        end = start + timedelta(hours=24)
    else:
        end = p[0]
        while not p[1] and end <= start:
            end += timedelta(days=1)
    return start, min(end, horizon_end)


# ---------- cost math ----------

def run_cost(hours: list[Hour], start: datetime, minutes: int, kw: float) -> float | None:
    end = start + timedelta(minutes=minutes)
    if not hours or start < hours[0].start or end > hours[-1].end:
        return None
    total = 0.0
    for h in hours:
        a, b = max(start, h.start), min(end, h.end)
        if b > a:
            total += (b - a).total_seconds() / 3600 * kw * h.price
    return total


def _ceil_step(t: datetime) -> datetime:
    if t.second or t.microsecond:
        t = t.replace(second=0, microsecond=0) + timedelta(minutes=1)
    return t + timedelta(minutes=(15 - t.minute % 15) % 15)


def _starts(earliest: datetime, latest_end: datetime, minutes: int):
    s, dur = _ceil_step(earliest), timedelta(minutes=minutes)
    while s + dur <= latest_end:
        yield s
        s += STEP


@dataclass
class Choice:
    start: datetime
    end: datetime
    cost: float


def best_start(hours, minutes, kw, earliest, latest_end) -> Choice | None:
    best = None
    for s in _starts(earliest, latest_end, minutes):
        c = run_cost(hours, s, minutes, kw)
        if c is not None and (best is None or c < best.cost - 1e-9):
            best = Choice(s, s + timedelta(minutes=minutes), c)
    return best


@dataclass
class Task:
    id: str
    label: str
    minutes: int
    avg_kw: float
    peak_kw: float


def plan(hours, tasks: list[Task], earliest, latest_end, limit_kw: float, base_kw: float):
    """Greedy: biggest loads first, each at its cheapest start that keeps total draw under limit_kw.
    The dryer starts after the washing machine finishes, within 3 hours (wet laundry shouldn't wait)."""
    order = sorted(tasks, key=lambda t: -t.avg_kw * t.minutes)
    ids = [t.id for t in order]
    if "washing_machine" in ids and "tumble_dryer" in ids:
        dryer = order.pop(ids.index("tumble_dryer"))
        order.insert([t.id for t in order].index("washing_machine") + 1, dryer)

    load: dict[datetime, float] = defaultdict(float)
    chosen: dict[str, Choice | None] = {}
    for t in order:
        start_from, end_by = earliest, latest_end
        if t.id == "tumble_dryer" and chosen.get("washing_machine"):
            start_from = max(earliest, chosen["washing_machine"].end)
            end_by = min(latest_end, start_from + timedelta(hours=3, minutes=t.minutes))
        options = []
        for s in _starts(start_from, end_by, t.minutes):
            c = run_cost(hours, s, t.minutes, t.avg_kw)
            if c is not None:
                options.append((c, s))
        chosen[t.id] = None
        for c, s in sorted(options, key=lambda x: (round(x[0], 6), x[1])):
            slots = [s + STEP * i for i in range(-(-t.minutes // 15))]
            if all(base_kw + load[x] + t.peak_kw <= limit_kw + 1e-9 for x in slots):
                for x in slots:
                    load[x] += t.peak_kw
                chosen[t.id] = Choice(s, s + timedelta(minutes=t.minutes), c)
                break
    return [(t, chosen[t.id]) for t in order]


# ---------- price levels ----------

def level(price: float, prices: list[float]) -> str:
    rank = sum(p < price for p in prices) / max(len(prices), 1)
    return "cheap" if rank <= 1 / 3 else "expensive" if rank >= 2 / 3 else "average"


def cheapest_block(hours: list[Hour], length: int) -> tuple[Hour, Hour, float] | None:
    best = None
    for i in range(len(hours) - length + 1):
        block = hours[i : i + length]
        avg = sum(h.price for h in block) / length
        if best is None or avg < best[2]:
            best = (block[0], block[-1], avg)
    return best


# ---------- voice phrasing ----------

_UNITS = {"EUR": ("cents", "euros"), "USD": ("cents", "dollars"), "GBP": ("pence", "pounds")}


def say_money(x: float, currency: str) -> str:
    small, big = _UNITS.get(currency, ("cents", currency))
    if x < 0.995:
        c = round(x * 100)
        return f"less than 1 {small[:-1] if small.endswith('s') else small}" if c < 1 else f"{c} {small}"
    return f"{x:.2f} {big}"


def say_rate(p: float, currency: str) -> str:
    small = _UNITS.get(currency, ("cents",))[0]
    return f"{p * 100:.0f} {small} per kilowatt-hour"


def say_clock(t: datetime) -> str:
    h = t.hour % 12 or 12
    suffix = "AM" if t.hour < 12 else "PM"
    return f"{h} {suffix}" if t.minute == 0 else f"{h}:{t.minute:02d} {suffix}"


def say_span(a: datetime, b: datetime, now: datetime) -> str:
    if a <= now:
        return f"from now until {say_clock(b)}"
    day = "tomorrow " if (a.date() - now.date()).days == 1 else ""
    return f"{day}from {say_clock(a)} to {say_clock(b)}"


def say_when(t: datetime, now: datetime) -> str:
    days = (t.date() - now.date()).days
    if days == 0:
        return f"at {say_clock(t)}"
    if days == 1:
        return f"tomorrow at {say_clock(t)}"
    return f"on {t:%A} at {say_clock(t)}"
