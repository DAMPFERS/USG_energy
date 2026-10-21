"""
Игровой движок купола. Работает в отдельном потоке, раз в tick_s секунд:
  1. читает показания реле и генерацию;
  2. отправляет сообщения сценария в терминал;
  3. считает баланс через аккумулятор;
  4. отслеживает три игровые ситуации и применяет последствия:
     * перегруз — потребление выше max_kw: предупреждение, через overload_grace_min — блэкаут
       (все линии выключаются, но не блокируются);
     * избыток — генерация − потребление больше max_charge_kw (АКБ не полный): предупреждение,
       через surplus_grace_min АКБ отключается на battery_off_min;
     * дефицит — потребление больше генерации + доступного разряда: через deficit_grace_s линии
       отключаются по приоритету (13 → 1), пока дефицит не исчезнет, и блокируются на deficit_lock_min;
  5. пишет историю (CSV) и снимок состояния (JSON) — после перезапуска игра продолжается
     с текущего реального момента, пропущенный промежуток на графике виден как потеря связи.

Время купола t — минуты от Сола 1 00:00 (см. clock.MarsTime). Все длительности — время купола.
Действия организатора (admin_*) в терминал участников не пишутся — для этого есть команда msg.
"""
import csv
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from .battery import Battery
from .clock import GameClock
from .linemap import LineMap
from .relays import RelayManager
from .scenario import Scenario
from .settings import Settings
from .solar import Forecast
from .terminal import Terminal

log = logging.getLogger("engine")

KINDS = ("overload", "surplus", "deficit")
TITLES = {"overload": "Overload", "surplus": "Battery overcharge", "deficit": "Power deficit"}
ADMIN_LOCK_REASON = "Locked by Mission Control"
SHED_REASON = "Load shedding: power deficit"


@dataclass
class LineState:
    id: int
    trip_until: datetime | None = None     # отключена при дефиците
    admin_lock: bool = False               # блокировка организатором
    admin_until: datetime | None = None    # None — бессрочно
    admin_reason: str = ""
    last_enforce: float = 0.0


@dataclass
class Alarm:
    active: bool = False
    elapsed_s: float = 0.0     # сколько длится нарушение (игровые секунды)
    ok_s: float = 0.0          # сколько нарушения уже нет (для снятия предупреждения)


def _iso(dt):
    return dt.isoformat() if dt else None


def _from_iso(s):
    return datetime.fromisoformat(s) if s else None


