"""
Загрузка сценария игрового дня (YAML). Формат описан в config/scenarios/day1.yaml.
"""
import os
from dataclasses import dataclass, field

import yaml

from .clock import hhmm_to_min

DEFAULT_PENALTIES = {
    # перегруз: потребление выше верхней границы коридора
    "on_overload": {"grace_s": 30, "trip": "auto", "duration_min": 10, "level": "CRIT",
                    "message": "Перегрузка сети купола! Аварийная защита отключила линии."},
    # гибернация: потребление ниже нижней границы коридора
    "on_underload": {"grace_s": 60, "trip": "all", "duration_min": 5, "level": "CRIT",
                     "message": "Потребление ниже порога. Система купола ушла в гибернацию."},
    # дефицит: генерации и активного АКБ не хватает, чтобы покрыть потребление
    "on_deficit": {"grace_s": 10, "trip": "auto", "duration_min": 10, "level": "CRIT",
                   "message": "Дефицит мощности! Защита отключила линии для сохранения сети."},
}


@dataclass
class Phase:
    idx: int
    name: str
    start: int            # минуты от полуночи
    end: int
    corridor: dict
    solar_factor: float
    locked_lines: dict    # {line_id: причина}
    penalties: dict
    message: dict | None
    batteries: dict


@dataclass
class Event:
    idx: int
    at: int
    level: str
    message: str | None
    batteries: dict
    lock_lines: dict | None
    set_active_battery: str | None


@dataclass
class Scenario:
    id: str
    path: str
    day: str
    date: str | None
    start: int
    end: int
    forecast_csv: str
    history_step_s: float
    batteries: list
    active_battery: str
    defaults: dict
    phases: list = field(default_factory=list)
    events: list = field(default_factory=list)

    def phase_at(self, minute: float) -> Phase | None:
        for p in self.phases:
            if p.start <= minute < p.end:
                return p
        return None

    def corridor_at(self, minute: float) -> dict:
        p = self.phase_at(minute)
        return p.corridor if p else self.defaults["corridor"]


def _msg(m):
    if m is None:
        return None
    if isinstance(m, str):
        return {"level": "INFO", "text": m}
    return {"level": m.get("level", "INFO"), "text": m["text"]}


def _merge_penalties(base: dict, override: dict | None) -> dict:
    out = {k: dict(v) for k, v in base.items()}
    for k, v in (override or {}).items():
        if k in out and isinstance(v, dict):
            out[k].update(v)
    return out


def _locked(spec, reason_default: str) -> dict:
    if not spec:
        return {}
    if isinstance(spec, list):
        return {int(x): reason_default for x in spec}
    return {int(k): (v or reason_default) for k, v in spec.items()}


def load_scenario(path: str) -> Scenario:
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    base_dir = os.path.dirname(os.path.abspath(path))

    d = raw.get("defaults", {}) or {}
    defaults = {
        "corridor": d.get("corridor", {"min_kw": 0.0, "max_kw": 999.0}),
        "solar_factor": float(d.get("solar_factor", 1.0)),
        "penalties": _merge_penalties(DEFAULT_PENALTIES, {k: d[k] for k in DEFAULT_PENALTIES if k in d}),
    }

    phases = []
    for i, p in enumerate(raw.get("phases", []) or []):
        phases.append(Phase(
            idx=i,
            name=p.get("name", f"Фаза {i + 1}"),
            start=hhmm_to_min(p["from"]),
            end=hhmm_to_min(p["to"]),
            corridor={**defaults["corridor"], **(p.get("corridor") or {})},
            solar_factor=float(p.get("solar_factor", defaults["solar_factor"])),
            locked_lines=_locked(p.get("locked_lines"), p.get("lock_reason", "отключена по распоряжению штаба")),
            penalties=_merge_penalties(defaults["penalties"], {k: p[k] for k in DEFAULT_PENALTIES if k in p}),
            message=_msg(p.get("message")),
            batteries=p.get("batteries") or {},
        ))
    phases.sort(key=lambda x: x.start)

    events = []
    for i, e in enumerate(raw.get("events", []) or []):
        events.append(Event(
            idx=i,
            at=hhmm_to_min(e["at"]),
            level=e.get("level", "INFO"),
            message=e.get("message"),
            batteries=e.get("batteries") or {},
            lock_lines=e.get("lock_lines"),
            set_active_battery=e.get("set_active_battery"),
        ))
    events.sort(key=lambda x: x.at)

    fc = raw["forecast_csv"]
    if not os.path.isabs(fc):
        fc = os.path.normpath(os.path.join(base_dir, fc))

    bats = raw.get("batteries", [])
    return Scenario(
        id=os.path.splitext(os.path.basename(path))[0],
        path=path,
        day=raw.get("day", "Игровой день"),
        date=str(raw["date"]) if raw.get("date") else None,
        start=hhmm_to_min(raw.get("start", "08:00")),
        end=hhmm_to_min(raw.get("end", "20:00")),
        forecast_csv=fc,
        history_step_s=float(raw.get("history_step_s", 30)),
        batteries=bats,
        active_battery=raw.get("active_battery", bats[0]["id"] if bats else None),
        defaults=defaults,
        phases=phases,
        events=events,
    )
