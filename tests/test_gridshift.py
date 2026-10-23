import asyncio
import io
import json
import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

os.environ["GRIDSHIFT_TARIFF"] = "sample"

from gridshift import prices as P
from gridshift.planner import Task, best_start, parse_time, plan, resolve_window, run_cost
from gridshift.prices import EsiosPublic, Hour, TimeOfUse

TZ = ZoneInfo("Europe/Madrid")


def flat_day(d, values):
    return P._hours_from_local_midnight(d, TZ, values)


def test_esios_public_parsing_on_dst_day(monkeypatch):
    # 25 Oct 2026: clocks go back, the day has 25 hours. File format as served by
    # api.esios.ree.es/archives/70 (same as parsed by aiopvpc / Home Assistant).
    rows = [{"Dia": "25/10/2026", "Hora": f"{i:02d}-{i + 1:02d}", "PCB": f"{100 + i},50", "CYM": "1,0"} for i in range(25)]
    body = json.dumps({"PVPC": rows}).encode()
    monkeypatch.setattr(P.urllib.request, "urlopen", lambda req, timeout: io.BytesIO(body))
    hours = EsiosPublic(TZ).day(date(2026, 10, 25))
    assert len(hours) == 25
    assert hours[0].start.isoformat() == "2026-10-25T00:00:00+02:00"
    assert hours[2].start.isoformat() == "2026-10-25T02:00:00+02:00"
    assert hours[3].start.isoformat() == "2026-10-25T02:00:00+01:00"  # the repeated hour
    assert hours[0].price == 0.1005  # 100,50 €/MWh -> €/kWh


def test_time_of_use_rates():
    tou = TimeOfUse(TZ, "0-7:0.10,7-16:0.20,16-21:0.40,21-24:0.20", "USD")
    day = tou.day(date(2026, 10, 23))
    assert [h.price for h in day][6:8] == [0.10, 0.20] and day[18].price == 0.40


def test_run_cost_and_best_start():
    d = date(2026, 10, 23)
    hours = flat_day(d, [0.30] * 24)
    hours[3] = Hour(hours[3].start, 0.05)
    hours[4] = Hour(hours[4].start, 0.05)
    s = hours[0].start
    assert abs(run_cost(hours, s, 60, 2.0) - 0.60) < 1e-9
    best = best_start(hours, 120, 1.0, s, hours[-1].end)
    assert best.start == hours[3].start and abs(best.cost - 0.10) < 1e-9


def test_plan_respects_power_limit_and_laundry_order():
    d = date(2026, 10, 23)
    hours = flat_day(d, [0.30] * 6 + [0.10] * 18)
    tasks = [
        Task("tumble_dryer", "tumble dryer", 120, 1.2, 2.5),
        Task("washing_machine", "washing machine", 90, 0.6, 2.0),
        Task("ev_charger", "EV charging", 300, 3.7, 3.7),
    ]
    result = dict((t.id, c) for t, c in plan(hours, tasks, hours[0].start, hours[-1].end, 4.6, 0.4))
    assert all(result.values())
    assert result["tumble_dryer"].start >= result["washing_machine"].end
    assert result["tumble_dryer"].start <= result["washing_machine"].end + timedelta(hours=3)
    ev = result["ev_charger"]
    for other in ("washing_machine", "tumble_dryer"):  # 3.7 + 2.x + 0.4 > 4.6 -> no overlap
        o = result[other]
        assert o.end <= ev.start or o.start >= ev.end


def test_parse_time_and_window_rollover():
    now = datetime(2026, 10, 23, 13, 10, tzinfo=TZ)
    assert parse_time("7pm", now)[0].hour == 19
    assert parse_time("tomorrow at 6am", now)[0].day == 24
    hours = flat_day(date(2026, 10, 23), [0.2] * 24) + flat_day(date(2026, 10, 24), [0.2] * 24)
    start, end = resolve_window("22:00", "07:00", now, hours)
    assert (start.day, start.hour, end.day, end.hour) == (23, 22, 24, 7)
    start, _ = resolve_window("07:00", "", now, hours)  # morning already passed -> tomorrow
    assert start.day == 24


def test_mcp_tools_end_to_end():
    from mcp import Client
    import server

    async def run():
        async with Client(server.mcp) as c:
            names = {t.name for t in (await c.list_tools()).tools}
            assert names == {"price_outlook", "find_best_time", "plan_my_day"}
            r = await c.call_tool("plan_my_day", {"appliances": ["washing_machine", "tumble_dryer", "ev_charger"]})
            assert not r.is_error and "plan" in r.structured_content["speech"]
            r = await c.call_tool("find_best_time", {"appliance": "dishwasher", "deadline": "07:00"})
            assert r.structured_content["best_start"]

    asyncio.run(run())