class Engine:
    def __init__(self, scenario: Scenario, clock: GameClock, relays: RelayManager, solar,
                 forecast: Forecast, terminal: Terminal, linemap: LineMap, settings: Settings,
                 logs_dir: str, tick_s: float = 1.0):
        self.sc = scenario
        self.mars = scenario.mars
        self.clock = clock
        self.relays = relays
        self.solar = solar
        self.forecast = forecast
        self.term = terminal
        self.map = linemap
        self.settings = settings
        self.tick_s = tick_s
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None

        self.battery = Battery(settings)
        self.lines = {lid: LineState(lid) for lid in relays.lines}
        self.priority = {lid: i + 1 for i, lid in enumerate(sorted(self.lines))}   # 1 — отключается последней
        self.alarms = {k: Alarm() for k in KINDS}
        self.fired: set[str] = set()
        self.stats = {"overload": 0, "blackouts": 0, "surplus": 0, "battery_off": 0, "sheds": 0,
                      "lines_shed": 0, "deficit_kwh": 0.0, "curtailed_kwh": 0.0}
        # переопределения организатора
        self.solar_admin = 1.0
        self.crisis_paused = False

        self.history: list[dict] = []
        self._last_now: datetime | None = None
        self._last_sample: datetime | None = None
        self._last_save = 0.0
        self._restart_checked = False
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

    def name(self, lid: int) -> str:
        return self.map.name(lid)

    def now_t(self) -> float:
        return self.mars.t(self.clock.now())

    @property
    def rules(self) -> dict:
        return self.settings.rules

    def _label(self, dt: datetime | None) -> str | None:
        return self.mars.label(self.mars.t(dt)) if dt else None

    def _hm(self, dt: datetime | None) -> str | None:
        return self.mars.hm(self.mars.t(dt)) if dt else None

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
    def _status(self, t: float) -> str:
        if t < self.sc.start_t:
            return "before"
        if t >= self.sc.end_t:
            return "finished"
        return "running"

    def gap_min(self) -> float:
        """Разрыв между точками истории, начиная с которого считаем, что сервер не работал."""
        return max(3 * self.sc.history_step_s, 3 * self.tick_s * self.clock.speed) / 60.0

    def tick(self):
        now = self.clock.now()
        t = self.mars.t(now)
        to_off: list[int] = []
        with self._lock:
            dt_s = 0.0 if self._last_now is None else (now - self._last_now).total_seconds()
            dt_s = min(max(dt_s, 0.0), 120.0 * self.clock.speed)
            self._last_now = now
            status = self._status(t)
            if status == "running":    # время до начала игры не считаем
                dt_s = min(dt_s, (t - self.sc.start_t) * 60.0)
            if not self._restart_checked:
                self._restart_checked = True
                self._report_outage(t, status)

            self._run_messages(t, status)
            self.solar.set_factor(self.solar_admin)
            self._expire(now)

            readings = self.relays.readings()
            gen = self.solar.power_kw()
            cons = sum(r.kw for r in readings.values() if r.on and r.online)
            dt_h = dt_s / 3600.0

            deficit = 0.0
            if status == "running":
                deficit, curtailed = self.battery.step(gen - cons, dt_h, now)
                self.stats["curtailed_kwh"] += curtailed * dt_h
                self.stats["deficit_kwh"] += deficit * dt_h
                if not self.crisis_paused:
                    to_off += self._check(now, dt_s, gen, cons, deficit, readings)
            else:
                self.battery.power_kw = 0.0

            self._gen, self._cons, self._deficit = gen, cons, deficit

            # всё, что заблокировано, но физически включено — выключаем
            for lid, r in readings.items():
                if r.on and self._line_block(lid, now) and time.time() - self.lines[lid].last_enforce > 3:
                    self.lines[lid].last_enforce = time.time()
                    to_off.append(lid)

            self._sample_history(now, t, gen, cons, readings, status)
            self._snap = self._build_snapshot(now, t, status, readings)
            if time.time() - self._last_save > 5:
                self._save_state()
                self._last_save = time.time()

        for lid in dict.fromkeys(to_off):
            self.relays.set(lid, False)
        if to_off:
            self._refresh()

    def _report_outage(self, t: float, status: str):
        if status != "running" or not self.history:
            return
        last = self.history[-1]["t"]
        if t - last > self.gap_min():
            self.term.post(f"Link with the dome restored. No telemetry from {self.mars.label(last)} "
                           f"to {self.mars.label(t)}.", "WARN", self.clock.now())
            self.actions.info("RESTART gap %.1f min", t - last)

    # ------------------------------------------------------------------ сценарий
    def _run_messages(self, t: float, status: str):
        if status == "before":
            return
        for m in self.sc.messages:
            if m.t > t:
                break
            if m.key in self.fired:
                continue
            self.fired.add(m.key)
            self.term.post(m.text, m.level, self.mars.dt(m.t))

    def _expire(self, now):
        freed = []
        for lid, ls in self.lines.items():
            if ls.trip_until and now >= ls.trip_until:
                ls.trip_until = None
                freed.append(self.name(lid))
            if ls.admin_lock and ls.admin_until and now >= ls.admin_until:
                ls.admin_lock, ls.admin_until, ls.admin_reason = False, None, ""
                self.actions.info("ADMIN LOCK EXPIRED line=%d", lid)
        if freed:
            self.term.post(f"Lock lifted, the line{'s' if len(freed) > 1 else ''} can be switched on: "
                           f"{', '.join(freed)}.", "OK", now)
        b = self.battery
        if b.off_until and now >= b.off_until:
            b.off_until = None
            self.term.post(f"{b.name} reconnected to the grid.", "OK", now)

    def _line_block(self, lid, now) -> tuple[str, str, datetime | None] | None:
        """Почему линию нельзя включать: (тип, причина, до какого времени) или None."""
        ls = self.lines[lid]
        if ls.admin_lock and (ls.admin_until is None or now < ls.admin_until):
            return "locked", ls.admin_reason or ADMIN_LOCK_REASON, ls.admin_until
        if ls.trip_until and now < ls.trip_until:
            return "tripped", SHED_REASON, ls.trip_until
        return None

    # ------------------------------------------------------------------ игровые ситуации
    def min_kw(self, gen: float, now: datetime) -> float | None:
        """Нижняя граница потребления: генерация − макс. мощность заряда.
        None — не действует (АКБ отключён или полный: излишек просто теряется)."""
        b = self.battery
        if not b.connected(now) or b.full:
            return None
        return max(0.0, gen - b.max_charge_kw)

    def _grace_s(self, kind: str) -> float:
        r = self.rules
        return {"overload": r["overload_grace_min"] * 60, "surplus": r["surplus_grace_min"] * 60,
                "deficit": r["deficit_grace_s"]}[kind]

    def _check(self, now, dt_s, gen, cons, deficit, readings) -> list[int]:
        r = self.rules
        lo = self.min_kw(gen, now)
        cond = {
            "overload": cons > r["max_kw"],
            "surplus": lo is not None and cons < lo - 1e-6,
            "deficit": deficit > 0.01,
        }
        to_off = []
        for kind, bad in cond.items():
            a = self.alarms[kind]
            if not bad:
                if a.active:
                    a.ok_s += dt_s
                    if a.ok_s >= r["clear_s"] or kind == "deficit":
                        if kind != "deficit":
                            self.term.post(f"{TITLES[kind]} cleared: parameters are back to normal.", "OK", now)
                        self.alarms[kind] = Alarm()
                continue
            if not a.active:
                a.active, a.elapsed_s = True, 0.0
                self._warn(kind, now, gen, cons)
            a.ok_s = 0.0
            a.elapsed_s += dt_s
            if a.elapsed_s >= self._grace_s(kind):
                to_off += self._consequence(kind, now, gen, cons, deficit, readings)
                self.alarms[kind] = Alarm()
        return to_off

    def _warn(self, kind, now, gen, cons):
        r, b = self.rules, self.battery
        if kind == "overload":
            self.stats["overload"] += 1
            self.term.post(f"OVERLOAD: consumption {cons:.2f} kW is above the {r['max_kw']:.2f} kW limit. "
                           f"Reduce the load, otherwise BLACKOUT in {r['overload_grace_min']:g} min.", "WARN", now)
        elif kind == "surplus":
            self.stats["surplus"] += 1
            self.term.post(f"BATTERY OVERCHARGE: generation surplus {gen - cons:.2f} kW exceeds the "
                           f"{b.max_charge_kw:.2f} kW charge limit. Increase consumption, otherwise the battery "
                           f"disconnects in {r['surplus_grace_min']:g} min for {r['battery_off_min']:g} min.",
                           "WARN", now)

    def _consequence(self, kind, now, gen, cons, deficit, readings) -> list[int]:
        r, b = self.rules, self.battery
        if kind == "overload":
            ids = [lid for lid, x in readings.items() if x.on]
            self.stats["blackouts"] += 1
            self.term.post(f"BLACKOUT: consumption stayed above {r['max_kw']:.2f} kW for "
                           f"{r['overload_grace_min']:g} min. All lines have been de-energized. "
                           f"You may switch them back on.", "CRIT", now)
            self.actions.info("BLACKOUT lines=%s", ids)
            return ids
        if kind == "surplus":
            b.off_until = now + timedelta(minutes=r["battery_off_min"])
            self.stats["battery_off"] += 1
            self.term.post(f"{b.name} DISCONNECTED until {self._label(b.off_until)}: the charge limit was exceeded "
                           f"for {r['surplus_grace_min']:g} min. Without storage, consumption must not exceed "
                           f"generation.", "CRIT", now)
            self.actions.info("BATTERY OFF until=%s", b.off_until)
            return []
        # дефицит: отключаем по приоритету, начиная с наименее важных (13 → 1)
        on = sorted((lid for lid, x in readings.items() if x.on and x.online), key=lambda l: -self.priority[l])
        ids, removed = [], 0.0
        for lid in on:
            if removed >= deficit:
                break
            ids.append(lid)
            removed += readings[lid].kw
        if not ids:
            return []
        until = now + timedelta(minutes=r["deficit_lock_min"])
        for lid in ids:
            self.lines[lid].trip_until = until
        self.stats["sheds"] += 1
        self.stats["lines_shed"] += len(ids)
        names = ", ".join(f"{self.name(l)} (#{self.priority[l]})" for l in ids)
        self.term.post(f"POWER DEFICIT {deficit:.2f} kW: generation and battery cannot cover the load. "
                       f"Load shedding by priority: {names}. Locked until {self._label(until)}.", "CRIT", now)
        self.actions.info("SHED deficit=%.2f lines=%s until=%s", deficit, ids, until)
        return ids

    # ------------------------------------------------------------------ действия участников
    def set_line(self, lid: int, on: bool, who: str = "LINE") -> tuple[bool, str]:
        now = self.clock.now()
        ru = who.startswith("ADMIN")
        with self._lock:
            if lid not in self.lines:
                return False, "Нет такой линии" if ru else "No such line"
            block = self._line_block(lid, now)
            if on and block:
                kind, reason, until = block
                if ru:
                    what = "отключена при дефиците" if kind == "tripped" else "заблокирована"
                    return False, f"{what}" + (f" (до {self._label(until)})" if until else "") + ". Сначала unlock"
                return False, f"{reason} (until {self._hm(until)})" if until else reason
        ok = self.relays.set(lid, on)
        self.actions.info("%s %d %s -> %s", who, lid, "ON" if on else "OFF", "ok" if ok else "FAIL")
        if not ok:
            return False, "Реле не ответило" if ru else "Relay did not respond"
        self._refresh()
        return True, ""

    def set_priority(self, lid: int, prio: int, who: str = "PRIORITY") -> tuple[bool, str]:
        """Поставить линии приоритет prio; линия, у которой он был, получает старый приоритет."""
        ru = who.startswith("ADMIN")
        with self._lock:
            if lid not in self.lines:
                return False, "Нет такой линии" if ru else "No such line"
            if not 1 <= prio <= len(self.lines):
                return False, (f"Приоритет — от 1 до {len(self.lines)}" if ru
                               else f"Priority must be 1–{len(self.lines)}")
            old = self.priority[lid]
            other = next(l for l, p in self.priority.items() if p == prio)
            self.priority[other], self.priority[lid] = old, prio
            self.actions.info("%s line=%d %d->%d (line %d -> %d)", who, lid, old, prio, other, old)
            self._save_state()
        self._refresh()
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
            t = self.mars.t(now)
            self._snap = self._build_snapshot(now, t, self._status(t), readings)

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
                ls.trip_until = None
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

    def admin_settings(self, section: str, values: dict) -> tuple[bool, str]:
        """Параметры АКБ / правила: действуют сразу и записываются в server.yaml."""
        with self._lock:
            ok, msg = self.settings.update(section, values)
            if ok:
                b = self.battery
                b.charge_kwh = min(b.charge_kwh, b.capacity_kwh)
                self.actions.info("ADMIN SETTINGS %s %s", section, values)
        self._refresh()
        return ok, msg

    def admin_battery(self, charge_kwh: float | None = None, soc: float | None = None,
                      connect: bool | None = None, minutes: float | None = None) -> tuple[bool, str]:
        """Состояние АКБ: заряд (кВт·ч или %), подключить / отключить (на N минут или до connect)."""
        now = self.clock.now()
        with self._lock:
            b = self.battery
            if soc is not None:
                charge_kwh = b.capacity_kwh * soc / 100.0
            if charge_kwh is not None:
                if charge_kwh < 0:
                    return False, "Заряд не может быть отрицательным"
                b.charge_kwh = min(charge_kwh, b.capacity_kwh)
            if connect is True:
                b.off_until = None
            elif connect is False:
                # «бессрочно» — до конца игры
                b.off_until = now + timedelta(minutes=minutes) if minutes else self.mars.dt(self.sc.end_t)
            self.actions.info("ADMIN BATTERY charge=%s soc=%s connect=%s minutes=%s", charge_kwh, soc, connect, minutes)
        self._refresh()
        return True, ""

    def admin_solar(self, factor: float) -> tuple[bool, str]:
        if not 0.0 <= factor <= 2.0:
            return False, "Коэффициент генерации — от 0.0 до 2.0"
        with self._lock:
            self.solar_admin = float(factor)
            self.solar.set_factor(self.solar_admin)
            self.actions.info("ADMIN SOLAR factor=%.2f", factor)
        self._refresh()
        return True, ""

    def admin_crisis(self, action: str) -> tuple[bool, str]:
        """pause — заморозить отслеживание нарушений, resume, cancel — сбросить отсчёты,
        снять блокировки после дефицита и подключить АКБ."""
        with self._lock:
            if action == "pause":
                self.crisis_paused = True
            elif action == "resume":
                self.crisis_paused = False
            elif action == "cancel":
                self.alarms = {k: Alarm() for k in KINDS}
                for ls in self.lines.values():
                    ls.trip_until = None
                self.battery.off_until = None
            else:
                return False, "Допустимо: pause | resume | cancel"
            self.actions.info("ADMIN CRISIS %s", action)
        self._refresh()
        return True, ""

    def admin_time(self, s: str) -> tuple[bool, str]:
        try:
            t = self.mars.parse(s)
            self.clock.set_time(self.mars.dt(t))
        except (RuntimeError, ValueError) as e:
            return False, str(e)
        with self._lock:
            self._last_now = self._last_sample = None
            # перемотка назад: «будущее» стираем из истории, сообщения сценария придут снова
            if self.history and self.history[-1]["t"] > t:
                self.history = [h for h in self.history if h["t"] <= t]
                self._rewrite_history()
            self.fired = {k for k in self.fired if not any(m.key == k and m.t > t for m in self.sc.messages)}
            self.actions.info("ADMIN TIME %s", s)
        self.tick()
        return True, ""

    def admin_view(self) -> dict:
        now = self.clock.now()
        with self._lock:
            return {
                "settings": self.settings.view(),
                "solar_admin": self.solar_admin,
                "crisis": {
                    "paused": self.crisis_paused,
                    "alarms": {k: {"active": a.active, "elapsed_s": round(a.elapsed_s, 1),
                                   "grace_s": self._grace_s(k)} for k, a in self.alarms.items()},
                },
                "lines_map": self.map.view(),
                "clock": {"mode": self.clock.mode, "speed": self.clock.speed,
                          "earth": now.strftime("%Y-%m-%d %H:%M:%S")},
                "game": {"start": self.mars.label(self.sc.start_t), "end": self.mars.label(self.sc.end_t),
                         "start_earth": self.mars.dt(self.sc.start_t).strftime("%Y-%m-%d %H:%M"),
                         "end_earth": self.mars.dt(self.sc.end_t).strftime("%Y-%m-%d %H:%M")},
            }

    # ------------------------------------------------------------------ данные для API
    def _lines_view(self, now, readings):
        out = []
        for lid in self.lines:
            r = readings[lid]
            block = self._line_block(lid, now)
            if not r.online:
                st = "offline"
            elif block:
                st = block[0]
            else:
                st = "on" if r.on else "off"
            out.append({"id": lid, "name": self.name(lid), "key": self.map.key(lid),
                        "on": r.on, "kw": round(r.kw if r.on else 0.0, 3),
                        "online": r.online, "status": st, "priority": self.priority[lid],
                        "reason": block[1] if block else "",
                        "until": self._hm(block[2]) if block else None,
                        "until_t": round(self.mars.t(block[2]), 3) if block and block[2] else None})
        return out

    def _build_snapshot(self, now, t, status, readings) -> dict:
        b, r = self.battery, self.rules
        lo = self.min_kw(self._gen, now)
        alarms = []
        if not self.crisis_paused:
            for k, a in self.alarms.items():
                if a.active:
                    grace = self._grace_s(k)
                    alarms.append({"kind": k, "title": TITLES[k], "grace_s": grace,
                                   "left_s": round(max(0.0, grace - a.elapsed_s) / self.clock.speed, 1)})
        return {
            "t": round(t, 3), "sol": self.mars.sol(t), "time": self.mars.hm(t, seconds=True),
            "sol_min": self.mars.sol_min, "start_t": self.sc.start_t, "end_t": self.sc.end_t,
            "name": self.sc.name, "status": status, "speed": self.clock.speed,
            "gen": round(self._gen, 3), "cons": round(self._cons, 3), "net": round(self._gen - self._cons, 3),
            "deficit": round(self._deficit, 3),
            "limits": {"max": r["max_kw"], "min": None if lo is None else round(lo, 3),
                       "max_charge": b.max_charge_kw, "max_discharge": b.max_discharge_kw},
            "lines": self._lines_view(now, readings),
            "battery": {"name": b.name, "capacity_kwh": b.capacity_kwh, "charge_kwh": round(b.charge_kwh, 3),
                        "soc": round(b.soc, 4), "power_kw": round(b.power_kw, 3), "full": b.full,
                        "max_charge_kw": b.max_charge_kw, "max_discharge_kw": b.max_discharge_kw,
                        "connected": b.connected(now), "off_until": self._hm(b.off_until)
                        if not b.connected(now) else None},
            "alarms": alarms,
            "paused": self.crisis_paused,
            "stats": {k: (round(v, 3) if isinstance(v, float) else v) for k, v in self.stats.items()},
        }

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._snap)

    def history_view(self, since: float | None = None) -> list[dict]:
        with self._lock:
            if since is None:
                return list(self.history)
            return [h for h in self.history if h["t"] > since]

    def gaps(self) -> list[list[float]]:
        """Промежутки, когда сервер не работал: [[t_от, t_до], ...]."""
        thr = self.gap_min()
        with self._lock:
            h = self.history
            return [[h[i - 1]["t"], h[i]["t"]] for i in range(1, len(h)) if h[i]["t"] - h[i - 1]["t"] > thr]

    def forecast_view(self) -> dict:
        return self.forecast.as_dict(self.sc.start_t, self.sc.end_t)

    # ------------------------------------------------------------------ история / сохранение
    def _csv_header(self) -> list[str]:
        return (["time", "t", "gen_kw", "cons_kw", "max_kw", "min_kw", "soc_kwh", "bat_kw", "bat_off"]
                + [f"L{l}_kw" for l in sorted(self.lines)])

    def _sample_history(self, now, t, gen, cons, readings, status):
        if status != "running":
            return
        if self._last_sample and 0 <= (now - self._last_sample).total_seconds() < self.sc.history_step_s:
            return
        self._last_sample = now
        b = self.battery
        lo = self.min_kw(gen, now)
        s = {"t": round(t, 3), "gen": round(gen, 3), "cons": round(cons, 3),
             "max": self.rules["max_kw"], "min": None if lo is None else round(lo, 3),
             "soc": round(b.charge_kwh, 3), "bat": round(b.power_kw, 3), "boff": not b.connected(now),
             "per": [round(readings[l].kw if readings[l].on else 0.0, 3) for l in sorted(readings)]}
        self.history.append(s)
        new = not os.path.exists(self.telemetry_path)
        with open(self.telemetry_path, "a", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(self._csv_header())
            w.writerow([now.strftime("%Y-%m-%d %H:%M:%S"), s["t"], s["gen"], s["cons"], s["max"],
                        "" if s["min"] is None else s["min"], s["soc"], s["bat"], int(s["boff"])] + s["per"])

    def _rewrite_history(self):
        with open(self.telemetry_path, encoding="utf-8") as f:
            rows = list(csv.reader(f))
        keep = [rows[0]] + [r for r in rows[1:] if float(r[1]) <= (self.history[-1]["t"] if self.history else -1)]
        with open(self.telemetry_path, "w", encoding="utf-8", newline="") as f:
            csv.writer(f).writerows(keep)

    def _load_history(self):
        if not os.path.exists(self.telemetry_path):
            return
        with open(self.telemetry_path, encoding="utf-8") as f:
            header = next(csv.reader(f), None)
        if header != self._csv_header():
            # изменился состав линий или формат — старую историю откладываем в сторону
            old = self.telemetry_path + time.strftime(".%Y%m%d-%H%M%S.old")
            os.replace(self.telemetry_path, old)
            log.warning("Формат истории изменился, история перенесена в %s", old)
            return
        with open(self.telemetry_path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    self.history.append({
                        "t": float(row["t"]), "gen": float(row["gen_kw"]), "cons": float(row["cons_kw"]),
                        "max": float(row["max_kw"]), "min": float(row["min_kw"]) if row["min_kw"] else None,
                        "soc": float(row["soc_kwh"]), "bat": float(row["bat_kw"]), "boff": row["bat_off"] == "1",
                        "per": [float(row[k]) for k in row if k.startswith("L") and k.endswith("_kw")],
                    })
                except (KeyError, ValueError):
                    continue
        log.info("История восстановлена: %d точек", len(self.history))

    def _save_state(self):
        data = {
            "fired": sorted(self.fired),
            "stats": self.stats,
            "battery": {"charge_kwh": self.battery.charge_kwh, "off_until": _iso(self.battery.off_until)},
            "priority": {str(l): p for l, p in self.priority.items()},
            "alarms": {k: {"active": a.active, "elapsed_s": a.elapsed_s} for k, a in self.alarms.items()},
            "lines": {lid: {"trip_until": _iso(l.trip_until), "admin_lock": l.admin_lock,
                            "admin_until": _iso(l.admin_until), "admin_reason": l.admin_reason}
                      for lid, l in self.lines.items()},
            "admin": {"solar": self.solar_admin, "crisis_paused": self.crisis_paused},
        }
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.state_path)

    def _load_state(self):
        if not os.path.exists(self.state_path):
            return
        with open(self.state_path, encoding="utf-8") as f:
            data = json.load(f)
        self.fired = set(data.get("fired", []))
        self.stats.update({k: v for k, v in data.get("stats", {}).items() if k in self.stats})
        b = data.get("battery") or {}
        if "charge_kwh" in b:
            self.battery.charge_kwh = min(float(b["charge_kwh"]), self.battery.capacity_kwh)
        self.battery.off_until = _from_iso(b.get("off_until"))
        prio = {int(l): int(p) for l, p in (data.get("priority") or {}).items() if int(l) in self.lines}
        if sorted(prio) == sorted(self.lines) and sorted(prio.values()) == list(range(1, len(self.lines) + 1)):
            self.priority = prio
        for k, a in (data.get("alarms") or {}).items():
            if k in self.alarms:
                self.alarms[k] = Alarm(bool(a.get("active")), float(a.get("elapsed_s", 0.0)))
        for lid, l in data.get("lines", {}).items():
            ls = self.lines.get(int(lid))
            if ls:
                ls.trip_until = _from_iso(l.get("trip_until"))
                ls.admin_lock = bool(l.get("admin_lock", False))
                ls.admin_until = _from_iso(l.get("admin_until"))
                ls.admin_reason = l.get("admin_reason", "")
        a = data.get("admin", {})
        self.solar_admin = float(a.get("solar", 1.0))
        self.crisis_paused = bool(a.get("crisis_paused", False))
        log.info("Состояние восстановлено из %s", self.state_path)
