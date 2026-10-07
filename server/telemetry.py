"""
Телеметрия для ЦУП: данные энергосистемы в физических единицах (Вт, А, А·ч).

GET /api/telemetry                       — текущий срез
GET /api/telemetry/forecast              — прогноз солнечной генерации W(t)
GET /api/telemetry/history?since=<t>     — история дня (для графиков)
WS  /ws/telemetry                        — поток: hello, forecast, telemetry (каждые push_interval_s), message

Мощности — игровые (кВт купола × 1000), т.е. те же, что видят участники.
Ток и ёмкость АКБ считаются через номинальное напряжение АКБ (voltage_v в сценарии
или telemetry.battery_voltage_v в server.yaml): I = P / U, А·ч = Вт·ч / U.

Если в server.yaml задан telemetry.token — его нужно передавать в заголовке
X-Telemetry-Token или параметром ?token=.
"""
import asyncio
import json
import logging
import time
from datetime import datetime

from fastapi import FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from .clock import hhmm_to_min, dt_to_min
from .engine import Engine

log = logging.getLogger("telemetry")

SCHEMA = 1


def _w(kw) -> float:
    return round(float(kw) * 1000.0, 1)


def _hhmm(minute: float) -> str:
    m = int(round(minute))
    return f"{m // 60:02d}:{m % 60:02d}"


class Telemetry:
    def __init__(self, engine: Engine, cfg: dict):
        self.engine = engine
        self.token = cfg.get("token") or None
        self.push_interval = max(0.2, float(cfg.get("push_interval_s", 1.0)))
        self.voltage = float(cfg.get("battery_voltage_v", 48.0))
        self.forecast_step = float(cfg.get("forecast_step_min", 15))
        self.cors_origins = cfg.get("cors_origins", ["*"])
        self._seq = 0

    def check(self, token: str | None):
        if self.token and token != self.token:
            raise HTTPException(403, "Неверный токен телеметрии")

    def _volt(self, b) -> float:
        return float(b.voltage_v or self.voltage)

    # ------------------------------------------------------------------ полезная нагрузка
    def meta(self) -> dict:
        e = self.engine
        return {
            "schema": SCHEMA,
            "day": e.sc.day, "scenario": e.sc.id,
            "day_start": _hhmm(e.sc.start), "day_end": _hhmm(e.sc.end),
            "clock": {"mode": e.clock.mode, "speed": e.clock.speed},
            "push_interval_s": self.push_interval,
            "lines": [{"id": lid, "name": ls.name} for lid, ls in e.lines.items()],
            "sources": [{"id": "solar", "name": "Солнечные панели", "type": "solar"}],
            "batteries": [{"id": b.id, "name": b.name, "voltage_v": self._volt(b),
                           "nominal_ah": round(b.nominal_kwh * 1000 / self._volt(b), 2)}
                          for b in e.batteries.values()],
        }

    def snapshot(self) -> dict | None:
        e = self.engine
        s = e.snapshot()
        if not s:
            return None
        readings = e.relays.readings()
        now = e.clock.now()
        fc_kw, fc_sd = e.forecast.at(dt_to_min(now))
        self._seq += 1

        lines = []
        for l in s["lines"]:
            r = readings.get(l["id"])
            lines.append({
                "id": l["id"], "name": l["name"], "on": l["on"], "online": l["online"],
                "status": l["status"], "power_w": _w(l["kw"]),
                "reason": l["reason"] or None, "until": l["until"],
                "measured": {"power_w": round(r.raw_w, 1), "voltage_v": round(r.voltage, 1),
                             "current_a": round(r.current, 3)} if r and r.online else None,
            })

        bats = []
        for b in e.batteries.values():
            u = self._volt(b)
            p_w = b.power_kw * 1000.0
            bats.append({
                "id": b.id, "name": b.name, "active": b.id == s["active_battery"], "status": b.status,
                "voltage_v": u,
                "capacity_ah": round(b.capacity_kwh * 1000 / u, 2),
                "nominal_ah": round(b.nominal_kwh * 1000 / u, 2),
                "available_ah": round(b.charge_kwh * 1000 / u, 2),
                "soc_pct": round(100 * b.charge_kwh / b.capacity_kwh, 1) if b.capacity_kwh else 0.0,
                "current_a": round(p_w / u, 2),
                "power_w": round(p_w, 1),
                "mode": "charge" if p_w > 0.5 else "discharge" if p_w < -0.5 else "idle",
                "max_charge_a": round(b.max_charge_kw * 1000 / u, 2),
                "max_discharge_a": round(b.max_discharge_kw * 1000 / u, 2),
                "peak_discharge_a": round(b.peak_discharge_kw * 1000 / u, 2),
                "fault_until": b.fault_until.strftime("%H:%M:%S") if b.fault_until else None,
            })

        return {
            "type": "telemetry", "schema": SCHEMA, "seq": self._seq,
            "ts": round(time.time(), 3),
            "game_time": now.isoformat(timespec="seconds"),
            "game_status": s["status"], "phase": s["phase"],
            "generation": {
                "total_w": _w(s["gen"]),
                "sources": [{"id": "solar", "name": "Солнечные панели", "type": "solar",
                             "power_w": _w(s["gen"]), "forecast_w": _w(fc_kw), "sigma_w": _w(fc_sd)}],
            },
            "consumption": {"total_w": _w(s["cons"]),
                            "corridor": {"min_w": _w(s["corridor"]["min"]), "max_w": _w(s["corridor"]["max"])}},
            "balance_w": _w(s["net"]),
            "deficit_w": _w(s["deficit"]),
            "lines": lines,
            "active_battery": s["active_battery"],
            "batteries": bats,
        }

    def forecast(self, t_from: str | None = None, t_to: str | None = None, step_min: float | None = None) -> dict:
        e = self.engine
        a = hhmm_to_min(t_from) if t_from else e.sc.start
        b = hhmm_to_min(t_to) if t_to else e.sc.end
        step = float(step_min or self.forecast_step)
        if step <= 0 or b < a:
            raise ValueError("Неверный интервал или шаг")
        if (b - a) / step > 2000:
            raise ValueError("Слишком много точек (больше 2000) — увеличьте step_min")
        points, m = [], float(a)
        while m <= b + 1e-9:
            kw, sd = e.forecast.at(m)
            points.append({"t": round(m, 3), "time": _hhmm(m), "forecast_w": _w(kw), "sigma_w": _w(sd),
                           "low_w": _w(max(0.0, kw - sd)), "high_w": _w(kw + sd)})
            m += step
        return {"type": "forecast", "schema": SCHEMA, "source": "solar",
                "date": e.clock.now().date().isoformat(), "step_min": step,
                "from": _hhmm(a), "to": _hhmm(b), "points": points}

    def history(self, since: float | None = None) -> dict:
        e = self.engine
        lids = sorted(e.lines)
        out = []
        for h in e.history_view():
            if since is not None and h["t"] <= since:
                continue
            out.append({
                "t": h["t"], "time": _hhmm(h["t"]),
                "generation_w": _w(h["gen"]), "consumption_w": _w(h["cons"]),
                "corridor": {"min_w": _w(h["min"]), "max_w": _w(h["max"])},
                "active_battery": h["active"],
                "lines_w": {str(lid): _w(kw) for lid, kw in zip(lids, h["per"])},
                "batteries_ah": {bid: round(kwh * 1000 / self._volt(e.batteries[bid]), 2)
                                 for bid, kwh in h["soc"].items() if bid in e.batteries},
            })
        return {"type": "history", "schema": SCHEMA, "step_s": e.sc.history_step_s, "samples": out}


