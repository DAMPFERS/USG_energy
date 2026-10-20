"""
Соответствие «номер линии (реле) → потребитель».

Список потребителей (ключ + имя) задаётся в server.yaml (consumers). Какое реле
питает какого потребителя, выясняется на площадке, поэтому привязка хранится
отдельно в config/line_map.yaml и меняется на лету из админки (команда name).
Привязка взаимно однозначная: если потребитель уже был на другой линии,
линии меняются потребителями.

Сценарий может ссылаться на линию номером (3) или ключом потребителя ("BIO-L").
"""
import logging
import os
import threading

import yaml

log = logging.getLogger("linemap")


class LineMap:
    def __init__(self, line_ids: list[int], consumers: list[dict] | None, path: str,
                 fallback_names: dict[int, str] | None = None):
        self.path = path
        self.line_ids = list(line_ids)
        self.consumers: dict[str, str] = {}          # ключ → имя (порядок из конфига)
        for c in consumers or []:
            self.consumers[str(c["key"]).upper()] = str(c.get("name", c["key"]))
        self.fallback = fallback_names or {}
        self._lock = threading.Lock()
        self._map: dict[int, str | None] = {}
        keys = list(self.consumers)
        for i, lid in enumerate(self.line_ids):     # по умолчанию — по порядку
            self._map[lid] = keys[i] if i < len(keys) else None
        self._load()

    def _load(self):
        if not os.path.exists(self.path):
            return
        with open(self.path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        loaded = {}
        for lid, key in raw.items():
            lid = int(lid)
            key = str(key).upper() if key else None
            if lid in self._map and (key is None or key in self.consumers):
                loaded[lid] = key
        if loaded:
            used = set(loaded.values())
            for lid in self._map:
                if lid in loaded:
                    self._map[lid] = loaded[lid]
                elif self._map[lid] in used:
                    self._map[lid] = None
            log.info("Привязка линий загружена из %s", self.path)

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("# line number -> consumer key (edited from the admin console: name <line> <key>)\n")
            yaml.safe_dump({lid: self._map[lid] for lid in self.line_ids}, f, allow_unicode=True, sort_keys=False)
        os.replace(tmp, self.path)

    # ------------------------------------------------------------------ чтение
    def key(self, lid: int) -> str | None:
        with self._lock:
            return self._map.get(lid)

    def name(self, lid: int) -> str:
        key = self.key(lid)
        if key:
            return self.consumers[key]
        return self.fallback.get(lid) or f"Line {lid}"

    def resolve(self, ref) -> int | None:
        """Линия по номеру или ключу потребителя."""
        if isinstance(ref, int) or (isinstance(ref, str) and ref.strip().isdigit()):
            lid = int(ref)
            return lid if lid in self._map else None
        key = str(ref).strip().upper()
        with self._lock:
            for lid, k in self._map.items():
                if k == key:
                    return lid
        return None

    def view(self) -> dict:
        with self._lock:
            return {"consumers": [{"key": k, "name": n,
                                   "line": next((l for l, kk in self._map.items() if kk == k), None)}
                                  for k, n in self.consumers.items()],
                    "map": {str(l): k for l, k in self._map.items()}}

    # ------------------------------------------------------------------ изменение
    def assign(self, lid: int, key: str | None) -> tuple[bool, str]:
        if lid not in self._map:
            return False, f"Нет линии {lid}"
        if key is not None:
            key = key.strip().upper()
            if key in ("-", "NONE", ""):
                key = None
            elif key not in self.consumers:
                return False, f"Неизвестный потребитель '{key}'. Есть: {', '.join(self.consumers)}"
        with self._lock:
            old = self._map[lid]
            if key is not None:
                for other, k in self._map.items():
                    if k == key and other != lid:
                        self._map[other] = old          # обмен потребителями
                        break
            self._map[lid] = key
            self._save()
        return True, ""
