# МКИ 2026 — энергосистема купола

Сервер игрового кейса «Энергетика»: агрегирует умные реле (12 линий), моделирует
солнечную генерацию и аккумуляторы, ведёт сценарий дня и отдаёт веб-панель участникам.

## Запуск

```
pip install -r requirements.txt
python -m server              # http://<ip-сервера>:8000
python -m server --reset      # начать игровой день заново
python -m server --scenario config/scenarios/day2.yaml
```

Для отладки в `config/server.yaml` стоит `clock.mode: sim` (день идёт ×20).
На игре поставьте `clock.mode: real`.

## Структура

```
config/server.yaml          сервер: порт, часы, линии (реле), источник генерации
config/scenarios/day1.yaml  сценарий дня: фазы, коридор, АКБ, события, последствия
data/forecast_day1.csv      прогноз солнца: time,forecast_kw,sigma_kw
server/
  relays.py    опрос реле в отдельном потоке (tuya — реальные, sim — эмулятор)
  solar.py     прогноз из CSV + генерация в отдельном потоке (прогноз × factor + шум)
  battery.py   модель АКБ (ёмкость, мощность, пик, перегруз → защита, деградация)
  scenario.py  загрузка сценария
  engine.py    игровой движок: баланс, коридор, штрафы, события, история, сохранение
  terminal.py  журнал сообщений терминала (logs/terminal_<день>.log)
  app.py       HTTP API
  telemetry.py телеметрия для ЦУП (REST + WebSocket, см. TELEMETRY.md)
static/       веб-панель участника
logs/         terminal_*.log, telemetry_*.csv (история), actions_*.log (действия), state_*.json
```

## Игровая механика

* **Баланс**: генерация − потребление идёт в **активный АКБ** (выбирают участники).
  Излишек сверх `max_charge_kw` или при полном АКБ теряется; дефицит покрывается разрядом
  до `peak_discharge_kw`. Работа выше `max_discharge_kw` дольше `overload_tolerance_s` →
  АКБ уходит в защиту на `fault_recovery_min`.
* **Коридор** `[min_kw, max_kw]` — по суммарному потреблению линий.
  Выход за границы → предупреждение в терминале и обратный отсчёт `grace_s` →
  последствие (`on_overload` / `on_underload` / `on_deficit`): отключение линий
  (`auto`, `all`, `top:N`, список) на `duration_min`. Пока последствие действует, такое же
  нарушение повторно не наказывается.
* **Блокировки линий**: `locked_lines` в фазе (на всю фазу) и `lock_lines` в событии
  (на `duration_min`). Заблокированную линию участники включить не могут; если её
  включили физически — сервер выключит.
* **Перезапуск**: состояние (АКБ, блокировки, сработавшие события, статистика) и
  история сохраняются в `logs/` — после перезапуска игра продолжится.

## API для организаторов

```
POST /api/admin/message  {"text": "...", "level": "INFO|WARN|CRIT|OK"}   X-Admin-Token: <admin_token>
POST /api/admin/time     {"time": "13:30"}   перемотка (только clock.mode: sim)
```

## Телеметрия для ЦУП

Только чтение, в Вт / А / А·ч: `WS /ws/telemetry` (поток раз в секунду) и
`GET /api/telemetry`, `/api/telemetry/meta`, `/api/telemetry/forecast`, `/api/telemetry/history`.
Настройки — раздел `telemetry` в `config/server.yaml`, описание протокола — [TELEMETRY.md](TELEMETRY.md).

## Реальные реле

В `config/server.yaml` у линии: `driver: tuya`, `tuya: {device_id, local_key, ip}`
(из `smart_rele/devices.json`). `scale` переводит реальные Вт в игровые кВт.
Реле, которое не отвечает, отображается как «НЕТ СВЯЗИ» и в сумму не входит.

## Реальные солнечные панели

Сделайте класс с методами `start()`, `stop()`, `power_kw()`, `set_factor(f)` (см.
`SimSolarSource` в `server/solar.py`) и подключите его в `server/__main__.py` по `solar.source`.
