"""
Консоль организатора:

    python -m admin                                   # сервер на этом же компьютере, токен из config/server.yaml
    python -m admin --url http://192.168.8.10:8000 --token <admin_token>
"""
import argparse
import os

import yaml

from .client import AdminClient
from .console import AdminConsole

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    ap = argparse.ArgumentParser(description="Энергосистема купола — консоль организатора")
    ap.add_argument("--config", default=os.path.join(ROOT, "config", "server.yaml"),
                    help="server.yaml, из которого брать port и admin_token")
    ap.add_argument("--url", help="адрес сервера, например http://192.168.8.10:8000")
    ap.add_argument("--token", help="токен организатора (admin_token в server.yaml)")
    ap.add_argument("--interval", type=float, default=1.0, help="период обновления, с")
    args = ap.parse_args()

    cfg = {}
    if os.path.exists(args.config):
        with open(args.config, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    url = args.url or f"http://127.0.0.1:{cfg.get('port', 8000)}"
    token = args.token or cfg.get("admin_token") or ""
    AdminConsole(AdminClient(url, token), args.interval).run()


if __name__ == "__main__":
    main()
