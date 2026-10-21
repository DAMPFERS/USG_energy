"""
Игровые часы и марсианский календарь.

GameClock — земное время игры:
  mode: real — системное время машины (боевой режим);
  mode: sim  — ускоренное время для отладки (speed). Точка отсчёта сохраняется в файл,
               поэтому после перезапуска сервера время продолжает идти «как будто сервер работал».

MarsTime — перевод земного времени в марсианское. Сол длится sol_min минут (24:40):
после 23:59 часы показывают 24:00…24:39, затем начинается следующий сол с 00:00.
Внутри сервера время купола — t: минуты от начала Сола 1 (00:00), непрерывная шкала.
"""
import json
import math
import os
import threading
import time
from datetime import datetime, timedelta

SOL_MIN = 24 * 60 + 40


class GameClock:
    def __init__(self, mode: str = "real", speed: float = 1.0, sim_start: datetime | None = None,
                 state_path: str | None = None):
        self.mode = mode
        self.speed = float(speed) if mode == "sim" else 1.0
        self._lock = threading.Lock()
        self._path = state_path
        self._real_t0 = time.time()
        self._game_t0 = sim_start or datetime.now()
        if mode == "sim" and state_path and os.path.exists(state_path):
            with open(state_path, encoding="utf-8") as f:
                d = json.load(f)
            self._real_t0, self._game_t0 = float(d["real_t0"]), datetime.fromisoformat(d["game_t0"])
        elif mode == "sim":
            self._save()

    def now(self) -> datetime:
        if self.mode != "sim":
            return datetime.now()
        with self._lock:
            return self._game_t0 + timedelta(seconds=(time.time() - self._real_t0) * self.speed)

    def set_time(self, dt: datetime) -> None:
        """Перемотка (только в режиме sim) — удобно при отладке сценария."""
        if self.mode != "sim":
            raise RuntimeError("Перемотка доступна только в режиме clock.mode: sim")
        with self._lock:
            self._game_t0, self._real_t0 = dt, time.time()
            self._save()

    def _save(self):
        if self._path:
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump({"real_t0": self._real_t0, "game_t0": self._game_t0.isoformat()}, f)


class MarsTime:
    """start (земное) соответствует моменту start_t (минуты от Сола 1 00:00)."""

    def __init__(self, start: datetime, start_t: float, sol_min: float = SOL_MIN):
        self.start = start
        self.start_t = float(start_t)
        self.sol_min = float(sol_min)

    def t(self, dt: datetime) -> float:
        return self.start_t + (dt - self.start).total_seconds() / 60.0

    def dt(self, t: float) -> datetime:
        return self.start + timedelta(minutes=t - self.start_t)

    def sol(self, t: float) -> int:
        return math.floor(t / self.sol_min) + 1

    def of_sol(self, t: float) -> float:
        """Минуты от начала сола (0 … sol_min)."""
        return t - (self.sol(t) - 1) * self.sol_min

    def hm(self, t: float | None, seconds: bool = False) -> str | None:
        if t is None:
            return None
        m = self.of_sol(t)
        s = int(m * 60 + 1e-6)
        out = f"{s // 3600:02d}:{s // 60 % 60:02d}"
        return out + f":{s % 60:02d}" if seconds else out

    def label(self, t: float | None, seconds: bool = False) -> str | None:
        """«Sol 2 14:05» — как видят участники."""
        if t is None:
            return None
        return f"Sol {self.sol(t)} {self.hm(t, seconds)}"

    def parse(self, s: str) -> float:
        """«2 14:05», «Sol 2 14:05» или «14:05» (Сол 1) → t."""
        parts = str(s).replace("Sol", "").replace("sol", "").split()
        if len(parts) == 1:
            sol, hm = 1, parts[0]
        elif len(parts) == 2:
            sol, hm = int(parts[0]), parts[1]
        else:
            raise ValueError(f"Время купола: ожидается «сол ЧЧ:ММ», получено '{s}'")
        return (sol - 1) * self.sol_min + hhmm_to_min(hm)


def hhmm_to_min(s: str) -> float:
    parts = [float(x) for x in str(s).strip().split(":")]
    if len(parts) not in (2, 3):
        raise ValueError(f"Ожидается ЧЧ:ММ, получено '{s}'")
    return parts[0] * 60 + parts[1] + (parts[2] / 60 if len(parts) == 3 else 0)
