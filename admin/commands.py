"""
Команды консоли организатора. execute() разбирает строку, вызывает API и
возвращает строки результата для вывода в консоль.
"""
from .client import AdminClient, ApiError

HELP = """\
ЛИНИИ   <L> = номер линии, ключ потребителя (BIO-L), список 1,3,5, диапазон 1-4 или all
  on <L>                       включить
  off <L>                      выключить
  lock <L> [мин] [причина...]  заблокировать и выключить; мин — игровые минуты (0 или без — до unlock)
  unlock <L>                   снять любую блокировку: админа, сценария, аварийную, фазы
  name <линия> <КЛЮЧ|->        привязать потребителя к линии (если ключ занят — линии меняются)
  names                        список потребителей и их линий
  blink <линия>                помигать линией 3 раза, чтобы найти её на площадке
КОРИДОР
  corr min <кВт> [мин]         переопределить нижнюю границу (на N игровых минут или до reset)
  corr max <кВт> [мин]         переопределить верхнюю границу
  corr reset [min|max]         вернуть значение из сценария
АККУМУЛЯТОРЫ
  bat <ID> поле=значение ...   например: bat A soc=50 max_discharge_kw=3 status=ok
  bat fields                   список изменяемых полей
  active <ID>                  принудительно сменить активный АКБ
КРИЗИС
  crisis pause|resume|cancel   нарушения коридора/дефицит: заморозить отсчёт и наказания /
                               продолжить / отменить (сброс отсчёта, последствий, аварийных отключений)
  phase pause|resume|cancel    текущая фаза сценария (коридор, солнце, блокировки):
                               пауза до resume / продолжить / отменить до конца дня
ГЕНЕРАЦИЯ
  solar <0.0-2.0>              масштаб генерации, умножается на коэффициент фазы
ТЕРМИНАЛ
  msg [INFO|WARN|CRIT|OK] <текст>  сообщение в терминал участников (пишите по-английски)
ПРОЧЕЕ
  time ЧЧ:ММ                   перемотать игровое время (только clock.mode: sim)
  help | quit                  ↑/↓ — история команд, Ctrl+Q — выход

Действия консоли участникам не объявляются — при необходимости используйте msg."""

BATTERY_FIELDS = (
    ("soc", "заряд в % от ёмкости"),
    ("charge_kwh", "заряд, кВт·ч"),
    ("capacity_kwh", "текущая ёмкость, кВт·ч"),
    ("nominal_kwh", "паспортная ёмкость, кВт·ч"),
    ("max_charge_kw", "макс. мощность заряда, кВт"),
    ("max_discharge_kw", "номинальная мощность разряда, кВт"),
    ("peak_discharge_kw", "пиковая мощность разряда, кВт"),
    ("efficiency", "КПД заряда, 0–1"),
    ("self_discharge_pct_h", "саморазряд, % ёмкости в час"),
    ("overload_tolerance_s", "сколько секунд терпит разряд выше номинала"),
    ("fault_recovery_min", "длительность защиты, мин"),
    ("status", "ok | degraded | fault | failed"),
    ("name", "название (без пробелов)"),
    ("description", "описание (без пробелов)"),
)
LEVELS = ("INFO", "WARN", "CRIT", "OK")
ACTIONS = {"on": "включена", "off": "выключена", "lock": "заблокирована", "unlock": "разблокирована"}
CRISIS_DONE = {
    "crisis": {"pause": "Нарушения на паузе: отсчёт и наказания заморожены",
               "resume": "Отслеживание нарушений возобновлено",
               "cancel": "Кризис отменён: отсчёт, последствия и аварийные отключения сняты"},
    "phase": {"pause": "Фаза сценария приостановлена (действуют значения по умолчанию)",
              "resume": "Фаза сценария возобновлена",
              "cancel": "Фаза сценария отменена до конца дня"},
}


class CommandError(Exception):
    pass


class Quit(Exception):
    pass


def _num(s: str, what: str) -> float:
    try:
        return float(s.replace(",", "."))
    except ValueError:
        raise CommandError(f"{what}: '{s}' — не число") from None


def parse_lines(spec: str, state: dict) -> list[int]:
    lines = [l["id"] for l in state["state"]["lines"]]
    keys = {c["key"]: c["line"] for c in state["admin"]["lines_map"]["consumers"]}
    if spec.lower() == "all":
        return lines
    out = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        a, sep, b = part.partition("-")
        if sep and a.isdigit() and b.isdigit():
            out += [l for l in range(int(a), int(b) + 1) if l in lines]
        elif part.isdigit():
            if int(part) not in lines:
                raise CommandError(f"Нет линии {part}")
            out.append(int(part))
        else:
            key = part.upper()
            if key not in keys:
                raise CommandError(f"Неизвестная линия или потребитель '{part}'")
            if keys[key] is None:
                raise CommandError(f"Потребитель {key} не привязан ни к одной линии")
            out.append(keys[key])
    if not out:
        raise CommandError("Не указаны линии")
    return list(dict.fromkeys(out))


def _call(api: AdminClient, path: str, body: dict) -> None:
    try:
        api.post(path, body)
    except ApiError as e:
        raise CommandError(str(e)) from None


