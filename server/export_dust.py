"""
Выгрузка запылённости атмосферы для коллег из георазведки (по ней они готовят спутниковые снимки):

    python -m server.export_dust                         # шаг 90 мин → dust_export.csv
    python -m server.export_dust --step 45 --out dust.csv

Строки — от начала до конца игры с заданным шагом: номер снимка, марсианское время (сол, ЧЧ:ММ),
земное время и запылённость, % (0 — ясно, 100 — пылевая буря, генерация 0).
"""
import argparse
import csv
import os

import yaml

from .scenario import load_scenario
from .solar import DustSeries

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def p(path):
    return path if os.path.isabs(path) else os.path.join(ROOT, path)


def main():
    ap = argparse.ArgumentParser(description="Выгрузка запылённости для спутниковых снимков")
    ap.add_argument("--config", default="config/server.yaml")
    ap.add_argument("--scenario", help="переопределить сценарий из конфига")
    ap.add_argument("--step", type=float, default=90, help="шаг, минуты (по умолчанию 90)")
    ap.add_argument("--out", default="dust_export.csv")
    args = ap.parse_args()

    with open(p(args.config), encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    sc = load_scenario(p(args.scenario or cfg["scenario"]))
    dust = DustSeries(sc.dust_csv, sc.mars.sol_min)
    m = sc.mars

    rows, t, n = [], sc.start_t, 1
    while t <= sc.end_t + 1e-9:
        rows.append([n, m.sol(t), m.hm(t), m.dt(t).strftime("%Y-%m-%d %H:%M"), f"{dust.at(t):.0f}"])
        t += args.step
        n += 1
    with open(args.out, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["n", "sol", "mars_time", "earth_time", "dust_pct"])
        w.writerows(rows)
    print(f"{args.out}: {len(rows)} строк, шаг {args.step:g} мин, {m.label(sc.start_t)} – {m.label(sc.end_t)}")


if __name__ == "__main__":
    main()
