"""
Солнечная генерация и прогноз.

Forecast — прогноз из CSV (time,forecast_kw,sigma_kw), линейная интерполяция.
SimSolarSource — источник «фактической» генерации в отдельном потоке:
    факт = прогноз(t) * factor + коррелированный шум ~ sigma(t)
    factor задаёт сценарий (например, песчаная буря = 0.3).

Чтобы подключить реальные панели, реализуйте класс с теми же методами
(start, stop, power_kw, set_factor) и укажите его в server.yaml (solar.source).
"""
import csv
import logging
import math
import random
import threading
from bisect import bisect_right

from .clock import GameClock, hhmm_to_min, dt_to_min

log = logging.getLogger("solar")


class Forecast:
    def __init__(self, path: str):
        self.t: list[float] = []      # минуты от полуночи
        self.mean: list[float] = []
        self.sigma: list[float] = []
        with open(path, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                self.t.append(hhmm_to_min(row["time"]))
                self.mean.append(float(row["forecast_kw"]))
                self.sigma.append(float(row.get("sigma_kw") or 0))
        if not self.t:
            raise ValueError(f"Пустой прогноз: {path}")
        log.info("Прогноз загружен: %s (%d точек)", path, len(self.t))

    def at(self, minute: float) -> tuple[float, float]:
        t = self.t
        if minute <= t[0]:
            return self.mean[0], self.sigma[0]
        if minute >= t[-1]:
            return self.mean[-1], self.sigma[-1]
        i = bisect_right(t, minute) - 1
        k = (minute - t[i]) / (t[i + 1] - t[i])
        return (self.mean[i] + k * (self.mean[i + 1] - self.mean[i]),
                self.sigma[i] + k * (self.sigma[i + 1] - self.sigma[i]))

    def as_dict(self, t_from: float, t_to: float) -> dict:
        idx = [i for i, x in enumerate(self.t) if t_from <= x <= t_to]
        return {"t": [self.t[i] for i in idx], "mean": [self.mean[i] for i in idx],
                "sd": [self.sigma[i] for i in idx]}


class SimSolarSource:
    def __init__(self, forecast: Forecast, clock: GameClock, interval: float = 1.0,
                 noise: float = 1.0, correlation: float = 0.97):
        self.forecast = forecast
        self.clock = clock
        self.interval = interval
        self.noise = noise              # множитель к sigma из прогноза
        self.correlation = correlation  # «вязкость» шума: облака не прыгают каждую секунду
        self._factor = 1.0
        self._z = 0.0
        self._kw = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._loop, name="SolarSource", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def set_factor(self, factor: float):
        with self._lock:
            self._factor = float(factor)

    def power_kw(self) -> float:
        with self._lock:
            return self._kw

    def _loop(self):
        a = self.correlation
        while not self._stop.is_set():
            mean, sd = self.forecast.at(dt_to_min(self.clock.now()))
            # AR(1) со стационарной дисперсией 1
            self._z = a * self._z + math.sqrt(1 - a * a) * random.gauss(0, 1)
            with self._lock:
                kw = mean * self._factor + self._z * sd * self.noise * self._factor
                self._kw = max(0.0, kw) if mean > 0 else 0.0
            self._stop.wait(self.interval)
