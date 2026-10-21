"""
HTTP API и раздача веб-интерфейса участников.

GET  /api/state              — текущее состояние (опрос раз в 1–2 с)
GET  /api/history?since=t    — история игры (генерация, потребление, границы, линии, АКБ) + пропуски связи
GET  /api/forecast           — прогноз солнечной генерации на всю игру
GET  /api/terminal?after=N   — сообщения терминала с номером > N
POST /api/line               — {"line": 3, "on": true}
POST /api/priority           — {"line": 3, "priority": 5}   (линия, у которой был 5, получает старый приоритет 3-й)

Админка (заголовок X-Admin-Token, клиент — python -m admin):
GET  /api/admin/state                 — состояние + настройки и переопределения организатора
POST /api/admin/line                  — {"line": 3, "action": "on|off|lock|unlock", "minutes": 10, "reason": "..."}
POST /api/admin/line/name             — {"line": 3, "consumer": "BIO-L"}   (null — снять привязку)
POST /api/admin/line/identify         — {"line": 3}   помигать линией
POST /api/admin/line/priority         — {"line": 3, "priority": 5}
POST /api/admin/settings              — {"section": "battery|rules", "values": {"max_kw": 10}}  (+ запись в server.yaml)
POST /api/admin/battery               — {"charge_kwh": 10} | {"soc": 50} | {"connect": false, "minutes": 15}
POST /api/admin/crisis                — {"action": "pause|resume|cancel"}
POST /api/admin/solar                 — {"factor": 1.2}   0.0–2.0
POST /api/admin/message               — {"text": "...", "level": "INFO|WARN|CRIT|OK"}
POST /api/admin/time                  — {"time": "2 13:30"}  перемотка (сол ЧЧ:ММ), только clock.mode = sim

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


class PriorityCmd(BaseModel):
    line: int
    priority: int


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


class AdminSettings(BaseModel):
    section: str
    values: dict


class AdminBattery(BaseModel):
    charge_kwh: float | None = None
    soc: float | None = None
    connect: bool | None = None
    minutes: float | None = None


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

    @app.middleware("http")
    async def no_stale_cache(request, call_next):
        # браузер обязан перепроверять файлы панели (по ETag), иначе после обновления сервера
        # может остаться старый energy.js при новом index.html
        resp = await call_next(request)
        resp.headers.setdefault("Cache-Control", "no-cache")
        return resp

    def admin(x_admin_token: str | None = Header(None)):
        if not admin_token or x_admin_token != admin_token:
            raise HTTPException(403, "Нет доступа: неверный admin_token")

    # ------------------------------------------------------------------ участники
    @app.get("/api/state")
    def state():
        return engine.snapshot()

    @app.get("/api/history")
    def history(since: float | None = None):
        return {"samples": engine.history_view(since), "gaps": engine.gaps(), "gap_min": engine.gap_min(),
                "lines": [engine.name(l) for l in engine.lines]}

    @app.get("/api/forecast")
    def forecast():
        return engine.forecast_view()

    @app.get("/api/terminal")
    def terminal(after: int = 0):
        return {"messages": engine.term.messages(after)}

    @app.post("/api/line")
    def line(cmd: LineCmd):
        return _result(engine.set_line(cmd.line, cmd.on))

    @app.post("/api/priority")
    def priority(cmd: PriorityCmd):
        return _result(engine.set_priority(cmd.line, cmd.priority))

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

    @app.post("/api/admin/line/priority", dependencies=[Depends(admin)])
    def admin_priority(m: PriorityCmd):
        return _result(engine.set_priority(m.line, m.priority, who="ADMIN PRIORITY"))

    @app.post("/api/admin/settings", dependencies=[Depends(admin)])
    def admin_settings(m: AdminSettings):
        return _result(engine.admin_settings(m.section, m.values))

    @app.post("/api/admin/battery", dependencies=[Depends(admin)])
    def admin_battery(m: AdminBattery):
        return _result(engine.admin_battery(m.charge_kwh, m.soc, m.connect, m.minutes))

    @app.post("/api/admin/crisis", dependencies=[Depends(admin)])
    def admin_crisis(m: AdminAction):
        return _result(engine.admin_crisis(m.action))

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
        return _result(engine.admin_time(m.time))

    @app.get("/")
    def index():
        return FileResponse(os.path.join(static_dir, "index.html"))

    app.mount("/", StaticFiles(directory=static_dir), name="static")
    return app
