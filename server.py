"""GridShift — MCP server for Alexa+ (Streamable HTTP).

"Alexa, when should I run the dishwasher?" -> GridShift finds the cheapest window in today's
and tomorrow's hourly electricity prices, keeps the house under its contracted power limit,
and answers with a ready-to-speak sentence plus structured data.

Run:  python server.py        ->  http://localhost:8000/mcp
"""

import json
import os
from datetime import timedelta
from typing import Literal

from pydantic import BaseModel, Field

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from gridshift.appliances import APPLIANCES, Appliance, catalog
from gridshift.planner import (
    Task, best_start, cheapest_block, level, plan, resolve_window, run_cost,
    say_clock, say_money, say_rate, say_span, say_when,
)
from gridshift.prices import Prices

POWER_LIMIT_KW = float(os.getenv("GRIDSHIFT_POWER_LIMIT_KW", "4.6"))  # most common contract in Spain
BASE_LOAD_KW = float(os.getenv("GRIDSHIFT_BASE_LOAD_KW", "0.4"))  # fridge, router, lights

ApplianceId = Literal[
    "dishwasher", "washing_machine", "tumble_dryer", "ev_charger",
    "oven", "water_heater", "pool_pump", "heat_pump", "custom",
]

prices = Prices()

mcp = MCPServer(
    name="gridshift",
    title="GridShift",
    version="1.0.0",
    description="Tells a household when electricity is cheapest and plans appliance runs around it.",
    instructions=(
        "GridShift answers questions about electricity prices and the best time to run home appliances. "
        "Every tool returns a `speech` field: a short answer written to be spoken aloud. Prefer saying it "
        "as-is. Times are in the household's local time zone. Never invent prices; call a tool."
    ),
)

READ_ONLY = dict(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True)


# ---------- output schemas ----------

class PricePoint(BaseModel):
    start: str
    price: float


class Outlook(BaseModel):
    speech: str
    next_24h: list[PricePoint] = Field(description="Hourly prices from the current hour, for devices with a screen")
    now_local: str
    price_now: float = Field(description="Current price per kWh")
    level_now: Literal["cheap", "average", "expensive"]
    cheapest_window_start: str | None
    cheapest_window_end: str | None
    cheapest_window_avg_price: float | None
    priciest_hour_today: str
    tomorrow_published: bool
    currency: str
    price_source: str


class BestTime(BaseModel):
    speech: str
    appliance: str
    energy_kwh: float
    best_start: str | None
    best_end: str | None
    cost_best: float | None
    cost_if_started_asap: float | None
    savings_percent: float | None
    currency: str
    price_source: str


class PlannedRun(BaseModel):
    appliance: str
    start: str | None
    end: str | None
    cost: float | None


class DayPlan(BaseModel):
    speech: str
    runs: list[PlannedRun]
    total_cost: float
    cost_if_all_started_asap: float | None
    monthly_savings_estimate: float | None = Field(description="If this plan is repeated daily for 30 days")
    asap_would_exceed_power_limit: bool
    power_limit_kw: float
    currency: str
    price_source: str


# ---------- tools ----------

@mcp.tool(annotations=ToolAnnotations(title="Electricity price outlook", **READ_ONLY))
def price_outlook() -> Outlook:
    """Is electricity cheap or expensive right now, and when is the cheapest time in the next 24 hours?
    Use for: 'is power expensive now?', 'when is electricity cheapest today?', 'energy briefing'."""
    now = prices.now()
    hours, has_tomorrow, source = prices.horizon(now)
    today = [h for h in hours if h.start.date() == now.date()]
    current = next(h for h in hours if h.start <= now < h.end)
    ahead = [h for h in hours if h.end > now and h.start < now + timedelta(hours=24)]
    block = cheapest_block(ahead, 3)
    priciest = max(today, key=lambda h: h.price)
    lvl = level(current.price, [h.price for h in today])
    cur = prices.currency

    speech = f"Electricity is {lvl} right now, {say_rate(current.price, cur)}."
    if block:
        a, b, avg = block
        speech += f" The cheapest stretch is {say_span(a.start, b.end, now)}, around {say_rate(avg, cur)}."
    if priciest.end > now:
        speech += f" Avoid {say_clock(priciest.start)} to {say_clock(priciest.end)}, the priciest hour today."
    if not has_tomorrow:
        speech += " Tomorrow's prices aren't published yet."
    return Outlook(
        speech=speech, next_24h=[PricePoint(start=h.start.isoformat(), price=h.price) for h in ahead],
        now_local=now.isoformat(timespec="minutes"),
        price_now=current.price, level_now=lvl,
        cheapest_window_start=block[0].start.isoformat() if block else None,
        cheapest_window_end=block[1].end.isoformat() if block else None,
        cheapest_window_avg_price=round(block[2], 5) if block else None,
        priciest_hour_today=priciest.start.isoformat(), tomorrow_published=has_tomorrow,
        currency=cur, price_source=source,
    )


