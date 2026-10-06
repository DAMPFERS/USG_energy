"""
Слой умных реле (линии энергосети купола).

Каждая линия — либо реальное реле TONGOU/Tuya (driver: tuya), либо эмулятор
(driver: sim) — чтобы можно было отлаживать игру без железа или с частью железа.

RelayManager работает в отдельном daemon-потоке: параллельно опрашивает все реле
и хранит последние показания. Команды вкл/выкл выполняются сразу в вызывающем потоке.
"""
import logging
import math
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

log = logging.getLogger("relays")


@dataclass
class Reading:
    on: bool = False
    kw: float = 0.0          # игровая мощность, кВт (уже умножена на scale)
    raw_w: float = 0.0       # то, что реально намерило реле, Вт
    voltage: float = 0.0
    current: float = 0.0
    online: bool = False
    ts: float = 0.0


class SimRelay:
    """Эмулятор реле с «живой» нагрузкой: базовая мощность + медленный дрейф + шум."""

    def __init__(self, base_kw: float = 0.8, variability: float = 0.1, on: bool = True):
        self.base_kw = float(base_kw)
        self.variability = float(variability)
        self._on = on
        self._phase = random.uniform(0, 2 * math.pi)
        self._drift = 0.0

    def status(self) -> Reading:
        kw = 0.0
        if self._on:
            self._drift = 0.9 * self._drift + random.gauss(0, self.variability * 0.4)
            slow = 0.12 * math.sin(time.time() / 180 + self._phase)
            kw = max(0.0, self.base_kw * (1 + slow) + self._drift)
        return Reading(on=self._on, kw=kw, raw_w=kw * 1000, voltage=230.0,
                       current=kw * 1000 / 230.0, online=True, ts=time.time())

    def set(self, on: bool) -> bool:
        self._on = bool(on)
        return True


class TuyaRelay:
    """Реальное реле TONGOU TO-Q-SY2-JWT через tinytuya (протокол 3.5, локально)."""

    def __init__(self, device_id: str, local_key: str, ip: str, version: float = 3.5, timeout: float = 3.0):
        import tinytuya  # импорт здесь: без железа модуль не обязателен
        self._dev = tinytuya.OutletDevice(dev_id=device_id, address=ip, local_key=local_key)
        self._dev.set_version(float(version))
        self._dev.set_socketTimeout(timeout)
        self._dev.set_socketRetryLimit(1)
        self._lock = threading.Lock()   # tinytuya-устройство не потокобезопасно

    def status(self) -> Reading:
        with self._lock:
            st = self._dev.status()
        if not st or "Error" in st:
            raise IOError(f"ошибка опроса: {st}")
        dps = st.get("dps", {})
        # DPS: 1 — реле, 18 — ток мА, 19 — мощность Вт*10, 20 — напряжение В*10
        power_w = dps.get("19", 0) / 10.0
        return Reading(on=bool(dps.get("1", False)), kw=power_w / 1000.0, raw_w=power_w,
                       voltage=dps.get("20", 0) / 10.0, current=dps.get("18", 0) / 1000.0,
                       online=True, ts=time.time())

    def set(self, on: bool) -> bool:
        with self._lock:
            res = self._dev.set_status(bool(on))
        if not res or "Error" in res:
            raise IOError(f"ошибка переключения: {res}")
        return True


@dataclass
class LineConfig:
    id: int
    name: str
    driver: object
    scale: float = 1.0       # игровые кВт = измеренные кВт * scale
    extra: dict = field(default_factory=dict)


def build_lines(cfg_lines: list[dict]) -> list[LineConfig]:
    lines = []
    for i, c in enumerate(cfg_lines):
        lid = int(c.get("id", i + 1))
        name = c.get("name", f"Линия {lid}")
        kind = c.get("driver", "sim")
        if kind == "tuya":
            t = c["tuya"]
            drv = TuyaRelay(t["device_id"], t["local_key"], t["ip"], t.get("version", 3.5))
        else:
            s = c.get("sim", {})
            drv = SimRelay(s.get("base_kw", 0.8), s.get("variability", 0.1), s.get("on", True))
        lines.append(LineConfig(lid, name, drv, float(c.get("scale", 1.0))))
    return lines


class RelayManager:
    def __init__(self, lines: list[LineConfig], poll_interval: float = 1.0):
        self.lines = {l.id: l for l in lines}
        self.poll_interval = poll_interval
        self._data: dict[int, Reading] = {l.id: Reading() for l in lines}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._pool = ThreadPoolExecutor(max_workers=max(1, len(lines)), thread_name_prefix="relay")
        self._thread: threading.Thread | None = None

    def start(self):
        self._thread = threading.Thread(target=self._loop, name="RelayManager", daemon=True)
        self._thread.start()
        log.info("Опрос реле запущен: %d линий, интервал %.1f с", len(self.lines), self.poll_interval)

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._pool.shutdown(wait=False)

    def _poll_one(self, line: LineConfig):
        try:
            r = line.driver.status()
            r.kw *= line.scale
        except Exception as e:  # реле недоступно — помечаем offline, состояние оставляем прежним
            log.warning("[L%d] %s", line.id, e)
            with self._lock:
                old = self._data[line.id]
            r = Reading(on=old.on, kw=0.0, online=False, ts=time.time())
        with self._lock:
            self._data[line.id] = r

    def _loop(self):
        while not self._stop.is_set():
            t0 = time.time()
            list(self._pool.map(self._poll_one, self.lines.values()))
            self._stop.wait(max(0.05, self.poll_interval - (time.time() - t0)))

    def readings(self) -> dict[int, Reading]:
        with self._lock:
            return {k: Reading(**vars(v)) for k, v in self._data.items()}

    def set(self, line_id: int, on: bool) -> bool:
        line = self.lines.get(line_id)
        if not line:
            return False
        try:
            line.driver.set(on)
        except Exception as e:
            log.error("[L%d] %s", line_id, e)
            return False
        with self._lock:
            r = self._data[line_id]
            r.on = bool(on)
            if not on:
                r.kw = 0.0
        log.info("[L%d] %s", line_id, "ВКЛ" if on else "ВЫКЛ")
        return True
