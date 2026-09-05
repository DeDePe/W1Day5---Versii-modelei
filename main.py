"""
GigaChat Model Benchmark — локальное веб-приложение.

Backend: FastAPI + httpx (async). Читает настройки из `GigaChat API settings.txt`,
список моделей из `GigaChat API models.txt`, тарифы из `pricing.json`.

Запуск:
    .venv\\Scripts\\activate         (Windows)
    pip install -r requirements.txt
    uvicorn main:app --reload
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import ssl
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

BASE_DIR = Path(__file__).resolve().parent
SETTINGS_FILE = BASE_DIR / "GigaChat API settings.txt"
MODELS_FILE = BASE_DIR / "GigaChat API models.txt"
PRICING_FILE = BASE_DIR / "pricing.json"
TEMPLATE_FILE = BASE_DIR / "templates" / "index.html"

OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
API_BASE = "https://gigachat.devices.sberbank.ru/api/v1"
API_BASE_ULTRA = "https://api.giga.chat/v1"  # GigaChat-3-Ultra живёт на отдельном адресе
REQUEST_TIMEOUT = 120.0  # сек, на одну модель


def api_base_for(model: str) -> str:
    return API_BASE_ULTRA if "ultra" in model.lower() else API_BASE

log = logging.getLogger("benchmark")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


# --------------------------------------------------------------------------- #
# Конфигурация
# --------------------------------------------------------------------------- #

@dataclass
class GigaConfig:
    auth_key: str = ""
    scope: str = "GIGACHAT_API_PERS"

    @property
    def ok(self) -> bool:
        return bool(self.auth_key)


def parse_settings(path: Path) -> GigaConfig:
    """Парсит 'GigaChat API settings.txt'.

    Поддерживает:
      - Authorization Key (base64 'client_id:client_secret') — родной формат файла
      - отдельные Client ID + Client Secret -> собирает base64 сам
      - готовый access-токен (строка вида 'ey...')
    """
    cfg = GigaConfig()
    if not path.exists():
        return cfg

    text = path.read_text(encoding="utf-8-sig", errors="replace")
    # Поля вида "Название\nзначение" (родной формат) или "Название: значение".
    values = re.findall(r"^\s*([A-Za-z ]+?)\s*:?\s*\n\s*(.+?)\s*$|^\s*([A-Za-z ]+?)\s*:\s*(.+?)\s*$", text, re.MULTILINE)
    fields: dict[str, str] = {}
    for k1, v1, k2, v2 in values:
        key = (k1 or k2).strip().lower()
        val = (v1 or v2).strip()
        if key and val and key not in fields:
            fields[key] = val

    auth = fields.get("authorization key") or fields.get("authorization")
    if auth:
        cfg.auth_key = auth
    else:
        client_id = fields.get("client id", "")
        client_secret = fields.get("client secret", "")
        if client_id and client_secret:
            cfg.auth_key = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()

    if fields.get("scope"):
        cfg.scope = fields["scope"]
    return cfg


def parse_models_file(path: Path) -> list[dict[str, str]]:
    """Читает 'GigaChat API models.txt'.

    Поддерживаемые форматы строки:
      - 'ModelName'
      - '- ModelName — описание' / 'ModelName = описание' / 'ModelName: описание'
      - 'ModelName,input_price,output_price' (переопределение тарифа)
    Заголовки-фразы без валидного id моделей игнорируются эвристикой:
    id модели = латиница/цифры/дефисы.
    """
    if not path.exists():
        return []
    models: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "//")):
            continue
        # срезаем маркеры списка
        line = re.sub(r"^[-*\d.)\s]+", "", line).strip()
        if not line:
            continue

        desc = ""
        prices: Optional[tuple[str, ...]] = None
        # 'id = desc' / 'id — desc' / 'id – desc'
        m = re.match(r"^([A-Za-z][A-Za-z0-9._-]*)\s*(?:[=—–]| - )\s*(.*)$", line)
        if m:
            mid, desc = m.group(1), m.group(2).strip()
        else:
            m = re.match(r"^([A-Za-z][A-Za-z0-9._-]*)\s*(?::\s*(.*))?$", line)
            if not m:
                continue
            mid, desc = m.group(1), (m.group(2) or "").strip()

        # опциональные цены: 'id,in,out' в описании после '|'
        if "|" in desc:
            desc, tail = desc.split("|", 1)
            parts = [p.strip() for p in tail.split(",") if p.strip()]
            if len(parts) == 2 and all(re.fullmatch(r"[\d.]+", p) for p in parts):
                prices = tuple(parts)
            desc = desc.strip()

        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9._-]*", mid) or mid.lower() in {"http", "https"}:
            continue
        if mid in seen:
            continue
        seen.add(mid)
        entry: dict[str, str] = {"id": mid, "description": desc}
        if prices:
            entry["price_input"], entry["price_output"] = prices
        models.append(entry)
    return models


def load_pricing(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"models": {"default": {"input": 0.0, "output": 0.0}}}
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("pricing.json повреждён (%s); используем нулевые тарифы", exc)
        return {"models": {"default": {"input": 0.0, "output": 0.0}}}


CONFIG = parse_settings(SETTINGS_FILE)
MODELS = parse_models_file(MODELS_FILE)
PRICING = load_pricing(PRICING_FILE)


def token_price(model_id: str) -> tuple[float, float]:
    """(input, output) ₽ за 1М токенов."""
    table = PRICING.get("models", {})
    row = table.get(model_id) or table.get("default") or {"input": 0.0, "output": 0.0}
    return float(row.get("input", 0.0)), float(row.get("output", 0.0))


def estimate_cost(model_id: str, prompt_tokens: int, completion_tokens: int) -> float:
    pin, pout = token_price(model_id)
    return prompt_tokens / 1_000_000 * pin + completion_tokens / 1_000_000 * pout


# --------------------------------------------------------------------------- #
# GigaChat API клиент
# --------------------------------------------------------------------------- #

# Сбер отдаёт сертификаты с частной цепочкой — проверку SSL отключаем,
# как рекомендует официальная документация (verify_ssl_certs=False).
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


class TokenManager:
    """Получает и кэширует Bearer-токен (TTL ~30 мин)."""

    def __init__(self, cfg: GigaConfig) -> None:
        self._cfg = cfg
        self._token: Optional[str] = None
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()

    async def get(self) -> str:
        async with self._lock:
            if self._token and time.monotonic() < self._expires_at - 30:
                return self._token
            await self._refresh()
            return self._token  # type: ignore[return-value]

    async def _refresh(self) -> None:
        if not self._cfg.ok:
            raise RuntimeError(
                "Не найден Authorization Key. Создайте файл 'GigaChat API settings.txt' "
                "с полями Client ID / Authorization Key (см. пример в README)."
            )
        rq_uid = str(uuid.uuid4())
        async with httpx.AsyncClient(verify=_SSL_CTX, timeout=30) as client:
            resp = await client.post(
                OAUTH_URL,
                headers={
                    "Authorization": f"Basic {self._cfg.auth_key}",
                    "RqUID": rq_uid,
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
                data={"scope": self._cfg.scope},
            )
        if resp.status_code != 200:
            raise RuntimeError(f"OAuth {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        self._token = data["access_token"]
        # GigaChat возвращает access_token.expires_at (мс, unix). Если нет — 30 мин.
        expires_ms = data.get("expires_at") or (
            (data.get("access_token") or {}).get("expires_at") if isinstance(data.get("access_token"), dict) else None
        )
        ttl = 1800.0
        if isinstance(expires_ms, (int, float)) and expires_ms > 1e12:
            ttl = max(60.0, expires_ms / 1000.0 - time.time())
        self._expires_at = time.monotonic() + ttl
        log.info("Получен новый GigaChat токен (ttl %.0f с)", ttl)


TOKENS = TokenManager(CONFIG)


async def chat_completion(model: str, prompt: str) -> dict[str, Any]:
    """Один запрос к модели. Возвращает ответ + usage + latency.

    Персональный тариф генерирует в один поток: параллельные запросы получают
    HTTP 429. Ретраим с нарастающим бэкоффом; 401 — обновляем токен.
    """
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7,
        "max_tokens": 1024,
    }
    backoffs = [0.0, 1.5, 3.0, 5.0]
    last_err = ""
    for attempt, delay in enumerate(backoffs):
        if delay:
            await asyncio.sleep(delay)
        token = await TOKENS.get()
        t0 = time.perf_counter()
        async with httpx.AsyncClient(verify=_SSL_CTX, timeout=REQUEST_TIMEOUT) as client:
            resp = await client.post(
                f"{api_base_for(model)}/chat/completions",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json=payload,
            )
        latency = time.perf_counter() - t0
        if resp.status_code == 200:
            data = resp.json()
            usage = data.get("usage") or {}
            choices = data.get("choices") or []
            content = (choices[0].get("message") or {}).get("content", "") if choices else ""
            return {
                "model": model,
                "content": content,
                "finish_reason": (choices[0].get("finish_reason") if choices else None),
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
                "latency_s": round(latency, 2),
                "cost_rub": round(estimate_cost(model, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)), 4),
            }
        if resp.status_code == 401:
            await TOKENS._refresh()
            last_err = "HTTP 401: токен отклонён"
            continue
        if resp.status_code == 429:
            last_err = f"HTTP 429: rate limit (попытка {attempt + 1}/{len(backoffs)})"
            continue
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
    raise RuntimeError(f"{last_err} — превышено число попыток")


# --------------------------------------------------------------------------- #
# FastAPI
# --------------------------------------------------------------------------- #

app = FastAPI(title="GigaChat Model Benchmark")


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    if not TEMPLATE_FILE.exists():
        return HTMLResponse("<h1>templates/index.html не найден</h1>", status_code=500)
    return HTMLResponse(TEMPLATE_FILE.read_text(encoding="utf-8"))


@app.get("/api/config")
async def api_config() -> JSONResponse:
    """Отдаёт фронту: список моделей, тарифы, статус конфигурации."""
    models_list = [
        {
            "id": m["id"],
            "description": m.get("description", ""),
            "price_input": float(m.get("price_input") or token_price(m["id"])[0]),
            "price_output": float(m.get("price_output") or token_price(m["id"])[1]),
        }
        for m in MODELS
    ]
    warnings: list[str] = []
    if not SETTINGS_FILE.exists():
        warnings.append(f"Файл не найден: {SETTINGS_FILE.name}")
    elif not CONFIG.ok:
        warnings.append("В settings.txt нет Authorization Key / Client ID+Secret")
    if not MODELS_FILE.exists():
        warnings.append(f"Файл не найден: {MODELS_FILE.name}")
    elif not MODELS:
        warnings.append("В models.txt не распознано ни одной модели")
    return JSONResponse({"models": models_list, "warnings": warnings, "pricing_meta": PRICING.get("_meta", {})})


@app.post("/api/benchmark")
async def api_benchmark(request: Request) -> JSONResponse:
    body = await request.json()
    prompt = (body.get("prompt") or "").strip()
    models: list[str] = [m for m in (body.get("models") or []) if m][:3]
    if not prompt:
        return JSONResponse({"error": "Пустой промпт"}, status_code=400)
    if len(models) < 1:
        return JSONResponse({"error": "Не выбрана ни одна модель"}, status_code=400)

    async def run_one(model: str) -> dict[str, Any]:
        try:
            return await chat_completion(model, prompt)
        except Exception as exc:  # ошибка одной модели не валит остальные
            log.warning("Модель %s: %s", model, exc)
            return {"model": model, "error": str(exc)[:500]}

    results = await asyncio.gather(*(run_one(m) for m in models))
    return JSONResponse({"results": list(results), "elapsed_s": round(time.perf_counter() - _t0_bench, 2)})


_t0_bench = 0.0


@app.middleware("http")
async def bench_timer(request: Request, call_next):
    global _t0_bench
    if request.url.path == "/api/benchmark":
        _t0_bench = time.perf_counter()
    return await call_next(request)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
