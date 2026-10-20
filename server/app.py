"""
HTTP API и раздача веб-интерфейса участников.

GET  /api/state              — текущее состояние (опрос раз в 1–2 с)
GET  /api/history            — история дня (генерация, потребление, коридор, линии, АКБ)
GET  /api/forecast           — прогноз солнечной генерации на игровой день
GET  /api/terminal?after=N   — сообщения терминала с номером > N
POST /api/line               — {"line": 3, "on": true}
POST /api/battery/active     — {"id": "B"}

Админка (заголовок X-Admin-Token, клиент — python -m admin):
GET  /api/admin/state                 — состояние + переопределения организатора
POST /api/admin/line                  — {"line": 3, "action": "on|off|lock|unlock", "minutes": 10, "reason": "..."}
POST /api/admin/line/name             — {"line": 3, "consumer": "BIO-L"}   (null — снять привязку)
POST /api/admin/line/identify         — {"line": 3}   помигать линией
POST /api/admin/corridor              — {"bound": "min|max|all", "value": 4.5 | null, "minutes": 15}
POST /api/admin/battery               — {"id": "A", "fields": {"charge_kwh": 10, "status": "ok"}}
POST /api/admin/battery/active        — {"id": "B"}
POST /api/admin/crisis                — {"action": "pause|resume|cancel"}   нарушения коридора/дефицит
POST /api/admin/phase                 — {"action": "pause|resume|cancel"}   текущая фаза сценария
POST /api/admin/solar                 — {"factor": 1.2}   0.0–2.0, умножается на коэффициент фазы
POST /api/admin/message               — {"text": "...", "level": "INFO|WARN|CRIT|OK"}
POST /api/admin/time                  — {"time": "13:30"}  перемотка, только clock.mode = sim

Телеметрия для ЦУП (Вт, А, А·ч) — см. server/telemetry.py и ISG_Energy_telemetry.md.
"""
import os

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .engine import Engine
from .terminal import LEVELS
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


class AdminLine(BaseModel):
    line: int
    action: str
    minutes: float | None = None
    reason: str | None = None


class AdminLineName(BaseModel):
    line: int
    consumer: str | None = None


class AdminLineRef(BaseModel):
    line: int


class AdminCorridor(BaseModel):
    bound: str
    value: float | None = None
    minutes: float | None = None


class AdminBattery(BaseModel):
    id: str
    fields: dict


class AdminAction(BaseModel):
    action: str


class AdminSolar(BaseModel):
    factor: float


def _result(res: tuple[bool, str]):
    ok, msg = res
    if not ok:
        raise HTTPException(409, msg)
    return {"ok": True}


def create_app(engine: Engine, static_dir: str, admin_token: str | None,
               telemetry_cfg: dict | None = None) -> FastAPI:
    app = FastAPI(title="Dome power system")
    telemetry.install(app, engine, telemetry_cfg)

    def admin(x_admin_token: str | None = Header(None)):
        if not admin_token or x_admin_token != admin_token:
            raise HTTPException(403, "Нет доступа: неверный admin_token")

    # ------------------------------------------------------------------ участники
    @app.get("/api/state")
    def state():
        return engine.snapshot()

    @app.get("/api/history")
    def history():
        return {"samples": engine.history_view(), "lines": [engine.name(l) for l in engine.lines],
                "batteries": list(engine.batteries)}

    @app.get("/api/forecast")
    def forecast():
        return engine.forecast_view()

    @app.get("/api/terminal")
    def terminal(after: int = 0):
        return {"messages": engine.term.messages(after)}

    @app.post("/api/line")
    def line(cmd: LineCmd):
        return _result(engine.set_line(cmd.line, cmd.on))

    @app.post("/api/battery/active")
    def battery(cmd: BatteryCmd):
        return _result(engine.set_active_battery(cmd.id))

    # ------------------------------------------------------------------ админка
    @app.get("/api/admin/state", dependencies=[Depends(admin)])
    def admin_state(after: int = 0):
        return {"state": engine.snapshot(), "admin": engine.admin_view(),
                "messages": engine.term.messages(after)}

    @app.post("/api/admin/line", dependencies=[Depends(admin)])
    def admin_line(m: AdminLine):
        return _result(engine.admin_line(m.line, m.action, m.minutes, m.reason))

    @app.post("/api/admin/line/name", dependencies=[Depends(admin)])
    def admin_line_name(m: AdminLineName):
        return _result(engine.admin_name(m.line, m.consumer))

    @app.post("/api/admin/line/identify", dependencies=[Depends(admin)])
    def admin_identify(m: AdminLineRef):
        return _result(engine.identify(m.line))

    @app.post("/api/admin/corridor", dependencies=[Depends(admin)])
    def admin_corridor(m: AdminCorridor):
        return _result(engine.admin_corridor(m.bound, m.value, m.minutes))

    @app.post("/api/admin/battery", dependencies=[Depends(admin)])
    def admin_battery(m: AdminBattery):
        return _result(engine.admin_battery(m.id, m.fields))

    @app.post("/api/admin/battery/active", dependencies=[Depends(admin)])
    def admin_battery_active(m: BatteryCmd):
        return _result(engine.set_active_battery(m.id, force=True))

    @app.post("/api/admin/crisis", dependencies=[Depends(admin)])
    def admin_crisis(m: AdminAction):
        return _result(engine.admin_crisis(m.action))

    @app.post("/api/admin/phase", dependencies=[Depends(admin)])
    def admin_phase(m: AdminAction):
        return _result(engine.admin_phase(m.action))

    @app.post("/api/admin/solar", dependencies=[Depends(admin)])
    def admin_solar(m: AdminSolar):
        return _result(engine.admin_solar(m.factor))

    @app.post("/api/admin/message", dependencies=[Depends(admin)])
    def admin_message(m: AdminMsg):
        if m.level.upper() not in LEVELS:
            raise HTTPException(400, f"Уровень — одно из: {', '.join(LEVELS)}")
        engine.admin_message(m.text, m.level.upper())
        return {"ok": True}

    @app.post("/api/admin/time", dependencies=[Depends(admin)])
    def admin_time(m: AdminTime):
        try:
            engine.clock.set_time(m.time)
        except (RuntimeError, ValueError) as e:
            raise HTTPException(409, str(e))
        return {"ok": True}

    @app.get("/")
    def index():
        return FileResponse(os.path.join(static_dir, "index.html"))

    app.mount("/", StaticFiles(directory=static_dir), name="static")
    return app
