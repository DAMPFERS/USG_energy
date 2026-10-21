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
  unlock <L>                   снять блокировку: админа или после дефицита
  prio <линия> <1-13>          приоритет линии (13 отключается первой); линии меняются приоритетами
  prio                         список приоритетов
  name <линия> <КЛЮЧ|->        привязать потребителя к линии (если ключ занят — линии меняются)
  names                        список потребителей и их линий
  blink <линия>                помигать линией 3 раза, чтобы найти её на площадке
ГРАНИЦЫ И ПРАВИЛА (сохраняются в config/server.yaml)
  max <кВт>                    верхняя граница коридора потребления
  set поле=значение ...        правила, например: set overload_grace_min=5 battery_off_min=15
  set                          список правил и текущих значений
АККУМУЛЯТОР
  bat поле=значение ...        параметры (сохраняются в server.yaml): capacity_kwh, initial_kwh,
                               max_charge_kw, max_discharge_kw; состояние: soc=50 (%) или charge_kwh=10
  bat off [мин]                отключить АКБ (на N игровых минут или до bat on)
  bat on                       подключить АКБ
  bat                          параметры АКБ
КРИЗИС
  crisis pause|resume|cancel   нарушения: заморозить отслеживание / продолжить / отменить
                               (сброс отсчётов, снятие блокировок после дефицита, подключение АКБ)
ГЕНЕРАЦИЯ
  solar <0.0-2.0>              масштаб генерации относительно прогноза (буря — 0.3)
ТЕРМИНАЛ
  msg [INFO|WARN|CRIT|OK] <текст>  сообщение в терминал участников (пишите по-английски)
ПРОЧЕЕ
  time <сол> ЧЧ:ММ             перемотать время купола, например time 2 13:30 (только clock.mode: sim)
  help | quit                  ↑/↓ — история команд, Ctrl+Q — выход

Действия консоли участникам не объявляются — при необходимости используйте msg."""

BATTERY_STATE = ("soc", "charge_kwh")
LEVELS = ("INFO", "WARN", "CRIT", "OK")
ACTIONS = {"on": "включена", "off": "выключена", "lock": "заблокирована", "unlock": "разблокирована"}
CRISIS_DONE = {"pause": "Отслеживание нарушений на паузе",
               "resume": "Отслеживание нарушений возобновлено",
               "cancel": "Кризис отменён: отсчёты сброшены, блокировки после дефицита сняты, АКБ подключён"}


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


def _kv(args: list[str]) -> dict:
    out = {}
    for kv in args:
        k, sep, v = kv.partition("=")
        if not sep:
            raise CommandError(f"'{kv}': ожидается поле=значение")
        out[k.strip().lower()] = v.strip().replace(",", ".")
    return out


def _fields(section: dict, values: dict) -> list[str]:
    return [f"  {k:<20} {values[k]:<8g} {d}" for k, d in section.items()]


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
    settings = state["admin"]["settings"]

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

    if cmd in ("prio", "priority"):
        if not rest:
            lines = sorted(state["state"]["lines"], key=lambda l: -l["priority"])
            return ["Приоритеты (первой отключается верхняя):"] + [
                f"  #{l['priority']:<3} L{l['id']:<3} {l['name']}" for l in lines]
        if len(rest) != 2:
            raise CommandError("Формат: prio <линия> <1-13>")
        lid = parse_lines(rest[0], state)[0]
        prio = int(_num(rest[1], "приоритет"))
        _call(api, "line/priority", {"line": lid, "priority": prio})
        return [f"L{lid} {name[lid]}: приоритет #{prio}"]

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

    if cmd in ("max", "corr"):
        if cmd == "corr" and rest[:1] == ["max"]:
            rest = rest[1:]
        if len(rest) != 1:
            raise CommandError("Формат: max <кВт>")
        value = _num(rest[0], "кВт")
        _call(api, "settings", {"section": "rules", "values": {"max_kw": value}})
        return [f"Верхняя граница = {value:.2f} кВт (сохранено в server.yaml)"]

    if cmd == "set":
        if not rest:
            return ["Правила:"] + _fields(settings["fields"]["rules"], settings["rules"])
        values = _kv(rest)
        _call(api, "settings", {"section": "rules", "values": values})
        return ["Правила: " + ", ".join(f"{k}={v}" for k, v in values.items()) + " (сохранено в server.yaml)"]

    if cmd in ("bat", "battery"):
        if not rest or rest[0].lower() == "fields":
            return [f"АКБ «{settings['battery']['name']}»:"] + _fields(settings["fields"]["battery"],
                                                                       settings["battery"]) + \
                   ["  состояние: soc=<%> или charge_kwh=<кВт·ч>; bat off [мин] / bat on"]
        sub = rest[0].lower()
        if sub in ("on", "off"):
            body = {"connect": sub == "on"}
            if sub == "off" and len(rest) > 1:
                body["minutes"] = _num(rest[1], "минуты")
            _call(api, "battery", body)
            if sub == "on":
                return ["АКБ подключён"]
            return [f"АКБ отключён" + (f" на {body['minutes']:g} мин" if body.get("minutes") else " до bat on")]
        values = _kv(rest)
        st = {k: _num(values.pop(k), k) for k in BATTERY_STATE if k in values}
        if values:
            _call(api, "settings", {"section": "battery", "values": values})
        if st:
            _call(api, "battery", st)
        return ["АКБ: " + ", ".join(f"{k}={v}" for k, v in {**values, **st}.items())
                + (" (параметры сохранены в server.yaml)" if values else "")]

    if cmd == "crisis":
        if len(rest) != 1 or rest[0].lower() not in CRISIS_DONE:
            raise CommandError("Формат: crisis pause|resume|cancel")
        _call(api, "crisis", {"action": rest[0].lower()})
        return [CRISIS_DONE[rest[0].lower()]]

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
        if len(rest) not in (1, 2):
            raise CommandError("Формат: time <сол> ЧЧ:ММ")
        _call(api, "time", {"time": " ".join(rest)})
        return [f"Время купола → {' '.join(rest)}"]

    raise CommandError(f"Неизвестная команда '{cmd}'. Справка — F1.")
