"""
Солнечная генерация и прогноз.

Генерация складывается из двух составляющих:
  * низкочастотная — положение Солнца (Forecast, data/forecast.csv: sol,time,forecast_kw; шаг 5 мин,
    время марсианское). Только её видят участники и ЦУП: прогноз ± fluctuation_pct;
  * высокочастотная — запылённость атмосферы (DustSeries, data/dust.csv: sol,time,dust_pct; 0 — ясно,
    100 — пылевая буря, генерация 0). Участникам не показывается: по ней коллеги из георазведки
    готовят спутниковые снимки (выгрузка: python -m server.export_dust).

SimSolarSource — «фактическая» генерация в отдельном потоке:
    факт = солнце(t) × (1 − пыль(t)/100) × factor × (1 + шум), шум ~ fluctuation_pct %
    factor задаёт организатор (команда solar: очистка панелей после бури и т.п.).

Чтобы подключить реальные панели, реализуйте класс с теми же методами
(start, stop, power_kw, set_factor) и укажите его в server.yaml (solar.source).
"""
import csv
import logging
import math
import os
import random
import threading
from bisect import bisect_right
from typing import Callable

from .clock import hhmm_to_min

log = logging.getLogger("solar")


def _read_series(path: str, sol_min: float, column: str) -> tuple[list[float], list[float]]:
    """CSV sol,time,<column> → (t, value), отсортировано по t."""
    rows = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            sol = int(row.get("sol") or 1)
            rows.append(((sol - 1) * sol_min + hhmm_to_min(row["time"]), float(row[column])))
    rows.sort()
    return [r[0] for r in rows], [r[1] for r in rows]


def _interp(ts: list[float], vs: list[float], t: float) -> float:
    if t <= ts[0]:
        return vs[0]
    if t >= ts[-1]:
        return vs[-1]
    i = bisect_right(ts, t) - 1
    k = (t - ts[i]) / (ts[i + 1] - ts[i]) if ts[i + 1] > ts[i] else 0.0
    return vs[i] + k * (vs[i + 1] - vs[i])


class Forecast:
    """Солнечная (низкочастотная) составляющая: что генерировали бы чистые панели при ясном небе."""

    def __init__(self, path: str, sol_min: float, fluctuation_pct: float = 5.0):
        self.t, self.mean = _read_series(path, sol_min, "forecast_kw")
        if not self.t:
            raise ValueError(f"Пустой прогноз: {path}")
        self.fluctuation = fluctuation_pct / 100.0
        log.info("Прогноз солнца загружен: %s (%d точек)", path, len(self.t))

    def at(self, t: float) -> tuple[float, float]:
        """(прогноз кВт, разброс кВт)."""
        kw = _interp(self.t, self.mean, t)
        return kw, kw * self.fluctuation

    def as_dict(self, t_from: float, t_to: float) -> dict:
        idx = [i for i, x in enumerate(self.t) if t_from <= x <= t_to]
        return {"t": [self.t[i] for i in idx], "mean": [self.mean[i] for i in idx],
                "sd": [self.mean[i] * self.fluctuation for i in idx]}


class DustSeries:
    """Запылённость атмосферы, % (0 — ясно, 100 — буря). Между точками — линейно."""

    def __init__(self, path: str | None, sol_min: float):
        self.t, self.v = [], []
        if path and os.path.exists(path):
            self.t, self.v = _read_series(path, sol_min, "dust_pct")
            log.info("Запылённость загружена: %s (%d точек)", path, len(self.t))
        else:
            log.warning("Файл запылённости не найден (%s) — считаем, что небо ясное", path)

    def at(self, t: float) -> float:
        return max(0.0, min(100.0, _interp(self.t, self.v, t))) if self.t else 0.0


class SimSolarSource:
    def __init__(self, forecast: Forecast, dust: DustSeries, now_t: Callable[[], float],
                 interval: float = 1.0, correlation: float = 0.97):
        self.forecast = forecast
        self.dust = dust
        self.now_t = now_t              # текущее время купола t
        self.interval = interval
        self.correlation = correlation  # «вязкость» флуктуаций: не прыгают каждую секунду
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
            t = self.now_t()
            sun, _ = self.forecast.at(t)
            clear = 1.0 - self.dust.at(t) / 100.0
            # AR(1) со стационарной дисперсией 1
            self._z = a * self._z + math.sqrt(1 - a * a) * random.gauss(0, 1)
            with self._lock:
                kw = sun * clear * self._factor * (1 + self._z * self.forecast.fluctuation)
                self._kw = max(0.0, kw) if sun > 0 else 0.0
            self._stop.wait(self.interval)