def execute(text: str, api: AdminClient, state: dict | None) -> list[str]:
    args = text.split()
    if not args:
        return []
    cmd, rest = args[0].lower(), args[1:]

    if cmd in ("help", "?", "h"):
        return ["Справка — F1"]
    if cmd in ("quit", "exit", "q"):
        raise Quit()
    if state is None:
        raise CommandError("Ещё нет данных от сервера")
    name = {l["id"]: l["name"] for l in state["state"]["lines"]}

    if cmd in ("on", "off", "lock", "unlock"):
        if not rest:
            raise CommandError(f"Формат: {cmd} <L>")
        ids = parse_lines(rest[0], state)
        body = {"action": cmd}
        if cmd == "lock":
            if len(rest) > 1:
                try:
                    body["minutes"] = float(rest[1]) or None
                    reason = rest[2:]
                except ValueError:
                    reason = rest[1:]
            else:
                reason = []
            if reason:
                body["reason"] = " ".join(reason)
        out, errors = [], []
        for lid in ids:
            try:
                _call(api, "line", {"line": lid, **body})
                out.append(f"L{lid} {name[lid]}: {ACTIONS[cmd]}")
            except CommandError as e:
                errors.append(f"[red]L{lid} {name[lid]}: {e}[/red]")
        if len(ids) > 3 and not errors:
            return [f"Линии {', '.join(map(str, ids))}: {ACTIONS[cmd]}"]
        return out + errors

    if cmd == "name":
        if len(rest) != 2:
            raise CommandError("Формат: name <линия> <КЛЮЧ|->")
        if not rest[0].isdigit():
            raise CommandError("Первый аргумент — номер линии")
        key = None if rest[1] in ("-", "none") else rest[1].upper()
        _call(api, "line/name", {"line": int(rest[0]), "consumer": key})
        return [f"L{rest[0]} → {key or '(без потребителя)'}"]

    if cmd == "names":
        out = []
        for c in state["admin"]["lines_map"]["consumers"]:
            out.append(f"  {c['key']:<7} {c['name']:<26} линия {c['line'] if c['line'] else '—'}")
        return out

    if cmd == "blink":
        if len(rest) != 1:
            raise CommandError("Формат: blink <линия>")
        lid = parse_lines(rest[0], state)[0]
        _call(api, "line/identify", {"line": lid})
        return [f"L{lid}: мигает 3 раза"]

    if cmd in ("corr", "corridor"):
        if not rest:
            raise CommandError("Формат: corr min|max <кВт> [мин]  |  corr reset [min|max]")
        sub = rest[0].lower()
        if sub == "reset":
            bound = rest[1].lower() if len(rest) > 1 else "all"
            if bound not in ("min", "max", "all"):
                raise CommandError("Формат: corr reset [min|max]")
            _call(api, "corridor", {"bound": bound, "value": None})
            what = {"min": "нижняя граница", "max": "верхняя граница", "all": "обе границы"}[bound]
            return [f"Коридор: {what} — из сценария"]
        if sub not in ("min", "max") or len(rest) < 2:
            raise CommandError("Формат: corr min|max <кВт> [мин]")
        value = _num(rest[1], "кВт")
        minutes = _num(rest[2], "минуты") if len(rest) > 2 else None
        _call(api, "corridor", {"bound": sub, "value": value, "minutes": minutes or None})
        return [f"Коридор {sub} = {value:.2f} кВт" + (f" на {minutes:g} мин" if minutes else " до reset")]

    if cmd in ("bat", "battery"):
        if rest and rest[0].lower() == "fields":
            return ["Изменяемые поля:"] + [f"  {k:<22} {d}" for k, d in BATTERY_FIELDS]
        if len(rest) < 2:
            raise CommandError("Формат: bat <ID> поле=значение ...   (bat fields — список полей)")
        bid = rest[0].upper()
        fields = {}
        for kv in rest[1:]:
            k, sep, v = kv.partition("=")
            if not sep:
                raise CommandError(f"'{kv}': ожидается поле=значение")
            k = k.strip().lower()
            fields[k] = v.strip() if k in ("name", "description") else v.strip().replace(",", ".")
        _call(api, "battery", {"id": bid, "fields": fields})
        return [f"АКБ {bid}: " + ", ".join(f"{k}={v}" for k, v in fields.items())]

    if cmd == "active":
        if len(rest) != 1:
            raise CommandError("Формат: active <ID>")
        _call(api, "battery/active", {"id": rest[0].upper()})
        return [f"Активный АКБ: {rest[0].upper()}"]

    if cmd in ("crisis", "phase"):
        if len(rest) != 1 or rest[0].lower() not in ("pause", "resume", "cancel"):
            raise CommandError(f"Формат: {cmd} pause|resume|cancel")
        _call(api, cmd, {"action": rest[0].lower()})
        return [CRISIS_DONE[cmd][rest[0].lower()]]

    if cmd == "solar":
        if len(rest) != 1:
            raise CommandError("Формат: solar <0.0-2.0>")
        f = _num(rest[0], "коэффициент")
        _call(api, "solar", {"factor": f})
        return [f"Масштаб генерации = {f:.2f}"]

    if cmd in ("msg", "say"):
        if not rest:
            raise CommandError("Формат: msg [INFO|WARN|CRIT|OK] <текст>")
        body = text.split(None, 1)[1].strip()
        first, _, tail = body.partition(" ")
        level = "INFO"
        if first.upper() in LEVELS and tail.strip():
            level, body = first.upper(), tail.strip()
        _call(api, "message", {"text": body, "level": level})
        return [f"Отправлено в терминал [{level}]"]

    if cmd == "time":
        if len(rest) != 1:
            raise CommandError("Формат: time ЧЧ:ММ")
        _call(api, "time", {"time": rest[0]})
        return [f"Игровое время → {rest[0]}"]

    raise CommandError(f"Неизвестная команда '{cmd}'. Справка — F1.")
