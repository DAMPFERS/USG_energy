"""
HTTP API и раздача веб-интерфейса участников.

GET  /api/state              — текущее состояние (опрос раз в 1–2 с)
GET  /api/history            — история дня (генерация, потребление, коридор, линии, АКБ)
GET  /api/forecast           — прогноз солнечной генерации на игровой день
GET  /api/terminal?after=N   — сообщения терминала с номером > N
POST /api/line               — {"line": 3, "on": true}
POST /api/battery/active     — {"id": "B"}
POST /api/admin/message      — {"text": "...", "level": "WARN"}   (заголовок X-Admin-Token)
POST /api/admin/time         — {"time": "13:30"}  перемотка, только clock.mode = sim

Телеметрия для ЦУП (Вт, А, А·ч) — см. server/telemetry.py и TELEMETRY.md.
"""
import os

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .engine import Engine
from . import telemetry


class LineCmd(BaseModel):
    line: int
    on: bool


class BatteryCmd(BaseModel):
    id: str


class AdminMsg(BaseModel):
    text: str
    level: str = "INFO"


class AdminTime(BaseModel):
    time: str


def create_app(engine: Engine, static_dir: str, admin_token: str | None,
               telemetry_cfg: dict | None = None) -> FastAPI:
    app = FastAPI(title="Купол: энергосистема")
    telemetry.install(app, engine, telemetry_cfg)

    def check_admin(token):
        if not admin_token or token != admin_token:
            raise HTTPException(403, "Нет доступа")

    @app.get("/api/state")
    def state():
        return engine.snapshot()

    @app.get("/api/history")
    def history():
        return {"samples": engine.history_view(), "lines": [l.name for l in engine.lines.values()],
                "batteries": list(engine.batteries)}

    @app.get("/api/forecast")
    def forecast():
        return engine.forecast_view()

    @app.get("/api/terminal")
    def terminal(after: int = 0):
        return {"messages": engine.term.messages(after)}

    @app.post("/api/line")
    def line(cmd: LineCmd):
        ok, msg = engine.set_line(cmd.line, cmd.on)
        if not ok:
            raise HTTPException(409, msg)
        return {"ok": True}

    @app.post("/api/battery/active")
    def battery(cmd: BatteryCmd):
        ok, msg = engine.set_active_battery(cmd.id)
        if not ok:
            raise HTTPException(409, msg)
        return {"ok": True}

    @app.post("/api/admin/message")
    def admin_message(m: AdminMsg, x_admin_token: str | None = Header(None)):
        check_admin(x_admin_token)
        engine.admin_message(m.text, m.level)
        return {"ok": True}

    @app.post("/api/admin/time")
    def admin_time(m: AdminTime, x_admin_token: str | None = Header(None)):
        check_admin(x_admin_token)
        try:
            engine.clock.set_time(m.time)
        except RuntimeError as e:
            raise HTTPException(409, str(e))
        return {"ok": True}

    @app.get("/")
    def index():
        return FileResponse(os.path.join(static_dir, "index.html"))

    app.mount("/", StaticFiles(directory=static_dir), name="static")
    return app
