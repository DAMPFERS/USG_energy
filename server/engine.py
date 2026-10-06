"""
Игровой движок купола. Работает в отдельном потоке, раз в tick_s секунд:
  1. читает показания реле и генерацию;
  2. применяет сценарий: фазы (коридор, блокировки линий, коэффициент солнца), события;
  3. считает баланс через активный АКБ;
  4. отслеживает выход из коридора / дефицит и применяет последствия (отключение линий);
  5. пишет историю (CSV) и снимок состояния (JSON) — после перезапуска игра продолжается.
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
from .relays import RelayManager
from .scenario import Scenario
from .solar import Forecast
from .terminal import Terminal

log = logging.getLogger("engine")

KIND_TITLES = {"overload": "Перегруз", "underload": "Гибернация", "deficit": "Дефицит мощности"}
KIND_WARN = {
    "overload": "ВНИМАНИЕ: потребление {cons:.2f} кВт выше допустимых {max:.2f} кВт. "
                "Срабатывание защиты через {grace:.0f} с.",
    "underload": "ВНИМАНИЕ: потребление {cons:.2f} кВт ниже порога {min:.2f} кВт. "
                 "Переход в гибернацию через {grace:.0f} с.",
    "deficit": "ВНИМАНИЕ: генерации и АКБ не хватает на {deficit:.2f} кВт. "
               "Аварийное отключение через {grace:.0f} с.",
}


@dataclass
class LineState:
    id: int
    name: str
    lock_until: datetime | None = None     # блокировка событием сценария
    lock_reason: str = ""
    trip_until: datetime | None = None     # аварийное отключение (последствие)
    trip_reason: str = ""
    last_enforce: float = 0.0


def _iso(dt):
    return dt.isoformat() if dt else None


def _from_iso(s):
    return datetime.fromisoformat(s) if s else None


class Engine:
    def __init__(self, scenario: Scenario, clock: GameClock, relays: RelayManager, solar,
                 forecast: Forecast, terminal: Terminal, logs_dir: str, tick_s: float = 1.0):
        self.sc = scenario
        self.clock = clock
        self.relays = relays
        self.solar = solar
        self.forecast = forecast
        self.term = terminal
        self.tick_s = tick_s
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None

        self.batteries: dict[str, Battery] = {b["id"]: Battery(**b) for b in scenario.batteries}
        self.active: str | None = scenario.active_battery
        self.lines = {lid: LineState(lid, l.name) for lid, l in relays.lines.items()}
        self.fired: set[str] = set()
        self.stats = {"overload": 0, "underload": 0, "deficit": 0, "out_of_corridor_s": 0.0,
                      "deficit_s": 0.0, "curtailed_kwh": 0.0, "deficit_kwh": 0.0}
        self.timers = {k: 0.0 for k in KIND_TITLES}
        self.warned = {k: False for k in KIND_TITLES}
        self.penalty_until: dict[str, datetime | None] = {k: None for k in KIND_TITLES}
        self.history: list[dict] = []
        self._last_now: datetime | None = None
        self._last_sample: datetime | None = None
        self._last_save = 0.0
        self._snap: dict = {}
        self._gen = self._cons = self._deficit = 0.0

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

    def tick(self):
        now = self.clock.now()
        minute = dt_to_min(now)
        to_off: list[int] = []
        with self._lock:
            dt_s = 0.0 if self._last_now is None else (now - self._last_now).total_seconds()
            dt_s = min(max(dt_s, 0.0), 120.0 * self.clock.speed)
            self._last_now = now
            status = self._game_status(now)
            phase = self.sc.phase_at(minute) if status == "running" else None

            self._run_scenario(now, minute, status, phase)
            self.solar.set_factor(phase.solar_factor if phase else self.sc.defaults["solar_factor"])
            self._expire_locks(now)

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
                            self.term.post(f"{b.name}: перегрузка по мощности разряда, сработала защита АКБ. "
                                           f"Восстановление в {b.fault_until:%H:%M}. Выберите другой накопитель!",
                                           "CRIT", now)
                    else:
                        b.idle(dt_h, now)
                if self.active is None:
                    deficit, curtailed = max(0.0, cons - gen), max(0.0, gen - cons)
                self.stats["curtailed_kwh"] += curtailed * dt_h
                self.stats["deficit_kwh"] += deficit * dt_h
                corridor = phase.corridor if phase else self.sc.defaults["corridor"]
                penalties = phase.penalties if phase else self.sc.defaults["penalties"]
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

            self._sample_history(now, minute, gen, cons, readings, status)
            self._snap = self._build_snapshot(now, minute, status, phase, readings)
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
                names = ", ".join(self.lines[l].name for l in phase.locked_lines if l in self.lines)
                self.term.post(f"Заблокированы по распоряжению штаба: {names}.", "WARN", now)
        if status == "finished" and "end" not in self.fired:
            self.fired.add("end")
            self.term.post("Игровой день завершён. Спасибо за смену, диспетчер!", "OK", now)
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
                reason = e.lock_lines.get("reason", "блокировка по сценарию")
                for lid in e.lock_lines.get("lines", []):
                    ls = self.lines.get(int(lid))
                    if ls:
                        ls.lock_until, ls.lock_reason = until, reason

    def _expire_locks(self, now):
        for ls in self.lines.values():
            if ls.lock_until and now >= ls.lock_until:
                ls.lock_until, ls.lock_reason = None, ""
                self.term.post(f"{ls.name}: блокировка снята, линию можно включать.", "OK", now)
            if ls.trip_until and now >= ls.trip_until:
                ls.trip_until, ls.trip_reason = None, ""
                self.term.post(f"{ls.name}: аварийная блокировка снята, линию можно включать.", "OK", now)

    def _line_block(self, lid, now, phase) -> tuple[str, str, datetime | None] | None:
        """Почему линию нельзя включать: (тип, причина, до какого времени) или None."""
        ls = self.lines[lid]
        if ls.trip_until and now < ls.trip_until:
            return "tripped", ls.trip_reason, ls.trip_until
        if ls.lock_until and now < ls.lock_until:
            return "locked", ls.lock_reason, ls.lock_until
        if phase and lid in phase.locked_lines:
            end = datetime.combine(now.date(), datetime.min.time()) + timedelta(minutes=phase.end)
            return "locked", phase.locked_lines[lid], end
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
                    self.term.post(f"{KIND_TITLES[kind]}: параметры вернулись в норму.", "OK", now)
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
            return [int(x) for x in trip]
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
        reason = f"{KIND_TITLES[kind]}: аварийное отключение"
        for lid in ids:
            ls = self.lines[lid]
            ls.trip_until = max(ls.trip_until or until, until)
            ls.trip_reason = reason
        self.penalty_until[kind] = until
        self.stats[kind] += 1
        names = ", ".join(self.lines[l].name for l in ids)
        tail = f" Отключены: {names}. Блокировка до {until:%H:%M}." if ids else f" Режим действует до {until:%H:%M}."
        self.term.post(spec.get("message", reason) + tail, spec.get("level", "CRIT"), now)
        self.actions.info("PENALTY %s lines=%s until=%s", kind, ids, until)
        return ids

    # ------------------------------------------------------------------ действия участников
    def set_line(self, lid: int, on: bool) -> tuple[bool, str]:
        now = self.clock.now()
        with self._lock:
            if lid not in self.lines:
                return False, "Нет такой линии"
            phase = self.sc.phase_at(dt_to_min(now)) if self._game_status(now) == "running" else None
            block = self._line_block(lid, now, phase)
            if on and block:
                kind, reason, until = block
                return False, f"{reason} (до {until:%H:%M})" if until else reason
        ok = self.relays.set(lid, on)
        self.actions.info("LINE %d %s -> %s", lid, "ON" if on else "OFF", "ok" if ok else "FAIL")
        if not ok:
            return False, "Реле не ответило"
        self._refresh_lines()
        return True, ""

    def set_active_battery(self, bid: str) -> tuple[bool, str]:
        now = self.clock.now()
        with self._lock:
            b = self.batteries.get(bid)
            if not b:
                return False, "Нет такого накопителя"
            if not b.working:
                return False, f"{b.name} недоступен ({'защита' if b.status == 'fault' else 'отказ'})"
            if self.active != bid:
                self.active = bid
                self.term.post(f"Активный накопитель: {b.name}.", "INFO", now)
                self.actions.info("BATTERY active=%s", bid)
            self._snap["active_battery"] = bid
            return True, ""

    def admin_message(self, text: str, level: str = "INFO"):
        self.term.post(text, level, self.clock.now())

    def _refresh_lines(self):
        readings = self.relays.readings()
        now = self.clock.now()
        with self._lock:
            if not self._snap:
                return
            phase = self.sc.phase_at(dt_to_min(now)) if self._game_status(now) == "running" else None
            self._snap["lines"] = self._lines_view(now, phase, readings)

    # ------------------------------------------------------------------ данные для API
    def _lines_view(self, now, phase, readings):
        out = []
        for lid, ls in self.lines.items():
            r = readings[lid]
            block = self._line_block(lid, now, phase)
            if not r.online:
                st = "offline"
            elif block:
                st = block[0]
            else:
                st = "on" if r.on else "off"
            out.append({"id": lid, "name": ls.name, "on": r.on, "kw": round(r.kw if r.on else 0.0, 3),
                        "online": r.online, "status": st,
                        "reason": block[1] if block else "",
                        "until": block[2].strftime("%H:%M") if block and block[2] else None})
        return out

    def _build_snapshot(self, now, minute, status, phase, readings) -> dict:
        corridor = phase.corridor if phase else self.sc.defaults["corridor"]
        pen = phase.penalties if phase else self.sc.defaults["penalties"]
        alarms = []
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
    def _sample_history(self, now, minute, gen, cons, readings, status):
        if status != "running":
            return
        if self._last_sample and (now - self._last_sample).total_seconds() < self.sc.history_step_s:
            return
        self._last_sample = now
        corridor = self.sc.corridor_at(minute)
        s = {"t": round(minute, 3), "gen": round(gen, 3), "cons": round(cons, 3),
             "min": corridor["min_kw"], "max": corridor["max_kw"], "active": self.active,
             "per": [round(readings[l].kw if readings[l].on else 0.0, 3) for l in sorted(readings)],
             "soc": {bid: round(b.charge_kwh, 3) for bid, b in self.batteries.items()}}
        self.history.append(s)
        new = not os.path.exists(self.telemetry_path)
        with open(self.telemetry_path, "a", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            bids = list(self.batteries)
            if new:
                w.writerow(["time", "t", "gen_kw", "cons_kw", "min_kw", "max_kw", "active"]
                           + [f"soc_{b}_kwh" for b in bids] + [f"L{l}_kw" for l in sorted(readings)])
            w.writerow([now.strftime("%Y-%m-%d %H:%M:%S"), s["t"], s["gen"], s["cons"], s["min"], s["max"],
                        s["active"]] + [s["soc"][b] for b in bids] + s["per"])

    def _load_history(self):
        if not os.path.exists(self.telemetry_path):
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
                            "trip_until": _iso(l.trip_until), "trip_reason": l.trip_reason}
                      for lid, l in self.lines.items()},
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
            for k, v in fields.items():
                if hasattr(b, k):
                    setattr(b, k, v)
        for lid, l in data.get("lines", {}).items():
            ls = self.lines.get(int(lid))
            if ls:
                ls.lock_until, ls.lock_reason = _from_iso(l["lock_until"]), l["lock_reason"]
                ls.trip_until, ls.trip_reason = _from_iso(l["trip_until"]), l["trip_reason"]
        log.info("Состояние восстановлено из %s", self.state_path)
