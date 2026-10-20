"""
Игровой движок купола. Работает в отдельном потоке, раз в tick_s секунд:
  1. читает показания реле и генерацию;
  2. применяет сценарий: фазы (коридор, блокировки линий, коэффициент солнца), события;
  3. считает баланс через активный АКБ;
  4. отслеживает выход из коридора / дефицит и применяет последствия (отключение линий);
  5. пишет историю (CSV) и снимок состояния (JSON) — после перезапуска игра продолжается.

Поверх сценария действуют переопределения организатора (админка, admin_* методы):
блокировки линий, границы коридора, коэффициент солнца, пауза/отмена нарушений и фаз.
Действия админа не пишутся в терминал участников — для этого есть команда msg.
"""
import csv
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, date

from .battery import Battery
from .clock import GameClock, dt_to_min
from .linemap import LineMap
from .relays import RelayManager
from .scenario import Scenario
from .solar import Forecast
from .terminal import Terminal

log = logging.getLogger("engine")

KIND_TITLES = {"overload": "Overload", "underload": "Hibernation", "deficit": "Power deficit"}
KIND_WARN = {
    "overload": "WARNING: consumption {cons:.2f} kW is above the {max:.2f} kW limit. "
                "Protection trips in {grace:.0f} s.",
    "underload": "WARNING: consumption {cons:.2f} kW is below the {min:.2f} kW threshold. "
                 "Hibernation in {grace:.0f} s.",
    "deficit": "WARNING: generation and battery are short by {deficit:.2f} kW. "
               "Emergency shutdown in {grace:.0f} s.",
}
ADMIN_LOCK_REASON = "Locked by Mission Control"

# поля АКБ, которые можно менять из админки
BATTERY_FIELDS = {
    "name": str, "description": str, "status": str,
    "capacity_kwh": float, "nominal_kwh": float, "charge_kwh": float,
    "max_charge_kw": float, "max_discharge_kw": float, "peak_discharge_kw": float,
    "efficiency": float, "self_discharge_pct_h": float,
    "overload_tolerance_s": float, "fault_recovery_min": float,
}
BATTERY_STATUSES = ("ok", "degraded", "fault", "failed")


@dataclass
class LineState:
    id: int
    lock_until: datetime | None = None     # блокировка событием сценария
    lock_reason: str = ""
    trip_until: datetime | None = None     # аварийное отключение (последствие)
    trip_reason: str = ""
    admin_lock: bool = False               # блокировка организатором
    admin_until: datetime | None = None    # None — бессрочно
    admin_reason: str = ""
    unlock_phase: int | None = None        # админ снял блокировку фазы с этим индексом
    last_enforce: float = 0.0


def _iso(dt):
    return dt.isoformat() if dt else None


def _from_iso(s):
    return datetime.fromisoformat(s) if s else None


def _hm(dt):
    return dt.strftime("%H:%M") if dt else None


