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
    Washer and dryer are placed as a pair: the dryer starts after the washer ends, within 3 hours
    (wet laundry shouldn't wait)."""
    load: dict[datetime, float] = defaultdict(float)

    def slots(t, s):
        return [s + STEP * i for i in range(-(-t.minutes // 15))]

    def fits(t, s):
        return all(base_kw + load[x] + t.peak_kw <= limit_kw + 1e-9 for x in slots(t, s))

    def place(t, s, sign=1):
        for x in slots(t, s):
            load[x] += sign * t.peak_kw

    def options(t, a, b):
        out = [(c, s) for s in _starts(a, b, t.minutes) if (c := run_cost(hours, s, t.minutes, t.avg_kw)) is not None]
        return sorted(out, key=lambda x: (round(x[0], 6), x[1]))

    def best_fit(t, a, b):
        return next(((c, s) for c, s in options(t, a, b) if fits(t, s)), None)

    def choose(t, found):
        if found:
            place(t, found[1])
        chosen[t.id] = Choice(found[1], found[1] + timedelta(minutes=t.minutes), found[0]) if found else None

    by_id = {t.id: t for t in tasks}
    washer, dryer = by_id.get("washing_machine"), by_id.get("tumble_dryer")
    chosen: dict[str, Choice | None] = {}
    for t in sorted(tasks, key=lambda t: -t.avg_kw * t.minutes):
        if t.id in chosen:
            continue
        if washer and dryer and t in (washer, dryer):
            best = None
            for c1, s1 in options(washer, earliest, latest_end):
                if not fits(washer, s1):
                    continue
                place(washer, s1)
                e1 = s1 + timedelta(minutes=washer.minutes)
                d = best_fit(dryer, e1, min(latest_end, e1 + timedelta(hours=3, minutes=dryer.minutes)))
                place(washer, s1, -1)
                if d and (best is None or c1 + d[0] < best[0] - 1e-9):
                    best = (c1 + d[0], (c1, s1), d)
            if best:
                choose(washer, best[1])
                choose(dryer, best[2])
            else:
                choose(washer, best_fit(washer, earliest, latest_end))
                choose(dryer, None)
            continue
        choose(t, best_fit(t, earliest, latest_end))
    return [(t, chosen[t.id]) for t in tasks]


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
    return f"{round(x)} {big}" if x >= 10 else f"{x:.2f} {big}"


def say_rate(p: float, currency: str) -> str:
    small = _UNITS.get(currency, ("cents",))[0]
    return f"{p * 100:.0f} {small} per kilowatt-hour"


def say_clock(t: datetime) -> str:
    if (t.hour, t.minute) in ((0, 0), (12, 0)):
        return "midnight" if t.hour == 0 else "noon"
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
    if days == 0 or (days == 1 and t.hour == 0 and t.minute == 0):  # tonight's midnight
        return f"at {say_clock(t)}"
    if days == 1:
        return f"tomorrow at {say_clock(t)}"
    return f"on {t:%A} at {say_clock(t)}"
