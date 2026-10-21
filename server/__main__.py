"""
Запуск сервера энергосистемы купола:

    python -m server                          # config/server.yaml
    python -m server --config my.yaml
    python -m server --scenario config/scenarios/mission.yaml
    python -m server --reset                  # начать игру заново (стереть логи/состояние этого сценария)
"""
import argparse
import logging
import os
import sys

import uvicorn
import yaml

from .app import create_app
from .clock import GameClock
from .engine import Engine
from .linemap import LineMap
from .relays import RelayManager, build_lines
from .scenario import load_scenario
from .settings import Settings
from .solar import Forecast, SimSolarSource
from .terminal import Terminal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def p(path):
    return path if os.path.isabs(path) else os.path.join(ROOT, path)


def main():
    ap = argparse.ArgumentParser(description="Сервер энергосистемы купола")
    ap.add_argument("--config", default="config/server.yaml")
    ap.add_argument("--scenario", help="переопределить сценарий из конфига")
    ap.add_argument("--reset", action="store_true", help="стереть состояние, историю и терминал этого сценария")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])

    cfg_path = p(args.config)
    with open(cfg_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    sc = load_scenario(p(args.scenario or cfg["scenario"]))
    logs_dir = p(cfg.get("logs_dir", "logs"))
    os.makedirs(logs_dir, exist_ok=True)
    logging.getLogger().addHandler(logging.FileHandler(os.path.join(logs_dir, "server.log"), encoding="utf-8"))

    clock_path = os.path.join(logs_dir, f"clock_{sc.id}.json")
    if args.reset:
        for name in (f"state_{sc.id}.json", f"telemetry_{sc.id}.csv", f"terminal_{sc.id}.log",
                     f"actions_{sc.id}.log", f"clock_{sc.id}.json"):
            fp = os.path.join(logs_dir, name)
            if os.path.exists(fp):
                os.remove(fp)
        logging.info("Игра %s сброшена", sc.id)

    ck = cfg.get("clock", {}) or {}
    sim_start = sc.mars.dt(sc.mars.parse(ck["start"]) if ck.get("start") else sc.start_t - 2)
    clock = GameClock(ck.get("mode", "real"), ck.get("speed", 1.0), sim_start, clock_path)

    line_cfgs = build_lines(cfg["lines"])
    relays = RelayManager(line_cfgs, float(cfg.get("relay_poll_s", 1.0)))
    linemap = LineMap([l.id for l in line_cfgs], cfg.get("consumers"), p(cfg.get("line_map", "config/line_map.yaml")),
                      {l.id: l.name for l in line_cfgs})
    forecast = Forecast(sc.forecast_csv, sc.mars.sol_min)
    sol = cfg.get("solar", {}) or {}
    if sol.get("source", "sim") != "sim":
        raise SystemExit(f"Неизвестный источник генерации: {sol.get('source')}")
    solar = SimSolarSource(forecast, lambda: sc.mars.t(clock.now()), float(sol.get("interval_s", 1.0)),
                           float(sol.get("noise", 1.0)), float(sol.get("correlation", 0.97)))
    terminal = Terminal(os.path.join(logs_dir, f"terminal_{sc.id}.log"),
                        lambda dt: sc.mars.label(sc.mars.t(dt), seconds=True))
    settings = Settings(cfg, cfg_path)
    engine = Engine(sc, clock, relays, solar, forecast, terminal, linemap, settings, logs_dir,
                    float(cfg.get("engine_tick_s", 1.0)))

    relays.start()
    solar.start()
    engine.start()
    logging.info("Сценарий: %s (%s), %s – %s, часы: %s x%.1f", sc.name, sc.id, sc.mars.label(sc.start_t),
                 sc.mars.label(sc.end_t), clock.mode, clock.speed)

    app = create_app(engine, p(cfg.get("static_dir", "static")), cfg.get("admin_token"),
                     cfg.get("telemetry") or {})
    try:
        uvicorn.run(app, host=cfg.get("host", "0.0.0.0"), port=int(cfg.get("port", 8000)), log_level="warning")
    finally:
        engine.stop()
        solar.stop()
        relays.stop()


if __name__ == "__main__":
    main()
