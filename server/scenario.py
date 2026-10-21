"""
Загрузка сценария игры (YAML). Формат описан в config/scenarios/mission.yaml.

Сценарий — это одна большая игра: начало (земное время), длительность, прогноз солнца
и сообщения терминала по расписанию. Время сообщений — марсианское («сол ЧЧ:ММ»).
"""
import os
from dataclasses import dataclass, field
from datetime import datetime

import yaml

from .clock import SOL_MIN, MarsTime, hhmm_to_min
from .terminal import LEVELS


@dataclass
class Message:
    key: str              # для учёта уже отправленных
    t: float              # время купола (минуты от Сола 1 00:00)
    level: str
    text: str


@dataclass
class Scenario:
    id: str
    path: str
    name: str
    mars: MarsTime
    start_t: float
    end_t: float
    forecast_csv: str
    history_step_s: float
    messages: list = field(default_factory=list)


def load_scenario(path: str) -> Scenario:
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    base_dir = os.path.dirname(os.path.abspath(path))

    start = datetime.fromisoformat(str(raw["start"]))
    sol_min = float(raw.get("sol_min", SOL_MIN))
    mars0 = MarsTime(start, 0, sol_min)
    start_t = mars0.parse(raw["start_mars"]) if raw.get("start_mars") else hhmm_to_min(start.strftime("%H:%M"))
    mars = MarsTime(start, start_t, sol_min)
    end_t = start_t + float(raw.get("duration_h", 50)) * 60

    messages = []
    for m in raw.get("messages", []) or []:
        t = mars.parse(m["at"])
        level = str(m.get("level", "INFO")).upper()
        if level not in LEVELS:
            raise ValueError(f"Сценарий: уровень '{level}' в сообщении {m['at']} — допустимо {', '.join(LEVELS)}")
        text = str(m["text"]).strip()
        messages.append(Message(f"msg:{t:g}:{text[:40]}", t, level, text))
    messages.sort(key=lambda x: x.t)

    fc = raw["forecast_csv"]
    if not os.path.isabs(fc):
        fc = os.path.normpath(os.path.join(base_dir, fc))

    return Scenario(
        id=os.path.splitext(os.path.basename(path))[0],
        path=path,
        name=raw.get("name", "Mars dome"),
        mars=mars,
        start_t=start_t,
        end_t=end_t,
        forecast_csv=fc,
        history_step_s=float(raw.get("history_step_s", 30)),
        messages=messages,
    )
