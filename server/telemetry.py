"""
Телеметрия для ЦУП: данные энергосистемы в физических единицах (Вт, А, А·ч).

GET /api/telemetry                       — текущий срез
GET /api/telemetry/meta                  — справочник
GET /api/telemetry/forecast              — прогноз солнечной генерации W(t)
GET /api/telemetry/history?since=<t>     — история игры (для графиков) + пропуски связи
WS  /ws/telemetry                        — поток: hello, forecast, telemetry (каждые push_interval_s), message

Время купола — марсианское: t — минуты от Сола 1 00:00, sol + time (сол длится 24:40).
Мощности — игровые (кВт купола × 1000), т.е. те же, что видят участники.
Ток и ёмкость АКБ считаются через номинальное напряжение (telemetry.battery_voltage_v):
I = P / U, А·ч = Вт·ч / U.

Если в server.yaml задан telemetry.token — его нужно передавать в заголовке
X-Telemetry-Token или параметром ?token=.
"""
import asyncio
import json
import logging
import time

from fastapi import FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from .engine import Engine

log = logging.getLogger("telemetry")

SCHEMA = 2


def _w(kw) -> float | None:
    return None if kw is None else round(float(kw) * 1000.0, 1)


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
            raise HTTPException(403, "Invalid telemetry token")

    def _ah(self, kwh) -> float:
        return round(kwh * 1000 / self.voltage, 2)

    def _a(self, kw) -> float:
        return round(kw * 1000 / self.voltage, 2)

    def _mars(self, t: float) -> dict:
        m = self.engine.mars
        return {"t": round(t, 3), "sol": m.sol(t), "time": m.hm(t, seconds=True)}

    # ------------------------------------------------------------------ полезная нагрузка
    def meta(self) -> dict:
        e = self.engine
        b = e.battery
        return {
            "schema": SCHEMA,
            "name": e.sc.name, "scenario": e.sc.id,
            "sol_min": e.mars.sol_min,
            "start": self._mars(e.sc.start_t), "end": self._mars(e.sc.end_t),
            "clock": {"mode": e.clock.mode, "speed": e.clock.speed},
            "push_interval_s": self.push_interval,
            "lines": [{"id": lid, "name": e.name(lid), "key": e.map.key(lid), "priority": e.priority[lid]}
                      for lid in e.lines],
            "sources": [{"id": "solar", "name": "Solar panels", "type": "solar"}],
            "battery": {"name": b.name, "voltage_v": self.voltage, "capacity_ah": self._ah(b.capacity_kwh)},
        }

    def snapshot(self) -> dict | None:
        e = self.engine
        s = e.snapshot()
        if not s:
            return None
        readings = e.relays.readings()
        fc_kw, fc_sd = e.forecast.at(s["t"])
        self._seq += 1

        lines = []
        for l in s["lines"]:
            r = readings.get(l["id"])
            lines.append({
                "id": l["id"], "name": l["name"], "key": l.get("key"), "on": l["on"], "online": l["online"],
                "status": l["status"], "priority": l["priority"], "power_w": _w(l["kw"]),
                "reason": l["reason"] or None, "until": l["until"], "until_t": l["until_t"],
                "measured": {"power_w": round(r.raw_w, 1), "voltage_v": round(r.voltage, 1),
                             "current_a": round(r.current, 3)} if r and r.online else None,
            })

        b = s["battery"]
        p_w = b["power_kw"] * 1000.0
        return {
            "type": "telemetry", "schema": SCHEMA, "seq": self._seq,
            "ts": round(time.time(), 3),
            "earth_time": e.clock.now().isoformat(timespec="seconds"),
            "mars": {"t": s["t"], "sol": s["sol"], "time": s["time"]},
            "game_status": s["status"],
            "generation": {
                "total_w": _w(s["gen"]),
                "sources": [{"id": "solar", "name": "Solar panels", "type": "solar",
                             "power_w": _w(s["gen"]), "forecast_w": _w(fc_kw), "sigma_w": _w(fc_sd)}],
            },
            "consumption": {"total_w": _w(s["cons"])},
            "limits": {"max_w": _w(s["limits"]["max"]), "min_w": _w(s["limits"]["min"]),
                       "max_charge_w": _w(s["limits"]["max_charge"]),
                       "max_discharge_w": _w(s["limits"]["max_discharge"])},
            "balance_w": _w(s["net"]),
            "deficit_w": _w(s["deficit"]),
            "alarms": [{"kind": a["kind"], "title": a["title"], "left_s": a["left_s"]} for a in s["alarms"]],
            "lines": lines,
            "battery": {
                "name": b["name"], "status": "connected" if b["connected"] else "disconnected",
                "off_until": b["off_until"], "voltage_v": self.voltage,
                "capacity_ah": self._ah(b["capacity_kwh"]), "available_ah": self._ah(b["charge_kwh"]),
                "soc_pct": round(100 * b["soc"], 1),
                "current_a": round(p_w / self.voltage, 2), "power_w": round(p_w, 1),
                "mode": "charge" if p_w > 0.5 else "discharge" if p_w < -0.5 else "idle",
                "max_charge_a": self._a(b["max_charge_kw"]), "max_discharge_a": self._a(b["max_discharge_kw"]),
            },
        }

    def forecast(self, t_from: str | None = None, t_to: str | None = None, step_min: float | None = None) -> dict:
        e = self.engine
        a = e.mars.parse(t_from) if t_from else e.sc.start_t
        b = e.mars.parse(t_to) if t_to else e.sc.end_t
        step = float(step_min or self.forecast_step)
        if step <= 0 or b < a:
            raise ValueError("Invalid interval or step")
        if (b - a) / step > 2000:
            raise ValueError("Too many points (over 2000), increase step_min")
        points, m = [], float(a)
        while m <= b + 1e-9:
            kw, sd = e.forecast.at(m)
            points.append({**self._mars(m), "time": e.mars.hm(m), "forecast_w": _w(kw), "sigma_w": _w(sd),
                           "low_w": _w(max(0.0, kw - sd)), "high_w": _w(kw + sd)})
            m += step
        return {"type": "forecast", "schema": SCHEMA, "source": "solar", "step_min": step,
                "from": e.mars.label(a), "to": e.mars.label(b), "points": points}

    def history(self, since: float | None = None) -> dict:
        e = self.engine
        lids = sorted(e.lines)
        out = []
        for h in e.history_view(since):
            out.append({
                **self._mars(h["t"]), "time": e.mars.hm(h["t"]),
                "generation_w": _w(h["gen"]), "consumption_w": _w(h["cons"]),
                "limits": {"max_w": _w(h["max"]), "min_w": _w(h["min"])},
                "lines_w": {str(lid): _w(kw) for lid, kw in zip(lids, h["per"])},
                "battery_ah": self._ah(h["soc"]), "battery_a": round(h["bat"] * 1000 / self.voltage, 2),
                "battery_connected": not h["boff"],
            })
        gaps = [{"from": self._mars(a), "to": self._mars(b)} for a, b in e.gaps()]
        return {"type": "history", "schema": SCHEMA, "step_s": e.sc.history_step_s, "samples": out, "gaps": gaps}


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
            raise HTTPException(503, "Engine is not running yet, retry in a second")
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
            await ws.close(code=1008, reason="Invalid telemetry token")
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
                        await ws.send_json({"type": "error", "error": f"Unknown request: {kind}"})
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
                    await ws.send_json({"type": "message", "n": m["n"], "ts": m["ts"], "mars": m.get("mars"),
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
