"""Typical household loads. avg_kw drives cost, peak_kw drives the power-limit check."""

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Appliance:
    label: str
    minutes: int
    avg_kw: float
    peak_kw: float

    @property
    def kwh(self) -> float:
        return round(self.avg_kw * self.minutes / 60, 2)


APPLIANCES: dict[str, Appliance] = {
    "dishwasher": Appliance("dishwasher", 120, 0.55, 2.0),
    "washing_machine": Appliance("washing machine", 90, 0.6, 2.0),
    "tumble_dryer": Appliance("tumble dryer", 120, 1.2, 2.5),
    "ev_charger": Appliance("EV charging", 300, 3.7, 3.7),
    "oven": Appliance("oven", 60, 1.4, 2.5),
    "water_heater": Appliance("water heater", 90, 1.5, 1.5),
    "pool_pump": Appliance("pool pump", 240, 0.75, 0.75),
    "heat_pump": Appliance("heat pump", 120, 1.0, 1.5),
}


def catalog() -> list[dict]:
    return [{"id": k, **asdict(a), "kwh": a.kwh} for k, a in APPLIANCES.items()]
