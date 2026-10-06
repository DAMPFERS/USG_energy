"""
Модель аккумулятора.

Активный аккумулятор принимает весь баланс (генерация − потребление):
излишек заряжает его (до max_charge_kw, остальное «сбрасывается»),
дефицит покрывается разрядом.

Отказоустойчивость:
  max_discharge_kw   — номинальная мощность разряда;
  peak_discharge_kw  — кратковременный пик, который АКБ ещё отдаёт;
  overload_tolerance_s — сколько секунд игрового времени АКБ терпит работу выше номинала,
                       после чего уходит в защиту (status = fault) на fault_recovery_min.
Статусы: ok, degraded (ухудшенные параметры, работает), fault (временная защита),
failed (отказ по сценарию, не работает до смены статуса сценарием).
"""
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta

WORKING = ("ok", "degraded")


@dataclass
class Battery:
    id: str
    name: str
    capacity_kwh: float
    charge_kwh: float
    max_charge_kw: float = 3.0
    max_discharge_kw: float = 3.0
    peak_discharge_kw: float | None = None
    efficiency: float = 0.95          # КПД заряда
    self_discharge_pct_h: float = 0.0  # саморазряд, % ёмкости в час
    overload_tolerance_s: float = 60.0
    fault_recovery_min: float = 10.0
    status: str = "ok"
    description: str = ""
    nominal_kwh: float | None = None  # паспортная ёмкость (для отображения потерь при деградации)
    # динамика
    power_kw: float = 0.0             # >0 заряд, <0 разряд
    overload_s: float = 0.0
    fault_until: datetime | None = None
    status_before_fault: str = "ok"

    def __post_init__(self):
        if self.nominal_kwh is None:
            self.nominal_kwh = self.capacity_kwh
        if self.peak_discharge_kw is None:
            self.peak_discharge_kw = self.max_discharge_kw * 1.5
        self.charge_kwh = min(self.charge_kwh, self.capacity_kwh)

    @property
    def working(self) -> bool:
        return self.status in WORKING

    def update(self, **fields):
        for k, v in fields.items():
            if k == "status" and v != "fault":
                self.fault_until = None
                self.overload_s = 0.0
            if hasattr(self, k):
                setattr(self, k, v)
        self.charge_kwh = max(0.0, min(self.charge_kwh, self.capacity_kwh))

    def idle(self, dt_h: float, now: datetime):
        self.power_kw = 0.0
        self.overload_s = max(0.0, self.overload_s - dt_h * 3600)
        self._self_discharge(dt_h)
        self._check_recovery(now)

    def step(self, net_kw: float, dt_h: float, now: datetime) -> tuple[float, float]:
        """Применить баланс к активному АКБ. Возвращает (дефицит кВт, сброшено кВт)."""
        self._check_recovery(now)
        self._self_discharge(dt_h)
        if not self.working or dt_h <= 0:
            self.power_kw = 0.0
            return (max(0.0, -net_kw), max(0.0, net_kw))

        if net_kw >= 0:  # заряд
            room = (self.capacity_kwh - self.charge_kwh) / (dt_h * self.efficiency)
            p = min(net_kw, self.max_charge_kw, max(0.0, room))
            self.charge_kwh += p * self.efficiency * dt_h
            self.power_kw = p
            self.overload_s = max(0.0, self.overload_s - dt_h * 3600)
            return 0.0, net_kw - p

        need = -net_kw
        avail = self.charge_kwh / dt_h
        p = min(need, self.peak_discharge_kw, avail)
        self.charge_kwh = max(0.0, self.charge_kwh - p * dt_h)
        self.power_kw = -p
        if p > self.max_discharge_kw + 1e-9:
            self.overload_s += dt_h * 3600
            if self.overload_s >= self.overload_tolerance_s:
                self.status_before_fault = self.status
                self.status = "fault"
                self.fault_until = now + timedelta(minutes=self.fault_recovery_min)
                self.overload_s = 0.0
        else:
            self.overload_s = max(0.0, self.overload_s - dt_h * 3600)
        return need - p, 0.0

    def _self_discharge(self, dt_h: float):
        if self.self_discharge_pct_h:
            self.charge_kwh = max(0.0, self.charge_kwh - self.capacity_kwh * self.self_discharge_pct_h / 100 * dt_h)

    def _check_recovery(self, now: datetime):
        if self.status == "fault" and self.fault_until and now >= self.fault_until:
            self.status = self.status_before_fault
            self.fault_until = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["fault_until"] = self.fault_until.strftime("%H:%M:%S") if self.fault_until else None
        d["soc"] = self.charge_kwh / self.capacity_kwh if self.capacity_kwh else 0.0
        return d
