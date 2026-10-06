"""
Игровые часы.

mode: real  — время купола = реальное локальное время (для игры).
mode: sim   — время стартует с заданного момента и идёт с ускорением speed
              (для отладки сценария: прогнать день за полчаса).
"""
import threading
import time
from datetime import datetime, date, timedelta


class GameClock:
    def __init__(self, mode: str = "real", start: str | None = None, speed: float = 1.0,
                 game_date: date | None = None):
        self.mode = mode
        self.speed = float(speed) if mode == "sim" else 1.0
        self._lock = threading.Lock()
        self._real_t0 = time.time()
        if mode == "sim":
            d = game_date or date.today()
            hh, mm = (int(x) for x in (start or "08:00").split(":"))
            self._game_t0 = datetime.combine(d, datetime.min.time()) + timedelta(hours=hh, minutes=mm)
        else:
            self._game_t0 = None

    def now(self) -> datetime:
        if self.mode != "sim":
            return datetime.now()
        with self._lock:
            elapsed = (time.time() - self._real_t0) * self.speed
            return self._game_t0 + timedelta(seconds=elapsed)

    def set_time(self, hhmm: str) -> None:
        """Перемотка (только в режиме sim) — удобно при отладке сценария."""
        if self.mode != "sim":
            raise RuntimeError("Перемотка доступна только в режиме sim")
        hh, mm = (int(x) for x in hhmm.split(":"))
        with self._lock:
            self._game_t0 = datetime.combine(self._game_t0.date(), datetime.min.time()) + timedelta(hours=hh, minutes=mm)
            self._real_t0 = time.time()


def hhmm_to_min(s: str) -> int:
    hh, mm = (int(x) for x in str(s).strip().split(":"))
    return hh * 60 + mm


def dt_to_min(dt: datetime) -> float:
    """Минуты от полуночи (с дробной частью)."""
    return dt.hour * 60 + dt.minute + dt.second / 60 + dt.microsecond / 60e6