def install(app: FastAPI, engine: Engine, cfg: dict | None):
    tm = Telemetry(engine, cfg or {})
    if tm.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=tm.cors_origins, allow_methods=["GET"],
                           allow_headers=["X-Telemetry-Token"])

    @app.get("/api/telemetry")
    def telemetry(token: str | None = None, x_telemetry_token: str | None = Header(None)):
        tm.check(x_telemetry_token or token)
        snap = tm.snapshot()
        if snap is None:
            raise HTTPException(503, "Движок ещё не запущен, повторите через секунду")
        return snap

    @app.get("/api/telemetry/meta")
    def telemetry_meta(token: str | None = None, x_telemetry_token: str | None = Header(None)):
        tm.check(x_telemetry_token or token)
        return tm.meta()

    @app.get("/api/telemetry/forecast")
    def telemetry_forecast(from_: str | None = Query(None, alias="from"), to: str | None = None,
                           step_min: float | None = None, token: str | None = None,
                           x_telemetry_token: str | None = Header(None)):
        tm.check(x_telemetry_token or token)
        try:
            return tm.forecast(from_, to, step_min)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get("/api/telemetry/history")
    def telemetry_history(since: float | None = None, token: str | None = None,
                          x_telemetry_token: str | None = Header(None)):
        tm.check(x_telemetry_token or token)
        return tm.history(since)

    @app.websocket("/ws/telemetry")
    async def ws_telemetry(ws: WebSocket):
        tok = ws.query_params.get("token") or ws.headers.get("x-telemetry-token")
        if tm.token and tok != tm.token:
            await ws.close(code=1008, reason="Неверный токен телеметрии")
            return
        await ws.accept()
        peer = f"{ws.client.host}:{ws.client.port}" if ws.client else "?"
        log.info("ЦУП подключился: %s", peer)
        msgs = engine.term.messages(0)
        last_n = msgs[-1]["n"] if msgs else 0

        async def reader():
            while True:
                raw = await ws.receive_text()
                try:
                    req = json.loads(raw)
                except ValueError:
                    req = None
                kind = req.get("type") if isinstance(req, dict) else None
                try:
                    if kind == "ping":
                        await ws.send_json({"type": "pong", "ts": round(time.time(), 3)})
                    elif kind == "get_forecast":
                        await ws.send_json(tm.forecast(req.get("from"), req.get("to"), req.get("step_min")))
                    elif kind == "get_history":
                        await ws.send_json(tm.history(req.get("since")))
                    elif kind == "get_meta":
                        await ws.send_json({"type": "hello", **tm.meta()})
                    else:
                        await ws.send_json({"type": "error", "error": f"Неизвестный запрос: {kind}"})
                except ValueError as e:
                    await ws.send_json({"type": "error", "error": str(e)})

        async def writer():
            nonlocal last_n
            await ws.send_json({"type": "hello", **tm.meta()})
            await ws.send_json(tm.forecast())
            while True:
                snap = tm.snapshot()
                if snap:
                    await ws.send_json(snap)
                for m in engine.term.messages(last_n):
                    last_n = m["n"]
                    await ws.send_json({"type": "message", "n": m["n"], "ts": m["ts"],
                                        "level": m["level"], "text": m["text"]})
                await asyncio.sleep(tm.push_interval)

        tasks = [asyncio.create_task(reader()), asyncio.create_task(writer())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                exc = t.exception()
                if exc and not isinstance(exc, WebSocketDisconnect):
                    log.warning("ЦУП %s: %r", peer, exc)
        finally:
            for t in tasks:
                t.cancel()
            log.info("ЦУП отключился: %s", peer)
    return tm
