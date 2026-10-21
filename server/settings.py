"""
Параметры игры, которые организатор меняет на лету: аккумулятор (battery) и правила (rules)
из config/server.yaml. Изменения сразу действуют на сервере и записываются обратно в файл —
построчно, с сохранением комментариев и порядка.
"""
import logging
import os
import re
import threading

log = logging.getLogger("settings")

# ключ: (значение по умолчанию, описание)
BATTERY = {
    "capacity_kwh": (40.0, "ёмкость, кВт·ч"),
    "initial_kwh": (25.0, "заряд на старте игры, кВт·ч"),
    "max_charge_kw": (5.0, "макс. мощность заряда, кВт (нижняя граница: избыток генерации выше неё)"),
    "max_discharge_kw": (6.0, "макс. мощность разряда, кВт"),
}
RULES = {
    "max_kw": (9.0, "верхняя граница коридора потребления, кВт"),
    "overload_grace_min": (5.0, "перегруз: через сколько минут блэкаут (все линии выкл)"),
    "surplus_grace_min": (5.0, "избыток генерации выше заряда: через сколько минут отключится АКБ"),
    "battery_off_min": (15.0, "на сколько минут отключается АКБ"),
    "deficit_grace_s": (10.0, "дефицит: через сколько секунд отключать линии"),
    "deficit_lock_min": (15.0, "дефицит: на сколько минут блокируются отключённые линии"),
    "clear_s": (10.0, "сколько секунд нарушения не должно быть, чтобы предупреждение снялось"),
}
SECTIONS = {"battery": BATTERY, "rules": RULES}


class Settings:
    def __init__(self, cfg: dict, path: str | None):
        self.path = path
        self._lock = threading.Lock()
        b = cfg.get("battery") or {}
        self.battery_name = str(b.get("name", "Dome battery"))
        self.battery = {k: float(b.get(k, d)) for k, (d, _) in BATTERY.items()}
        r = cfg.get("rules") or {}
        self.rules = {k: float(r.get(k, d)) for k, (d, _) in RULES.items()}

    def section(self, name: str) -> dict:
        return {"battery": self.battery, "rules": self.rules}[name]

    def update(self, section: str, values: dict) -> tuple[bool, str]:
        if section not in SECTIONS:
            return False, f"Раздел — одно из: {', '.join(SECTIONS)}"
        spec = SECTIONS[section]
        upd = {}
        for k, v in values.items():
            if k not in spec:
                return False, f"Неизвестный параметр '{k}'. Есть: {', '.join(spec)}"
            try:
                v = float(str(v).replace(",", "."))
            except ValueError:
                return False, f"{k}: '{v}' — не число"
            if v < 0 or (k == "capacity_kwh" and v <= 0):
                return False, f"{k}: значение вне допустимого диапазона"
            upd[k] = v
        with self._lock:
            self.section(section).update(upd)
            if self.path:
                try:
                    set_yaml_values(self.path, section, upd)
                except OSError as e:
                    log.error("Не удалось записать %s: %s", self.path, e)
                    return False, f"Применено, но не записано в файл: {e}"
        return True, ""

    def view(self) -> dict:
        return {"battery": dict(self.battery, name=self.battery_name), "rules": dict(self.rules),
                "fields": {s: {k: d for k, (_, d) in spec.items()} for s, spec in SECTIONS.items()}}


def _fmt(v) -> str:
    return f"{v:g}" if isinstance(v, float) else str(v)


def set_yaml_values(path: str, section: str, values: dict):
    """Заменить значения `key: value` в разделе верхнего уровня, не трогая остальное."""
    with open(path, encoding="utf-8", newline="") as f:
        text = f.read()
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(nl)
    head = next((i for i, l in enumerate(lines) if re.match(rf"^{re.escape(section)}:\s*(#.*)?$", l)), None)
    left = dict(values)
    if head is None:
        if lines and lines[-1] == "":
            lines.pop()
        lines += ["", f"{section}:"] + [f"  {k}: {_fmt(v)}" for k, v in left.items()] + [""]
    else:
        last = head
        i = head + 1
        while i < len(lines) and (lines[i].strip() == "" or lines[i][:1] in (" ", "\t", "#")):
            m = re.match(r"^(\s+)([A-Za-z_]\w*)(\s*:\s*)([^#]*?)(\s*#.*)?$", lines[i])
            if m:
                last = i
                if m.group(2) in left:
                    v = left.pop(m.group(2))
                    lines[i] = m.group(1) + m.group(2) + m.group(3) + _fmt(v) + (m.group(5) or "")
            i += 1
        for k, v in left.items():
            last += 1
            lines.insert(last, f"  {k}: {_fmt(v)}")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(nl.join(lines))
    os.replace(tmp, path)
