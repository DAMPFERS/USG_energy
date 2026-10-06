"""
Эмулятор терминала: служебные сообщения пишутся в лог-файл (по одному на строку),
веб-страница получает их через API, которое читает этот же файл.

Формат строки:  2026-10-07 09:15:00 | WARN | Текст сообщения (переводы строк как \\n)
Уровни: INFO, WARN, CRIT, OK.
"""
import os
import threading
from datetime import datetime

LEVELS = ("INFO", "WARN", "CRIT", "OK")


class Terminal:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._lock = threading.Lock()
        self._cache: list[dict] = []
        self._cache_size = -1

    def post(self, text: str, level: str = "INFO", when: datetime | None = None):
        level = level.upper() if level and level.upper() in LEVELS else "INFO"
        ts = (when or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
        body = str(text).strip().replace("\r", "").replace("\n", "\\n")
        with self._lock, open(self.path, "a", encoding="utf-8") as f:
            f.write(f"{ts} | {level} | {body}\n")

    def messages(self, after: int = 0) -> list[dict]:
        """Все сообщения с порядковым номером > after."""
        with self._lock:
            size = os.path.getsize(self.path) if os.path.exists(self.path) else 0
            if size != self._cache_size:
                self._cache = self._read()
                self._cache_size = size
            return [m for m in self._cache if m["n"] > after]

    def _read(self) -> list[dict]:
        out = []
        if not os.path.exists(self.path):
            return out
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                parts = line.rstrip("\n").split(" | ", 2)
                if len(parts) != 3:
                    continue
                out.append({"n": len(out) + 1, "ts": parts[0], "level": parts[1],
                            "text": parts[2].replace("\\n", "\n")})
        return out
