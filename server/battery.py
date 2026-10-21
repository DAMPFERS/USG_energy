"""
Модель аккумулятора купола (он один).

Аккумулятор принимает весь баланс (генерация − потребление): излишек заряжает его
(до max_charge_kw, остальное и всё при полном АКБ — теряется), дефицит покрывается
разрядом до max_discharge_kw. Параметры (ёмкость, мощности) берутся из Settings —
организатор меняет их на лету.

Если избыток генерации дольше surplus_grace_min превышает max_charge_kw, АКБ
отключается на battery_off_min (off_until): не заряжается и не разряжается.
"""
from datetime import datetime

from .settings import Settings


class Battery:
    def __init__(self, settings: Settings):
        self.s = settings
        self.charge_kwh = min(settings.battery["initial_kwh"], settings.battery["capacity_kwh"])
        self.power_kw = 0.0               # >0 заряд, <0 разряд
        self.off_until: datetime | None = None

    @property
    def name(self) -> str:
        return self.s.battery_name

    @property
    def capacity_kwh(self) -> float:
        return self.s.battery["capacity_kwh"]

    @property
    def max_charge_kw(self) -> float:
        return self.s.battery["max_charge_kw"]

    @property
    def max_discharge_kw(self) -> float:
        return self.s.battery["max_discharge_kw"]

    @property
    def soc(self) -> float:
        return self.charge_kwh / self.capacity_kwh if self.capacity_kwh else 0.0

    @property
    def full(self) -> bool:
        return self.charge_kwh >= self.capacity_kwh - 1e-3

    def connected(self, now: datetime) -> bool:
        return self.off_until is None or now >= self.off_until

    def step(self, net_kw: float, dt_h: float, now: datetime) -> tuple[float, float]:
        """Применить баланс. Возвращает (дефицит кВт, потеряно кВт)."""
        self.charge_kwh = max(0.0, min(self.charge_kwh, self.capacity_kwh))
        if not self.connected(now) or dt_h <= 0:
            self.power_kw = 0.0
            return max(0.0, -net_kw), max(0.0, net_kw)
        if net_kw >= 0:  # заряд
            room = (self.capacity_kwh - self.charge_kwh) / dt_h
            p = min(net_kw, self.max_charge_kw, max(0.0, room))
            self.charge_kwh += p * dt_h
            self.power_kw = p
            return 0.0, net_kw - p
        need = -net_kw
        p = min(need, self.max_discharge_kw, self.charge_kwh / dt_h)
        self.charge_kwh = max(0.0, self.charge_kwh - p * dt_h)
        self.power_kw = -p
        return need - p, 0.0