class Engine:
    def __init__(self, scenario: Scenario, clock: GameClock, relays: RelayManager, solar,
                 forecast: Forecast, terminal: Terminal, linemap: LineMap, logs_dir: str, tick_s: float = 1.0):
        self.sc = scenario
        self.clock = clock
        self.relays = relays
        self.solar = solar
        self.forecast = forecast
        self.term = terminal
        self.map = linemap
        self.tick_s = tick_s
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None

        self.batteries: dict[str, Battery] = {b["id"]: Battery(**b) for b in scenario.batteries}
        self.active: str | None = scenario.active_battery
        self.lines = {lid: LineState(lid) for lid in relays.lines}
        self.fired: set[str] = set()
        self.stats = {"overload": 0, "underload": 0, "deficit": 0, "out_of_corridor_s": 0.0,
                      "deficit_s": 0.0, "curtailed_kwh": 0.0, "deficit_kwh": 0.0}
        self.timers = {k: 0.0 for k in KIND_TITLES}
        self.warned = {k: False for k in KIND_TITLES}
        self.penalty_until: dict[str, datetime | None] = {k: None for k in KIND_TITLES}
        # переопределения организатора
        self.solar_admin = 1.0
        self.corr_override: dict[str, dict | None] = {"min": None, "max": None}  # {value, until}
        self.crisis_paused = False
        self.phase_off: dict[int, str] = {}   # индекс фазы → paused | cancelled

        self.history: list[dict] = []
        self._last_now: datetime | None = None
        self._last_sample: datetime | None = None
        self._last_save = 0.0
        self._snap: dict = {}
        self._gen = self._cons = self._deficit = 0.0
        self._solar_factor = 1.0

        os.makedirs(logs_dir, exist_ok=True)
        self.state_path = os.path.join(logs_dir, f"state_{scenario.id}.json")
        self.telemetry_path = os.path.join(logs_dir, f"telemetry_{scenario.id}.csv")
        self.actions = logging.getLogger("actions")
        ah = logging.FileHandler(os.path.join(logs_dir, f"actions_{scenario.id}.log"), encoding="utf-8")
        ah.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        self.actions.addHandler(ah)
        self.actions.propagate = False
        self.actions.setLevel(logging.INFO)

        self._load_state()
        self._load_history()

    def name(self, lid: int) -> str:
        return self.map.name(lid)

    # ------------------------------------------------------------------ поток
    def start(self):
        self._thread = threading.Thread(target=self._loop, name="Engine", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        with self._lock:
            self._save_state()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("Ошибка такта движка")
            self._stop.wait(self.tick_s)

    # ------------------------------------------------------------------ такт
    def _game_status(self, now: datetime) -> str:
        if self.sc.date:
            d = date.fromisoformat(self.sc.date)
            if now.date() < d:
                return "before"
            if now.date() > d:
                return "finished"
        m = dt_to_min(now)
        if m < self.sc.start:
            return "before"
        if m >= self.sc.end:
            return "finished"
        return "running"

    def _scenario_phase(self, now, status=None):
        """Фаза по расписанию сценария (без учёта паузы/отмены)."""
        if (status or self._game_status(now)) != "running":
            return None
        return self.sc.phase_at(dt_to_min(now))

    def _phase(self, now, status=None):
        """Действующая фаза: приостановленная или отменённая админом не действует."""
        p = self._scenario_phase(now, status)
        return None if p is None or p.idx in self.phase_off else p

    def _corridor(self, phase) -> dict:
        base = phase.corridor if phase else self.sc.defaults["corridor"]
        c = {"min_kw": float(base["min_kw"]), "max_kw": float(base["max_kw"])}
        for b in ("min", "max"):
            o = self.corr_override[b]
            if o:
                c[f"{b}_kw"] = o["value"]
        return c

    def tick(self):
        now = self.clock.now()
        minute = dt_to_min(now)
        to_off: list[int] = []
        with self._lock:
            dt_s = 0.0 if self._last_now is None else (now - self._last_now).total_seconds()
            dt_s = min(max(dt_s, 0.0), 120.0 * self.clock.speed)
            self._last_now = now
            status = self._game_status(now)

            self._run_scenario(now, minute, status, self._scenario_phase(now, status))
            phase = self._phase(now, status)
            self._solar_factor = (phase.solar_factor if phase else self.sc.defaults["solar_factor"]) * self.solar_admin
            self.solar.set_factor(self._solar_factor)
            self._expire(now)
            corridor = self._corridor(phase)

            readings = self.relays.readings()
            gen = self.solar.power_kw()
            cons = sum(r.kw for r in readings.values() if r.on and r.online)
            dt_h = dt_s / 3600.0

            deficit = curtailed = 0.0
            if status == "running":
                for bid, b in self.batteries.items():
                    if bid == self.active:
                        deficit, curtailed = b.step(gen - cons, dt_h, now)
                        fault_key = f"fault:{bid}:{_iso(b.fault_until)}"
                        if b.status == "fault" and fault_key not in self.fired:
                            self.fired.add(fault_key)
                            self.term.post(f"{b.name}: discharge power overload, battery protection tripped. "
                                           f"Recovery at {b.fault_until:%H:%M}. Switch to another storage unit!",
                                           "CRIT", now)
                    else:
                        b.idle(dt_h, now)
                if self.active is None:
                    deficit, curtailed = max(0.0, cons - gen), max(0.0, gen - cons)
                self.stats["curtailed_kwh"] += curtailed * dt_h
                self.stats["deficit_kwh"] += deficit * dt_h
                penalties = phase.penalties if phase else self.sc.defaults["penalties"]
                if not self.crisis_paused:
                    to_off += self._check_violations(now, dt_s, cons, deficit, corridor, penalties, readings)
            else:
                for b in self.batteries.values():
                    b.power_kw = 0.0

            self._gen, self._cons, self._deficit = gen, cons, deficit

            # всё, что заблокировано/отключено, но физически включено — выключаем
            for lid, r in readings.items():
                if r.on and self._line_block(lid, now, phase) and time.time() - self.lines[lid].last_enforce > 3:
                    self.lines[lid].last_enforce = time.time()
                    to_off.append(lid)

            self._sample_history(now, minute, gen, cons, corridor, readings, status)
            self._snap = self._build_snapshot(now, minute, status, phase, corridor, readings)
            if time.time() - self._last_save > 5:
                self._save_state()
                self._last_save = time.time()

        for lid in dict.fromkeys(to_off):
            self.relays.set(lid, False)

    # ------------------------------------------------------------------ сценарий
    def _apply_batteries(self, spec: dict):
        for bid, fields in (spec or {}).items():
            b = self.batteries.get(str(bid))
            if b:
                b.update(**fields)

    def _run_scenario(self, now, minute, status, phase):
        if phase and f"phase:{phase.idx}" not in self.fired:
            self.fired.add(f"phase:{phase.idx}")
            if phase.message:
                self.term.post(phase.message["text"], phase.message["level"], now)
            self._apply_batteries(phase.batteries)
            if phase.locked_lines:
                ids = [self.map.resolve(r) for r in phase.locked_lines]
                names = ", ".join(self.name(l) for l in ids if l in self.lines)
                if names:
                    self.term.post(f"Locked by order of Mission Control: {names}.", "WARN", now)
        if status == "finished" and "end" not in self.fired:
            self.fired.add("end")
            self.term.post("The game day is over. Thank you for your shift, dispatcher!", "OK", now)
        for e in self.sc.events:
            key = f"event:{e.idx}"
            if key in self.fired or e.at > minute or status == "finished":
                continue
            if status == "before" and self.sc.date and now.date() < date.fromisoformat(self.sc.date):
                continue
            self.fired.add(key)
            if e.message:
                self.term.post(e.message, e.level, now)
            self._apply_batteries(e.batteries)
            if e.set_active_battery:
                self.active = e.set_active_battery
            if e.lock_lines:
                until = now + timedelta(minutes=float(e.lock_lines.get("duration_min", 30)))
                reason = e.lock_lines.get("reason", "Locked by scenario")
                for ref in e.lock_lines.get("lines", []):
                    ls = self.lines.get(self.map.resolve(ref))
                    if ls:
                        ls.lock_until, ls.lock_reason = until, reason

    def _expire(self, now):
        for lid, ls in self.lines.items():
            if ls.lock_until and now >= ls.lock_until:
                ls.lock_until, ls.lock_reason = None, ""
                self.term.post(f"{self.name(lid)}: lock lifted, the line can be switched on.", "OK", now)
            if ls.trip_until and now >= ls.trip_until:
                ls.trip_until, ls.trip_reason = None, ""
                self.term.post(f"{self.name(lid)}: emergency lock lifted, the line can be switched on.", "OK", now)
            if ls.admin_lock and ls.admin_until and now >= ls.admin_until:
                ls.admin_lock, ls.admin_until, ls.admin_reason = False, None, ""
                self.actions.info("ADMIN LOCK EXPIRED line=%d", lid)
        for b, o in self.corr_override.items():
            if o and o["until"] and now >= o["until"]:
                self.corr_override[b] = None
                self.actions.info("ADMIN CORRIDOR %s override expired", b)

    def _phase_lock(self, lid, phase) -> str | None:
        for ref, reason in phase.locked_lines.items():
            if self.map.resolve(ref) == lid:
                return reason
        return None

    def _line_block(self, lid, now, phase) -> tuple[str, str, datetime | None] | None:
        """Почему линию нельзя включать: (тип, причина, до какого времени) или None."""
        ls = self.lines[lid]
        if ls.admin_lock and (ls.admin_until is None or now < ls.admin_until):
            return "locked", ls.admin_reason or ADMIN_LOCK_REASON, ls.admin_until
        if ls.trip_until and now < ls.trip_until:
            return "tripped", ls.trip_reason, ls.trip_until
        if ls.lock_until and now < ls.lock_until:
            return "locked", ls.lock_reason, ls.lock_until
        if phase and ls.unlock_phase != phase.idx:
            reason = self._phase_lock(lid, phase)
            if reason:
                end = datetime.combine(now.date(), datetime.min.time()) + timedelta(minutes=phase.end)
                return "locked", reason, end
        return None

    # ------------------------------------------------------------------ коридор
    def _check_violations(self, now, dt_s, cons, deficit, corridor, penalties, readings) -> list[int]:
        cond = {
            "overload": cons > corridor["max_kw"],
            "underload": cons < corridor["min_kw"],
            "deficit": deficit > 0.01,
        }
        if cond["overload"] or cond["underload"]:
            self.stats["out_of_corridor_s"] += dt_s
        if cond["deficit"]:
            self.stats["deficit_s"] += dt_s
        to_off = []
        for kind, bad in cond.items():
            spec = penalties[f"on_{kind}"]
            pu = self.penalty_until[kind]
            if pu and now < pu:          # последствие уже действует — не наказываем повторно
                self.timers[kind] = 0.0
                continue
            if not bad:
                if self.warned[kind]:
                    self.term.post(f"{KIND_TITLES[kind]}: parameters are back to normal.", "OK", now)
                self.timers[kind], self.warned[kind] = 0.0, False
                continue
            self.timers[kind] += dt_s
            grace = float(spec.get("grace_s", 30))
            if not self.warned[kind]:
                self.warned[kind] = True
                self.term.post(KIND_WARN[kind].format(cons=cons, max=corridor["max_kw"], min=corridor["min_kw"],
                                                      deficit=deficit, grace=grace), "WARN", now)
            if self.timers[kind] >= grace:
                to_off += self._apply_penalty(kind, spec, now, cons, deficit, corridor, readings)
                self.timers[kind], self.warned[kind] = 0.0, False
        return to_off

    def _pick_lines(self, trip, kind, cons, deficit, corridor, readings) -> list[int]:
        on = sorted((lid for lid, r in readings.items() if r.on), key=lambda l: -readings[l].kw)
        if trip == "all":
            return on
        if isinstance(trip, list):
            return [l for l in (self.map.resolve(x) for x in trip) if l is not None]
        if isinstance(trip, str) and trip.startswith("top:"):
            return on[:int(trip[4:])]
        # auto: отключаем самые мощные, пока не уйдём из нарушения
        if kind == "overload":
            need = cons - corridor["max_kw"]
        elif kind == "deficit":
            need = deficit
        else:
            return on
        out, removed = [], 0.0
        for lid in on:
            if removed >= need and out:
                break
            out.append(lid)
            removed += readings[lid].kw
        return out

    def _apply_penalty(self, kind, spec, now, cons, deficit, corridor, readings) -> list[int]:
        dur = float(spec.get("duration_min", 10))
        until = now + timedelta(minutes=dur)
        ids = [l for l in self._pick_lines(spec.get("trip", "auto"), kind, cons, deficit, corridor, readings)
               if l in self.lines]
        reason = f"{KIND_TITLES[kind]}: emergency trip"
        for lid in ids:
            ls = self.lines[lid]
            ls.trip_until = max(ls.trip_until or until, until)
            ls.trip_reason = reason
        self.penalty_until[kind] = until
        self.stats[kind] += 1
        names = ", ".join(self.name(l) for l in ids)
        tail = f" Tripped: {names}. Locked until {until:%H:%M}." if ids else f" Active until {until:%H:%M}."
        self.term.post(spec.get("message", reason) + tail, spec.get("level", "CRIT"), now)
        self.actions.info("PENALTY %s lines=%s until=%s", kind, ids, until)
        return ids

    # ------------------------------------------------------------------ действия участников
    def set_line(self, lid: int, on: bool, who: str = "LINE") -> tuple[bool, str]:
        now = self.clock.now()
        ru = who.startswith("ADMIN")
        with self._lock:
            if lid not in self.lines:
                return False, "Нет такой линии" if ru else "No such line"
            block = self._line_block(lid, now, self._phase(now))
            if on and block:
                kind, reason, until = block
                if ru:
                    what = "аварийно отключена" if kind == "tripped" else "заблокирована"
                    return False, f"{what}: {reason}" + (f" (до {until:%H:%M})" if until else "") + ". Сначала unlock"
                return False, f"{reason} (until {until:%H:%M})" if until else reason
        ok = self.relays.set(lid, on)
        self.actions.info("%s %d %s -> %s", who, lid, "ON" if on else "OFF", "ok" if ok else "FAIL")
        if not ok:
            return False, "Реле не ответило" if ru else "Relay did not respond"
        self._refresh()
        return True, ""

    def set_active_battery(self, bid: str, force: bool = False) -> tuple[bool, str]:
        now = self.clock.now()
        with self._lock:
            b = self.batteries.get(bid)
            if not b:
                return False, f"Нет АКБ '{bid}'" if force else "No such storage unit"
            if not b.working and not force:
                return False, f"{b.name} is unavailable ({'protection' if b.status == 'fault' else 'failure'})"
            if self.active != bid:
                self.active = bid
                if not force:
                    self.term.post(f"Active storage unit: {b.name}.", "INFO", now)
                self.actions.info("%s active=%s", "ADMIN BATTERY" if force else "BATTERY", bid)
            self._snap["active_battery"] = bid
            return True, ""

    def admin_message(self, text: str, level: str = "INFO"):
        self.term.post(text, level, self.clock.now())
        self.actions.info("ADMIN MSG %s %s", level, text)

    def _refresh(self):
        """Пересобрать снимок сразу после действия, не дожидаясь такта движка."""
        readings = self.relays.readings()
        now = self.clock.now()
        with self._lock:
            if not self._snap:
                return
            status = self._game_status(now)
            phase = self._phase(now, status)
            self._solar_factor = (phase.solar_factor if phase else self.sc.defaults["solar_factor"]) * self.solar_admin
            self.solar.set_factor(self._solar_factor)
            self._snap = self._build_snapshot(now, dt_to_min(now), status, phase, self._corridor(phase), readings)

    # ------------------------------------------------------------------ админка
    def admin_line(self, lid: int, action: str, minutes: float | None = None,
                   reason: str | None = None) -> tuple[bool, str]:
        if lid not in self.lines:
            return False, f"Нет линии {lid}"
        if action in ("on", "off"):
            return self.set_line(lid, action == "on", who="ADMIN LINE")
        now = self.clock.now()
        with self._lock:
            ls = self.lines[lid]
            if action == "lock":
                ls.admin_lock = True
                ls.admin_until = now + timedelta(minutes=minutes) if minutes else None
                ls.admin_reason = reason or ADMIN_LOCK_REASON
                ls.last_enforce = 0.0
                self.actions.info("ADMIN LOCK line=%d until=%s reason=%s", lid, ls.admin_until, ls.admin_reason)
            elif action == "unlock":
                ls.admin_lock, ls.admin_until, ls.admin_reason = False, None, ""
                ls.lock_until, ls.lock_reason = None, ""
                ls.trip_until, ls.trip_reason = None, ""
                phase = self._phase(now)
                if phase and self._phase_lock(lid, phase):
                    ls.unlock_phase = phase.idx
                self.actions.info("ADMIN UNLOCK line=%d", lid)
            else:
                return False, f"Неизвестное действие '{action}'"
        if action == "lock":
            self.relays.set(lid, False)
        self._refresh()
        return True, ""

    def admin_name(self, lid: int, key: str | None) -> tuple[bool, str]:
        ok, msg = self.map.assign(lid, key)
        if ok:
            self.actions.info("ADMIN NAME line=%d consumer=%s", lid, key)
            self._refresh()
        return ok, msg

    def identify(self, lid: int, times: int = 3) -> tuple[bool, str]:
        """Помигать линией (выкл/вкл), чтобы найти её на площадке."""
        if lid not in self.lines:
            return False, f"Нет линии {lid}"

        def run():
            orig = self.relays.readings()[lid].on
            for _ in range(times):
                self.relays.set(lid, not orig)
                time.sleep(1.0)
                self.relays.set(lid, orig)
                time.sleep(1.0)
        threading.Thread(target=run, name=f"identify-{lid}", daemon=True).start()
        self.actions.info("ADMIN IDENTIFY line=%d", lid)
        return True, ""

    def admin_corridor(self, bound: str, value: float | None, minutes: float | None = None) -> tuple[bool, str]:
        """value=None — вернуть границу из сценария. bound: min | max | all (только для сброса)."""
        now = self.clock.now()
        with self._lock:
            if value is None:
                for b in (("min", "max") if bound == "all" else (bound,)):
                    self.corr_override[b] = None
                self.actions.info("ADMIN CORRIDOR reset %s", bound)
                self._refresh()
                return True, ""
            if bound not in ("min", "max"):
                return False, "Граница — min или max"
            if value < 0:
                return False, "Значение должно быть >= 0"
            cur = self._corridor(self._phase(now))
            if bound == "min" and value >= cur["max_kw"]:
                return False, f"min должен быть меньше текущего max ({cur['max_kw']:.2f} кВт)"
            if bound == "max" and value <= cur["min_kw"]:
                return False, f"max должен быть больше текущего min ({cur['min_kw']:.2f} кВт)"
            until = now + timedelta(minutes=minutes) if minutes else None
            self.corr_override[bound] = {"value": float(value), "until": until}
            self.actions.info("ADMIN CORRIDOR %s=%.2f until=%s", bound, value, until)
            self._refresh()
            return True, ""

    def admin_battery(self, bid: str, fields: dict) -> tuple[bool, str]:
        now = self.clock.now()
        with self._lock:
            b = self.batteries.get(bid)
            if not b:
                return False, f"Нет АКБ '{bid}'. Есть: {', '.join(self.batteries)}"
            upd = {}
            for k, v in fields.items():
                if k == "soc":   # удобство: заряд в процентах от ёмкости
                    try:
                        upd["charge_kwh"] = b.capacity_kwh * float(v) / 100.0
                    except ValueError:
                        return False, f"soc: '{v}' — не число"
                    continue
                if k not in BATTERY_FIELDS:
                    return False, f"Неизвестное поле '{k}'. Поля: soc, {', '.join(BATTERY_FIELDS)}"
                if BATTERY_FIELDS[k] is float:
                    try:
                        v = float(v)
                    except ValueError:
                        return False, f"{k}: '{v}' — не число"
                    if v < 0 or (k == "efficiency" and v > 1):
                        return False, f"{k}: значение вне допустимого диапазона"
                if k == "status":
                    v = str(v).lower()
                    if v not in BATTERY_STATUSES:
                        return False, f"status — одно из: {', '.join(BATTERY_STATUSES)}"
                upd[k] = v
            if upd.get("status") == "fault":
                upd.pop("status")
                b.update(**upd)
                if b.status != "fault":
                    b.status_before_fault = b.status if b.working else "ok"
                    b.status = "fault"
                b.fault_until = now + timedelta(minutes=b.fault_recovery_min)
                b.overload_s = 0.0
            else:
                b.update(**upd)
            if b.capacity_kwh > (b.nominal_kwh or 0):
                b.nominal_kwh = b.capacity_kwh
            self.actions.info("ADMIN BATTERY %s %s", bid, fields)
            self._refresh()
            return True, ""

    def admin_solar(self, factor: float) -> tuple[bool, str]:
        if not 0.0 <= factor <= 2.0:
            return False, "Коэффициент генерации — от 0.0 до 2.0"
        with self._lock:
            self.solar_admin = float(factor)
            self.actions.info("ADMIN SOLAR factor=%.2f", factor)
        self._refresh()
        return True, ""

    def admin_crisis(self, action: str) -> tuple[bool, str]:
        """Нарушения коридора/дефицит: pause — заморозить отсчёт и наказания, resume, cancel —
        сбросить отсчёт, действующие последствия и аварийные отключения линий."""
        with self._lock:
            if action == "pause":
                self.crisis_paused = True
            elif action == "resume":
                self.crisis_paused = False
            elif action == "cancel":
                for k in KIND_TITLES:
                    self.timers[k], self.warned[k], self.penalty_until[k] = 0.0, False, None
                for ls in self.lines.values():
                    ls.trip_until, ls.trip_reason = None, ""
            else:
                return False, "Допустимо: pause | resume | cancel"
            self.actions.info("ADMIN CRISIS %s", action)
        self._refresh()
        return True, ""

    def admin_phase(self, action: str) -> tuple[bool, str]:
        """Текущая фаза сценария: pause (до resume) | cancel (до конца дня) | resume."""
        now = self.clock.now()
        with self._lock:
            p = self._scenario_phase(now)
            if action == "resume":
                if p is not None and p.idx in self.phase_off:
                    del self.phase_off[p.idx]
                elif self.phase_off:      # вне фазы — снять паузы со всех
                    self.phase_off = {i: s for i, s in self.phase_off.items() if s != "paused"}
                else:
                    return False, "Нечего возобновлять"
            elif action in ("pause", "cancel"):
                if p is None:
                    return False, "Сейчас нет активной фазы сценария"
                self.phase_off[p.idx] = "paused" if action == "pause" else "cancelled"
            else:
                return False, "Допустимо: pause | resume | cancel"
            self.actions.info("ADMIN PHASE %s %s", action, p.name if p else "-")
        self._refresh()
        return True, ""

    def admin_view(self) -> dict:
        now = self.clock.now()
        with self._lock:
            sp = self._scenario_phase(now)
            phase = self._phase(now)
            pen = phase.penalties if phase else self.sc.defaults["penalties"]
            base = phase.corridor if phase else self.sc.defaults["corridor"]
            return {
                "solar_admin": self.solar_admin,
                "solar_phase": phase.solar_factor if phase else self.sc.defaults["solar_factor"],
                "solar_factor": self._solar_factor,
                "corridor_base": {"min": base["min_kw"], "max": base["max_kw"]},
                "corridor_override": {b: ({"value": o["value"], "until": _hm(o["until"])} if o else None)
                                      for b, o in self.corr_override.items()},
                "crisis": {
                    "paused": self.crisis_paused,
                    "timers": {k: {"elapsed_s": round(self.timers[k], 1),
                                   "grace_s": float(pen[f"on_{k}"].get("grace_s", 30))} for k in KIND_TITLES},
                    "penalties": {k: _hm(v) for k, v in self.penalty_until.items() if v and now < v},
                },
                "phase": {"name": sp.name if sp else None, "idx": sp.idx if sp else None,
                          "state": (self.phase_off.get(sp.idx, "active") if sp else None),
                          "to": f"{sp.end // 60:02d}:{sp.end % 60:02d}" if sp else None},
                "phases": [{"idx": p.idx, "name": p.name,
                            "from": f"{p.start // 60:02d}:{p.start % 60:02d}",
                            "to": f"{p.end // 60:02d}:{p.end % 60:02d}",
                            "state": self.phase_off.get(p.idx, "scheduled")} for p in self.sc.phases],
                "lines_map": self.map.view(),
                "clock": {"mode": self.clock.mode, "speed": self.clock.speed},
            }

    # ------------------------------------------------------------------ данные для API
    def _lines_view(self, now, phase, readings):
        out = []
        for lid in self.lines:
            r = readings[lid]
            block = self._line_block(lid, now, phase)
            if not r.online:
                st = "offline"
            elif block:
                st = block[0]
            else:
                st = "on" if r.on else "off"
            out.append({"id": lid, "name": self.name(lid), "key": self.map.key(lid),
                        "on": r.on, "kw": round(r.kw if r.on else 0.0, 3),
                        "online": r.online, "status": st,
                        "reason": block[1] if block else "",
                        "until": _hm(block[2]) if block else None})
        return out

    def _build_snapshot(self, now, minute, status, phase, corridor, readings) -> dict:
        pen = phase.penalties if phase else self.sc.defaults["penalties"]
        alarms = []
        if not self.crisis_paused:
            for k in KIND_TITLES:
                if self.timers[k] > 0:
                    grace = float(pen[f"on_{k}"].get("grace_s", 30))
                    alarms.append({"kind": k, "title": KIND_TITLES[k],
                                   "left_s": round(max(0.0, grace - self.timers[k]) / self.clock.speed, 1)})
        return {
            "now": now.strftime("%H:%M:%S"), "date": now.strftime("%d.%m.%Y"), "t": round(minute, 3),
            "day": self.sc.day, "status": status, "phase": phase.name if phase else None,
            "start": self.sc.start, "end": self.sc.end, "speed": self.clock.speed,
            "gen": round(self._gen, 3), "cons": round(self._cons, 3), "net": round(self._gen - self._cons, 3),
            "deficit": round(self._deficit, 3),
            "corridor": {"min": corridor["min_kw"], "max": corridor["max_kw"]},
            "lines": self._lines_view(now, phase, readings),
            "batteries": [b.to_dict() for b in self.batteries.values()],
            "active_battery": self.active,
            "alarms": alarms,
            "stats": {k: (round(v, 3) if isinstance(v, float) else v) for k, v in self.stats.items()},
        }

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._snap)

    def history_view(self) -> list[dict]:
        with self._lock:
            return list(self.history)

    def forecast_view(self) -> dict:
        return self.forecast.as_dict(self.sc.start, self.sc.end)

    # ------------------------------------------------------------------ история / сохранение
    def _csv_header(self) -> list[str]:
        return (["time", "t", "gen_kw", "cons_kw", "min_kw", "max_kw", "active"]
                + [f"soc_{b}_kwh" for b in self.batteries] + [f"L{l}_kw" for l in sorted(self.lines)])

    def _sample_history(self, now, minute, gen, cons, corridor, readings, status):
        if status != "running":
            return
        if self._last_sample and (now - self._last_sample).total_seconds() < self.sc.history_step_s:
            return
        self._last_sample = now
        s = {"t": round(minute, 3), "gen": round(gen, 3), "cons": round(cons, 3),
             "min": corridor["min_kw"], "max": corridor["max_kw"], "active": self.active,
             "per": [round(readings[l].kw if readings[l].on else 0.0, 3) for l in sorted(readings)],
             "soc": {bid: round(b.charge_kwh, 3) for bid, b in self.batteries.items()}}
        self.history.append(s)
        new = not os.path.exists(self.telemetry_path)
        with open(self.telemetry_path, "a", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(self._csv_header())
            w.writerow([now.strftime("%Y-%m-%d %H:%M:%S"), s["t"], s["gen"], s["cons"], s["min"], s["max"],
                        s["active"]] + [s["soc"][b] for b in self.batteries] + s["per"])

    def _load_history(self):
        if not os.path.exists(self.telemetry_path):
            return
        with open(self.telemetry_path, encoding="utf-8") as f:
            header = next(csv.reader(f), None)
        if header != self._csv_header():
            # изменился состав линий/АКБ — старую историю откладываем в сторону
            old = self.telemetry_path + time.strftime(".%Y%m%d-%H%M%S.old")
            os.replace(self.telemetry_path, old)
            log.warning("Состав линий/АКБ изменился, история перенесена в %s", old)
            return
        with open(self.telemetry_path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    self.history.append({
                        "t": float(row["t"]), "gen": float(row["gen_kw"]), "cons": float(row["cons_kw"]),
                        "min": float(row["min_kw"]), "max": float(row["max_kw"]), "active": row["active"],
                        "per": [float(row[k]) for k in row if k.startswith("L") and k.endswith("_kw")],
                        "soc": {k[4:-4]: float(row[k]) for k in row if k.startswith("soc_")},
                    })
                except (KeyError, ValueError):
                    continue
        log.info("История восстановлена: %d точек", len(self.history))

    def _save_state(self):
        data = {
            "active": self.active,
            "fired": sorted(self.fired),
            "stats": self.stats,
            "penalty_until": {k: _iso(v) for k, v in self.penalty_until.items()},
            "batteries": {bid: {k: v for k, v in b.to_dict().items() if k not in ("soc",)}
                          for bid, b in self.batteries.items()},
            "lines": {lid: {"lock_until": _iso(l.lock_until), "lock_reason": l.lock_reason,
                            "trip_until": _iso(l.trip_until), "trip_reason": l.trip_reason,
                            "admin_lock": l.admin_lock, "admin_until": _iso(l.admin_until),
                            "admin_reason": l.admin_reason, "unlock_phase": l.unlock_phase}
                      for lid, l in self.lines.items()},
            "admin": {
                "solar": self.solar_admin,
                "crisis_paused": self.crisis_paused,
                "phase_off": self.phase_off,
                "corridor": {b: ({"value": o["value"], "until": _iso(o["until"])} if o else None)
                             for b, o in self.corr_override.items()},
            },
        }
        for bid, b in self.batteries.items():
            data["batteries"][bid]["fault_until"] = _iso(b.fault_until)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.state_path)

    def _load_state(self):
        if not os.path.exists(self.state_path):
            return
        with open(self.state_path, encoding="utf-8") as f:
            data = json.load(f)
        self.active = data.get("active", self.active)
        self.fired = set(data.get("fired", []))
        self.stats.update(data.get("stats", {}))
        self.penalty_until = {k: _from_iso(v) for k, v in data.get("penalty_until", {}).items()} or self.penalty_until
        for bid, fields in data.get("batteries", {}).items():
            b = self.batteries.get(bid)
            if not b:
                continue
            fields = dict(fields)
            fields["fault_until"] = _from_iso(fields.get("fault_until"))
            fields.pop("id", None)
            fields.pop("voltage_v", None)   # напряжение — всегда из сценария
            for k, v in fields.items():
                if hasattr(b, k):
                    setattr(b, k, v)
        for lid, l in data.get("lines", {}).items():
            ls = self.lines.get(int(lid))
            if ls:
                ls.lock_until, ls.lock_reason = _from_iso(l["lock_until"]), l["lock_reason"]
                ls.trip_until, ls.trip_reason = _from_iso(l["trip_until"]), l["trip_reason"]
                ls.admin_lock = bool(l.get("admin_lock", False))
                ls.admin_until = _from_iso(l.get("admin_until"))
                ls.admin_reason = l.get("admin_reason", "")
                ls.unlock_phase = l.get("unlock_phase")
        a = data.get("admin", {})
        self.solar_admin = float(a.get("solar", 1.0))
        self.crisis_paused = bool(a.get("crisis_paused", False))
        self.phase_off = {int(k): v for k, v in (a.get("phase_off") or {}).items()}
        for b, o in (a.get("corridor") or {}).items():
            if b in self.corr_override and o:
                self.corr_override[b] = {"value": float(o["value"]), "until": _from_iso(o.get("until"))}
        log.info("Состояние восстановлено из %s", self.state_path)
