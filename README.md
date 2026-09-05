# GigaChat Benchmark

Локальное веб-приложение для сравнительного тестирования (бенчмаркинга) моделей GigaChat: один промпт — три модели — параллельные запросы — метрики и стоимость в удобном 3-колоночном интерфейсе.

![stack](https://img.shields.io/badge/Python-3.10+-blue) ![fastapi](https://img.shields.io/badge/FastAPI-0.115+-green) ![license](https://img.shields.io/badge/license-MIT-green)

## Возможности

- **Параллельный бенчмарк** трёх выбранных моделей (`asyncio.gather`), время ответа каждой замеряется независимо.
- **Метрики**: latency (сотые доли секунды), prompt/completion/total токены (из `usage` ответа API), расчётная стоимость в ₽.
- **Тарифы** вынесены в [`pricing.json`](pricing.json) — правьте под актуальные цены; freemium-модели помечаются `0`.
- **Markdown + LaTeX**: ответы рендерятся через marked.js + KaTeX (формулы `$...$`, `$$...$$`, `\(...\)`, `\[...\]`).
- **Тёмная/светлая тема**, копирование ответа, skeleton-загрузка, Ctrl+Enter для запуска.
- **GigaChat-3-Ultra поддержана**: модель живёт на отдельном базовом URL (`api.giga.chat`) — маршрутизация выполняется автоматически по имени модели.
- **Устойчивость**: ретраи 401 (обновление токена) и 429 (rate limit персонального тарифа) с нарастающим бэкоффом; SSL-проверка сертификатов Сбера отключена (требование их инфраструктуры).

## Требования

- Python 3.10+
- Ключ авторизации GigaChat (см. [быстрый старт Сбера](https://developers.sber.ru/docs/ru/gigachat/quickstart/ind-start))

## Установка и запуск

```bash
# 1. Виртуальное окружение
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # Linux/macOS

# 2. Зависимости
pip install -r requirements.txt

# 3. Конфигурация
copy "GigaChat API settings.template.txt" "GigaChat API settings.txt"   # Windows
# cp "GigaChat API settings.template.txt" "GigaChat API settings.txt"   # Linux/macOS
copy "GigaChat API models.template.txt" "GigaChat API models.txt"
# → впишите свои Client ID / Authorization Key в settings.txt
# → оставьте нужные модели в models.txt

# 4. Запуск
uvicorn main:app --reload
```

Откройте http://127.0.0.1:8000

## Конфигурационные файлы

| Файл | Назначение |
|---|---|
| `GigaChat API settings.txt` | Client ID, Client Secret или готовый Authorization Key (base64), Scope. **Секрет — не коммитится.** |
| `GigaChat API models.txt` | Список моделей для сравнения, по одной на строку. Формат: `- ID — описание`. |
| `pricing.json` | Цены за 1М токенов (input/output) для расчёта стоимости. |

> ⚠️ `settings.txt` содержит секрет и исключён из git через `.gitignore`. В репозитории лежат шаблоны `*.template.txt`.

## API эндпоинты

| Метод | Путь | Описание |
|---|---|---|
| GET | `/` | Веб-интерфейс |
| GET | `/api/config` | Список моделей, тарифы, статус конфигурации |
| POST | `/api/benchmark` | `{ "prompt": "...", "models": ["A", "B", "C"] }` → метрики и ответы |

## Структура проекта

```
├── main.py                            # FastAPI: OAuth, параллельные запросы, ретраи
├── templates/
│   └── index.html                     # Единый фронт (Tailwind CDN + marked.js + KaTeX)
├── pricing.json                       # Тарифы за 1М токенов
├── requirements.txt
├── GigaChat API settings.template.txt # Шаблон настроек (секреты — в вашей копии)
└── GigaChat API models.template.txt   # Шаблон списка моделей
```

## Примечания

- Токен доступа кэшируется (TTL ~30 мин) и обновляется автоматически.
- Персональный тариф генерирует в один поток: параллельные запросы могут получать 429 — приложение автоматически повторяет с паузой.
- Стоимость рассчитывается по `pricing.json` и является оценочной.

## Результаты сравнения

- Для сравнения брал три модели: GIGAChat, GIGAChat-Plus и GIGAChat-3-Ultra. 
- Всем моделям в качестве промпта давалась логическая задача про переправу фермером через реку козы, волка и капусты, но с измененными условиями. Волк у меня был вегетарианец и он не ел козу, зато с удовольствием съел бы капусту. 
- Первая модель - галюцинации и неверный подсчет количества рейсов.
- Вторая модель - запуталась в логике и ошиблась при подсчете количества рейсов.
- Третья модель - тоже запуталась в логике и неправильно подсчитала количество рейсов.
- От модели к модели растет время ответа, количество затраченных токенов и стоимость. Ответы становятся более витиеватыми, но результат пока все равно отрицательный - задача не решена. 