@mcp.tool(annotations=ToolAnnotations(title="Best time to run an appliance", **READ_ONLY))
def find_best_time(
    appliance: ApplianceId = "dishwasher",
    earliest: str = Field("", description="Earliest start, local 24h 'HH:MM' (or 'tomorrow 07:00'). Empty = now."),
    deadline: str = Field("", description="Must be finished by, local 24h 'HH:MM'. Empty = within 24 hours."),
    duration_minutes: int = Field(0, description="Override the appliance's typical run time. Required for 'custom'."),
    power_kw: float = Field(0.0, description="Override the average power draw in kW. Required for 'custom'."),
) -> BestTime:
    """Find the cheapest start time for one appliance run and how much it saves versus starting now.
    Use for: 'when should I run the dishwasher?', 'charge the car before 7', 'best time for laundry'."""
    base = APPLIANCES.get(appliance) or Appliance("appliance", 60, 1.0, 1.0)
    a = Appliance(base.label, duration_minutes or base.minutes, power_kw or base.avg_kw, max(power_kw, base.peak_kw))
    now = prices.now()
    hours, has_tomorrow, source = prices.horizon(now)
    start, end = resolve_window(earliest, deadline, now, hours)
    best = best_start(hours, a.minutes, a.avg_kw, start, end)
    cur = prices.currency
    common = dict(appliance=a.label, energy_kwh=a.kwh, currency=cur, price_source=source)

    if best is None:
        why = "" if has_tomorrow else " Tomorrow's prices are usually published around 8:15 in the evening."
        return BestTime(speech=f"I can't fit a {a.minutes}-minute {a.label} run in that window with the prices I have.{why}",
                        best_start=None, best_end=None, cost_best=None, cost_if_started_asap=None,
                        savings_percent=None, **common)

    asap = run_cost(hours, start, a.minutes, a.avg_kw)
    saving = round(100 * (1 - best.cost / asap), 1) if asap and asap > 0 else None
    if best.start - start < timedelta(minutes=15) or not saving or saving < 3:
        speech = f"Now is a good time for the {a.label}: it will cost about {say_money(best.cost, cur)}."
    else:
        speech = (f"Start the {a.label} {say_when(best.start, now)}. It will cost about "
                  f"{say_money(best.cost, cur)} instead of {say_money(asap, cur)} if you start it right away. "
                  f"That's {saving:.0f} percent less.")
    return BestTime(speech=speech, best_start=best.start.isoformat(), best_end=best.end.isoformat(),
                    cost_best=round(best.cost, 4), cost_if_started_asap=round(asap, 4) if asap else None,
                    savings_percent=saving, **common)


@mcp.tool(annotations=ToolAnnotations(title="Plan several appliance runs", **READ_ONLY))
def plan_my_day(
    appliances: list[ApplianceId] = Field(description="Appliances to run once each, e.g. ['washing_machine','tumble_dryer','ev_charger']"),
    earliest: str = Field("", description="Earliest start, local 24h 'HH:MM'. Empty = now."),
    deadline: str = Field("", description="Everything finished by, local 24h 'HH:MM'. Empty = within 24 hours."),
    power_limit_kw: float = Field(0.0, description="Contracted power limit in kW. 0 = household default."),
) -> DayPlan:
    """Schedule several appliances at their cheapest times without tripping the home's power limit.
    The dryer runs within 3 hours after the washing machine. Use for: 'plan my laundry and charge the car tonight'."""
    limit = power_limit_kw or POWER_LIMIT_KW
    tasks = [Task(i, APPLIANCES[i].label, APPLIANCES[i].minutes, APPLIANCES[i].avg_kw, APPLIANCES[i].peak_kw)
             for i in dict.fromkeys(appliances) if i in APPLIANCES]
    now = prices.now()
    hours, has_tomorrow, source = prices.horizon(now)
    start, end = resolve_window(earliest, deadline, now, hours)
    result = plan(hours, tasks, start, end, limit, BASE_LOAD_KW)
    cur = prices.currency

    runs, parts, total, missed = [], [], 0.0, []
    for t, c in sorted(result, key=lambda r: r[1].start if r[1] else end):
        runs.append(PlannedRun(appliance=t.label, start=c.start.isoformat() if c else None,
                               end=c.end.isoformat() if c else None, cost=round(c.cost, 4) if c else None))
        if c:
            total += c.cost
            parts.append(f"the {t.label} {say_when(c.start, now)}")
        else:
            missed.append(t.label)

    asap_costs = [run_cost(hours, start, t.minutes, t.avg_kw) for t in tasks]
    asap = sum(asap_costs) if all(x is not None for x in asap_costs) and not missed else None
    asap_overload = BASE_LOAD_KW + sum(t.peak_kw for t in tasks) > limit
    monthly = round((asap - total) * 30, 2) if asap and asap > total else None

    speech = "Here's the plan: " + (", ".join(parts[:-1]) + " and " + parts[-1] if len(parts) > 1 else parts[0] if parts else "nothing fits") + "."
    speech += f" Total about {say_money(total, cur)}"
    if asap and asap > total:
        speech += f" instead of {say_money(asap, cur)}, roughly {say_money(monthly, cur)} a month if you do this every day"
    speech += "."
    if asap_overload:
        speech += f" Running everything at once would trip your {limit:g} kilowatt limit, so I staggered them."
    if missed:
        speech += f" I couldn't fit the {', '.join(missed)} in that window."
        if not has_tomorrow:
            speech += " Tomorrow's prices aren't out yet."
    return DayPlan(speech=speech, runs=runs, total_cost=round(total, 4),
                   cost_if_all_started_asap=round(asap, 4) if asap else None,
                   monthly_savings_estimate=monthly, asap_would_exceed_power_limit=asap_overload,
                   power_limit_kw=limit, currency=cur, price_source=source)


# ---------- resource & prompt ----------

@mcp.resource("gridshift://appliances", name="appliance-catalog", mime_type="application/json",
              description="Typical run time, average and peak power of each supported appliance.")
def appliance_catalog() -> str:
    return json.dumps(catalog(), indent=2)


@mcp.prompt(title="Morning energy briefing")
def energy_briefing() -> str:
    """A spoken energy briefing for the household."""
    return ("Give me a short spoken energy briefing: call price_outlook, then tell me the best time "
            "today for the dishwasher and the washing machine. Keep it under four sentences.")


if __name__ == "__main__":
    mcp.run("streamable-http", host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "8000")))
