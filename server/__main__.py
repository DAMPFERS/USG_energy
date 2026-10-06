"""
Запуск сервера энергосистемы купола:

    python -m server                          # config/server.yaml
    python -m server --config my.yaml
    python -m server --scenario config/scenarios/day2.yaml
    python -m server --reset                  # начать день заново (стереть логи/состояние этого дня)
"""
import argparse
import logging
import os
import sys
from datetime import date

import uvicorn
import yaml

from .app import create_app
from .clock import GameClock
from .engine import Engine
from .relays import RelayManager, build_lines
from .scenario import load_scenario
from .solar import Forecast, SimSolarSource
from .terminal import Terminal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def p(path):
    return path if os.path.isabs(path) else os.path.join(ROOT, path)


def main():
    ap = argparse.ArgumentParser(description="Сервер энергосистемы купола")
    ap.add_argument("--config", default="config/server.yaml")
    ap.add_argument("--scenario", help="переопределить сценарий из конфига")
    ap.add_argument("--reset", action="store_true", help="стереть состояние, историю и терминал этого дня")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])

    with open(p(args.config), encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    sc = load_scenario(p(args.scenario or cfg["scenario"]))
    logs_dir = p(cfg.get("logs_dir", "logs"))
    os.makedirs(logs_dir, exist_ok=True)
    logging.getLogger().addHandler(logging.FileHandler(os.path.join(logs_dir, "server.log"), encoding="utf-8"))

    if args.reset:
        for name in (f"state_{sc.id}.json", f"telemetry_{sc.id}.csv", f"terminal_{sc.id}.log",
                     f"actions_{sc.id}.log"):
            fp = os.path.join(logs_dir, name)
            if os.path.exists(fp):
                os.remove(fp)
        logging.info("День %s сброшен", sc.id)

    ck = cfg.get("clock", {}) or {}
    clock = GameClock(ck.get("mode", "real"), ck.get("start"), ck.get("speed", 1.0),
                      date.fromisoformat(sc.date) if sc.date else None)

    relays = RelayManager(build_lines(cfg["lines"]), float(cfg.get("relay_poll_s", 1.0)))
    forecast = Forecast(sc.forecast_csv)
    sol = cfg.get("solar", {}) or {}
    if sol.get("source", "sim") != "sim":
        raise SystemExit(f"Неизвестный источник генерации: {sol.get('source')}")
    solar = SimSolarSource(forecast, clock, float(sol.get("interval_s", 1.0)), float(sol.get("noise", 1.0)),
                           float(sol.get("correlation", 0.97)))
    terminal = Terminal(os.path.join(logs_dir, f"terminal_{sc.id}.log"))
    engine = Engine(sc, clock, relays, solar, forecast, terminal, logs_dir, float(cfg.get("engine_tick_s", 1.0)))

    relays.start()
    solar.start()
    engine.start()
    logging.info("Сценарий: %s (%s), часы: %s x%.1f", sc.day, sc.id, clock.mode, clock.speed)

    app = create_app(engine, p(cfg.get("static_dir", "static")), cfg.get("admin_token"))
    try:
        uvicorn.run(app, host=cfg.get("host", "0.0.0.0"), port=int(cfg.get("port", 8000)), log_level="warning")
    finally:
        engine.stop()
        solar.stop()
        relays.stop()


if __name__ == "__main__":
    main()
