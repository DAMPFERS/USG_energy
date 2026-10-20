"""HTTP-клиент админского API сервера (только стандартная библиотека)."""
import json
import urllib.error
import urllib.request


class ApiError(Exception):
    pass


class AdminClient:
    def __init__(self, url: str, token: str, timeout: float = 4.0):
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _req(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers={"X-Admin-Token": self.token, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read().decode("utf-8")).get("detail", "")
            except ValueError:
                detail = ""
            if isinstance(detail, list):    # ошибка валидации FastAPI
                detail = "; ".join(str(d.get("msg", d)) for d in detail)
            raise ApiError(detail or f"HTTP {e.code}") from None
        except (urllib.error.URLError, OSError) as e:
            raise ApiError(f"Нет связи с сервером: {getattr(e, 'reason', e)}") from None

    def state(self, after: int = 0) -> dict:
        return self._req("GET", f"/api/admin/state?after={after}")

    def post(self, path: str, body: dict) -> dict:
        return self._req("POST", "/api/admin/" + path, body)
