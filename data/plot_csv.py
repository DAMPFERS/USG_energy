"""
Графики исходных данных энергосистемы купола.

    python data/plot_csv.py
    python data/plot_csv.py --out-dir data/plots --dpi 160
    python data/plot_csv.py --factor 0.5
    python data/plot_csv.py --show

Строит три графика:
  1. солнечный прогноз из data/forecast.csv (чистое небо, кВт);
  2. запылённость из data/dust.csv (%);
  3. итоговую генерацию = солнце × (1 − пыль / 100) × factor.

Дополнительно сохраняет сводный лист с тремя панелями.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import matplotlib

WANT_SHOW = "--show" in sys.argv
if WANT_SHOW:
    try:
        matplotlib.use("TkAgg")
    except Exception:
        matplotlib.use("Agg")
else:
    matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.ticker import FuncFormatter

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.clock import SOL_MIN  # noqa: E402
from server.solar import DustSeries, Forecast  # noqa: E402

BG = "#0e0d14"
CARD = "#12111a"
GRID = "#24222f"
TEXT = "#f1f0f5"
MUTED = "#8c8a99"
ORANGE = "#ff8c3a"
PURPLE = "#b58cff"
GREEN = "#2fbf71"


def sol_label(t: float) -> str:
    """Минуты от Сола 1 00:00 → «Sol 2 14:05»."""
    n = int(t // SOL_MIN)
    m = t - n * SOL_MIN
    hh = int(m // 60)
    mm = int(round(m - hh * 60))
    if mm == 60:
        hh, mm = hh + 1, 0
    return f"Sol {n + 1} {hh:02d}:{mm:02d}"


def setup_style():
    plt.rcParams.update({
        "figure.facecolor": BG,
        "savefig.facecolor": BG,
        "axes.facecolor": CARD,
        "axes.edgecolor": GRID,
        "axes.labelcolor": TEXT,
        "axes.titlecolor": TEXT,
        "text.color": TEXT,
        "xtick.color": MUTED,
        "ytick.color": MUTED,
        "grid.color": GRID,
        "grid.alpha": 0.7,
        "font.family": "DejaVu Sans",
        "font.size": 10,
        "legend.facecolor": CARD,
        "legend.edgecolor": GRID,
        "legend.labelcolor": TEXT,
    })


def time_ticks(t0: float, t1: float) -> list[float]:
    """Каждые 6 часов внутри каждого сола + границы солов."""
    ticks = []
    n = 1
    while (n - 1) * SOL_MIN <= t1:
        base = (n - 1) * SOL_MIN
        for hour in (0, 6, 12, 18):
            tick = base + hour * 60
            if t0 - 1e-6 <= tick <= t1 + 1e-6:
                ticks.append(float(tick))
        n += 1
    return ticks or [t0, t1]


def style_time_axis(ax, t0: float, t1: float):
    ax.set_xlim(t0, t1)
    ax.set_xticks(time_ticks(t0, t1))
    ax.xaxis.set_major_formatter(FuncFormatter(lambda x, _pos: sol_label(x)))
    ax.grid(True, axis="y", linestyle="-", linewidth=0.6)
    ax.grid(True, axis="x", linestyle=":", linewidth=0.5, alpha=0.35)
    for n in range(1, int(t1 // SOL_MIN) + 1):
        boundary = n * SOL_MIN
        if t0 < boundary < t1:
            ax.axvline(boundary, color=MUTED, linewidth=0.8, linestyle="--", alpha=0.6)
    ax.tick_params(axis="x", labelsize=8.5)
    for label in ax.get_xticklabels():
        label.set_rotation(25)
        label.set_ha("right")


def energy_kwh(y: np.ndarray, t: np.ndarray) -> float:
    """Энергия по точкам мощности (кВт) и времени (мин), кВт·ч."""
    if len(t) < 2:
        return 0.0
    return float(np.sum((y[1:] + y[:-1]) * 0.5 * np.diff(t))) / 60.0


def save(fig, out_dir: Path, name: str, dpi: int) -> Path:
    path = out_dir / name
    fig.savefig(path, dpi=dpi, bbox_inches="tight", facecolor=BG)
    return path


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Графики forecast.csv / dust.csv и итоговой генерации")
    ap.add_argument("--forecast", default=str(ROOT / "data" / "forecast.csv"),
                    help="CSV солнечного прогноза (sol,time,forecast_kw)")
    ap.add_argument("--dust", default=str(ROOT / "data" / "dust.csv"),
                    help="CSV запылённости (sol,time,dust_pct)")
    ap.add_argument("--out-dir", default=str(ROOT / "data" / "plots"),
                    help="куда сохранять PNG")
    ap.add_argument("--dpi", type=int, default=150, help="разрешение PNG (по умолчанию 150)")
    ap.add_argument("--factor", type=float, default=1.0,
                    help="коэффициент панелей (solar) для итогового графика; по умолчанию 1.0")
    ap.add_argument("--fluctuation-pct", type=float, default=5.0,
                    help="полоса прогноза ±%% на первом графике (по умолчанию 5)")
    ap.add_argument("--show", action="store_true", help="открыть графики в окне после сохранения")
    args = ap.parse_args()

    if args.factor < 0:
        raise SystemExit("--factor должен быть >= 0")
    if args.fluctuation_pct < 0:
        raise SystemExit("--fluctuation-pct должен быть >= 0")

    forecast_path = Path(args.forecast).expanduser()
    dust_path = Path(args.dust).expanduser()
    out_dir = Path(args.out_dir).expanduser()
    if not forecast_path.exists():
        raise SystemExit(f"Не найден файл прогноза: {forecast_path}")
    if not dust_path.exists():
        raise SystemExit(f"Не найден файл запылённости: {dust_path}")

    logging.getLogger("solar").setLevel(logging.WARNING)
    forecast = Forecast(str(forecast_path), SOL_MIN, args.fluctuation_pct)
    dust = DustSeries(str(dust_path), SOL_MIN)

    t = np.asarray(forecast.t, dtype=float)
    sun = np.asarray(forecast.mean, dtype=float)
    dust_pct = np.asarray([dust.at(x) for x in t], dtype=float)
    gen = np.clip(sun * (1.0 - dust_pct / 100.0) * args.factor, 0.0, None)
    t0, t1 = float(t[0]), float(t[-1])
    band = sun * max(0.0, args.fluctuation_pct) / 100.0
    peak = float(gen.max()) if len(gen) else 0.0
    energy = energy_kwh(gen, t)

    setup_style()
    out_dir.mkdir(parents=True, exist_ok=True)
    figs = []

    # 1. солнце
    fig, ax = plt.subplots(figsize=(15, 6.5), constrained_layout=True)
    ax.plot(t, sun, color=ORANGE, linewidth=2.2, label="Солнце (чистое небо)")
    ax.fill_between(t, np.maximum(0, sun - band), sun + band, color=ORANGE, alpha=0.16,
                    label=f"±{args.fluctuation_pct:g} %")
    ax.set_title("1. Солнечный прогноз — data/forecast.csv", fontsize=14, pad=12)
    ax.set_ylabel("Мощность, кВт")
    ax.set_xlabel("Время купола")
    style_time_axis(ax, t0, t1)
    ax.legend(loc="upper left")
    figs.append((fig, "01_sun_forecast.png"))

    # 2. запылённость
    fig, ax = plt.subplots(figsize=(15, 6.5), constrained_layout=True)
    ax.plot(t, dust_pct, color=PURPLE, linewidth=2.0, label="Запылённость (линейная интерполяция)")
    ax.scatter(dust.t, dust.v, color=PURPLE, edgecolor=TEXT, linewidth=0.4, s=22, zorder=3,
               label="Точки data/dust.csv")
    ax.set_ylim(0, 105)
    ax.set_title("2. Запылённость атмосферы — data/dust.csv", fontsize=14, pad=12)
    ax.set_ylabel("Запылённость, %")
    ax.set_xlabel("Время купола")
    style_time_axis(ax, t0, t1)
    ax.legend(loc="upper left")
    figs.append((fig, "02_dust.png"))

    # 3. итоговая генерация
    fig, ax = plt.subplots(figsize=(15, 6.5), constrained_layout=True)
    ax.plot(t, gen, color=GREEN, linewidth=2.4, label="Итоговая генерация")
    ax.plot(t, sun, color=ORANGE, linewidth=1.2, linestyle="--", alpha=0.75,
            label="Солнце без пыли")
    ax.fill_between(t, 0, gen, color=GREEN, alpha=0.14)
    title = "3. Итоговая генерация = солнце × (1 − пыль / 100)"
    if args.factor != 1.0:
        title += f" × {args.factor:g}"
    ax.set_title(title, fontsize=14, pad=12)
    ax.set_ylabel("Мощность, кВт")
    ax.set_xlabel("Время купола")
    style_time_axis(ax, t0, t1)
    ax.text(0.01, 0.97,
            f"Пик: {peak:.2f} кВт\nЭнергия за период: {energy:.1f} кВт·ч\nfactor: {args.factor:g}",
            transform=ax.transAxes, va="top", ha="left", fontsize=10, color=MUTED,
            bbox={"boxstyle": "round,pad=0.4", "facecolor": CARD, "edgecolor": GRID})
    ax.legend(loc="upper right")
    figs.append((fig, "03_generation.png"))

    # 4. сводный лист
    fig, axes = plt.subplots(3, 1, figsize=(15, 11), sharex=True, constrained_layout=True)
    axes[0].plot(t, sun, color=ORANGE, linewidth=2.0)
    axes[0].fill_between(t, np.maximum(0, sun - band), sun + band, color=ORANGE, alpha=0.16)
    axes[0].set_ylabel("Солнце, кВт")
    axes[0].set_title("1. Солнечный прогноз", fontsize=12, loc="left")

    axes[1].plot(t, dust_pct, color=PURPLE, linewidth=2.0)
    axes[1].scatter(dust.t, dust.v, color=PURPLE, edgecolor=TEXT, linewidth=0.4, s=18, zorder=3)
    axes[1].set_ylim(0, 105)
    axes[1].set_ylabel("Пыль, %")
    axes[1].set_title("2. Запылённость", fontsize=12, loc="left")

    axes[2].plot(t, gen, color=GREEN, linewidth=2.4)
    axes[2].fill_between(t, 0, gen, color=GREEN, alpha=0.14)
    axes[2].set_ylabel("Генерация, кВт")
    axes[2].set_title("3. Итоговая генерация", fontsize=12, loc="left")
    axes[2].set_xlabel("Время купола")
    for ax in axes:
        style_time_axis(ax, t0, t1)
    for ax in axes[:-1]:
        ax.tick_params(labelbottom=False)
    fig.suptitle("Исходные данные и итоговая генерация", fontsize=16)
    figs.append((fig, "00_summary.png"))

    print(f"Прогноз: {forecast_path} ({len(t)} точек)")
    print(f"Пыль:    {dust_path} ({len(dust.t)} точек)")
    print(f"Период:  {sol_label(t0)} – {sol_label(t1)}")
    print(f"Пик генерации: {peak:.2f} кВт; энергия: {energy:.1f} кВт·ч (factor={args.factor:g})")
    print("Сохранено:")
    for fig, name in figs:
        path = save(fig, out_dir, name, args.dpi)
        print(f"  {path}")

    if args.show:
        if matplotlib.get_backend().lower() == "agg":
            print("Интерактивный показ недоступен (backend Agg) — откройте PNG вручную.")
        else:
            plt.show()
    for fig, _ in figs:
        plt.close(fig)


if __name__ == "__main__":
    main()
