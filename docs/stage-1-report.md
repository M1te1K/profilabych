# Отчёт: первый этап Профилабыча

Дата: 9 октября 2026 года. Рабочая директория — корень существующего
репозитория `Профилабыч/`.

## 1. Что реализовано

- Создан устанавливаемый пакет `profilabych` с `src` layout и двумя точками входа:
  `python -m profilabych` и команда `profilabych`.
- Выделены пакеты Telegram Interface, Agent Core, LLM Provider, Tool Registry
  и Storage. На первом этапе это границы для дальнейшей реализации.
- Реализованы Configuration и Logging: env/dotenv, валидация, неизменяемые
  настройки, SecretStr, JSON в stdout, UTC и маскирование секретов.
- Подготовлены `AGENTS.md`, README, описание архитектуры, `.env.example`,
  `.gitignore`, `.dockerignore`, Dockerfile и зависимости с хешами.
- Созданы `.venv` и локальный `.env` с пустыми ключами; права `.env` — 0600.
- Добавлены 47 проверок pytest для конфигурации, логов, импортов и offline CLI.

Локальный запуск проверяет конфигурацию, пишет `infrastructure_ready` и выходит
с кодом 0. Он не обращается к Telegram/OpenRouter и не создаёт БД.

## 2. Структура проекта

```text
Профилабыч/
├── AGENTS.md
├── README.md
├── pyproject.toml
├── requirements.txt
├── requirements-dev.txt
├── .env.example
├── .gitignore
├── .dockerignore
├── Dockerfile
├── docs/
│   ├── architecture.md
│   └── stage-1-report.md
├── src/profilabych/
│   ├── __init__.py
│   ├── __main__.py
│   ├── main.py
│   ├── config.py
│   ├── logging.py
│   ├── py.typed
│   ├── agent/
│   ├── interfaces/telegram/
│   ├── llm/
│   ├── tools/
│   └── storage/
└── tests/
    ├── conftest.py
    ├── test_config.py
    ├── test_logging.py
    └── test_structure.py
```

`.env`, `.venv`, кеши и будущие локальные данные исключены из Git.

## 3. Технологии

Фактически установлены и проверены на macOS arm64:

| Технология | Версия / состояние |
| --- | --- |
| Python | 3.13.13; диапазон проекта ≥3.12, <3.15 |
| aiogram | 3.31.0; импорт проверен, бот не подключён |
| httpx | 0.28.1; импорт проверен, запросы не выполнялись |
| aiosqlite | 0.22.1; импорт проверен, репозитории ещё не реализованы |
| Pydantic | 2.13.5 |
| pydantic-settings | 2.15.0 |
| pytest | 9.1.1 |
| pytest-asyncio | 1.4.0; подготовлен для следующего этапа |
| Ruff | 0.16.10 |
| mypy | 1.20.2 с `pydantic.mypy` |
| uv | 0.12.24; только генерация requirements |
| logging / JSON | Стандартная библиотека Python |
| Docker | CLI 29.4.0; движок недоступен |
| Git | Репозиторий уже существовал, ветка main, коммитов нет |

По умолчанию указан `qwen/qwen3-coder:free`: [каталог OpenRouter](https://openrouter.ai/qwen/qwen3-coder:free)
отмечает бесплатную стоимость и ориентацию на function calling. Каталог прочитан
9 октября 2026 года; ни inference API, ни другие API OpenRouter не вызывались.
Работа tools у конкретного активного endpoint и его доступность не проверялись.

## 4. Архитектурные решения и причины

- Компоненты разделены пакетами, чтобы транспорт, HTTP, инструменты и SQL
  не смешивались с управлением диалогом.
- `main.py` — единственная точка сборки. Сейчас она загружает Settings и настраивает
  logging. Клиенты и DI будут добавлены обычными аргументами без DI-контейнера.
- Формальные Protocol и domain DTO отложены до первых рабочих сценариев этапа 2;
  лишние абстракции и фиктивные инструменты не созданы.
- Модель задаётся через `LLM_MODEL`; проверка не привязана к Qwen. Бесплатный
  DeepSeek с поддержкой tools можно будет выбрать конфигурацией при его наличии.
- Платные ID и произвольные API endpoints отвергаются. При интеграции потребуется
  дополнительная проверка тарифа/tools и нулевая `provider.max_price`.
- SQLite и async HTTP соответствуют бюджету и не требуют тяжёлых локальных сервисов.
- Структурированные логи реализованы стандартным logging без внешнего сервиса.
  Маскируются известные секреты, токены в URL и чувствительные ключи контекста.
- Unit-тесты работают с временным окружением и без внешних ключей. Audit hook
  запрещает сетевые socket-события при проверке импорта/локального запуска.
- Docker использует Python 3.12 slim, wheel, только runtime-зависимости и пользователя
  UID/GID 10001. В образ не копируются секреты и локальные данные.

## 5. Выполненные проверки

| Проверка | Фактический результат |
| --- | --- |
| Установка requirements-dev с `--require-hashes` | Успешно |
| Editable-установка проекта с `--no-deps -e .` | Успешно |
| `pytest` после исправлений | **47 passed**, 1.84 с |
| `test_config.py` | 32 passed |
| `test_logging.py` | 4 passed |
| `test_structure.py` | 11 passed |
| `ruff check .` | All checks passed |
| `ruff format --check .` | 18 files already formatted |
| `mypy src` | No issues found in 11 source files |
| `pip check` | No broken requirements found |
| `compileall -q src` | Код завершения 0 |
| `python -m profilabych` | JSON `infrastructure_ready`, код 0 |
| `.venv/bin/profilabych` | JSON `infrastructure_ready`, код 0 |
| Проверка неверной конфигурации в subprocess | Код 1, исходное значение не попало в лог |
| Запуск с запретом сетевых socket-событий | Успешно, файлов состояния нет |
| Импорт aiogram, httpx, aiosqlite и пакетов проекта | Успешно |
| Парсинг исходников с грамматикой Python 3.12 | Успешно; это не запуск под Python 3.12 |
| Сборка обычного wheel | `profilabych-0.1.0-py3-none-any.whl` успешно собран |
| Проверка содержимого wheel | Компоненты и `py.typed` включены, локальных данных нет |
| `git check-ignore` | `.env`, окружение, SQLite/WAL, логи, кеши исключены |
| `git diff --check` | Код 0; файлы пока untracked, поэтому это не полная проверка нового содержимого |
| `git diff --no-index --check` для каждого нового файла | Все 27 файлов прошли проверку пробелов |
| Docker engine | Недоступен; сборка и запуск образа не выполнялись |

**Не прошедшие тесты в финальном прогоне: отсутствуют.** Полный pytest выполнялся
на Python 3.13.13. Запуск на Python 3.12/3.14 и в Linux-контейнере не выполнялся.
Новые файлы не добавлялись в индекс, коммиты и push не выполнялись.

## 6. Обнаруженные проблемы и решения

1. Первая попытка чтения PyPI из ограниченной среды не смогла разрешить DNS.
   Установка повторена с разрешённым сетевым доступом и завершилась успешно.
   Кеши перенесены в `/private/tmp`, чтобы не писать в системный кеш пользователя.
2. Первый pytest: 45 passed, 2 failed. Тест
   `test_common_token_patterns_are_redacted_without_registration` обнаружил
   пропуск Telegram-токена после префикса `bot` в URL. Исправлена граница regex;
   повторный тест прошёл.
3. Тест `test_imports_do_not_access_network_or_create_files` заменял `socket.socket`
   функцией и ломал наследование `ssl.SSLSocket`. Вместо замены класса установлен
   Python audit hook, запрещающий реальные socket-события; повторный тест прошёл.
4. Ruff обнаружил длинную строку и несогласованное форматирование. Код отформатирован,
   обе проверки прошли.
5. mypy первоначально не распознавал служебный аргумент Pydantic Settings `_env_file`.
   Подключён штатный `pydantic.mypy`, проверка прошла без `type: ignore`.
6. Docker CLI указывает на незапущенный OrbStack engine: сокет отсутствует.
   Dockerfile подготовлен, wheel собран; Docker build/run остаются непроверенными.
   Платные сервисы и деплой не использовались.

## 7. Что осталось сделать

В объёме первого этапа разработка завершена. Для полной проверки контейнера
потребуется доступный Docker engine; для проверки всех поддерживаемых версий —
отдельная матрица Python 3.12/3.13/3.14.

Следующий продуктовый этап: OpenRouter provider, SQLite-история и заметки,
Tool Registry, согласованные инструменты, последовательный цикл агента и aiogram.
Изоляция пользовательских данных пока определена архитектурой и AGENTS.md;
она ещё не реализована и будет проверяться вместе с репозиториями/инструментами.
Ключи понадобятся только перед живой интеграцией и будут предоставлены Product Owner.

## 8. Рекомендации для следующего этапа

Сначала реализовать и проверить OpenRouter adapter через mock HTTP, затем SQLite
с тестами разделения пользователей, registry и агентный цикл с fake LLM.
После этого подключить Telegram, обработку длинных ответов и ошибок, закрытие
клиентов при остановке и ограничение параллельной обработки одного диалога.
Перед реальными запросами проверить бесплатную стоимость и поддержку tools;
не добавлять платные fallback. Дополнительные AI-возможности отложить.

**Готовность к интеграции Telegram и OpenRouter:** да, инфраструктура и окружение
подготовлены. Рабочая интеграция и полноценный Telegram-агент ещё не реализованы.

## Выполненные команды

Диагностика: `pwd`, `rg --files`, `ls -la`, `git status --short --branch`,
`command -v python3/uv/docker/git`, `python3 --version`, `python3 -m pip --version`,
`uname -m`, проверка родительских AGENTS.md, `git log -1 --oneline`
(коммитов ещё нет), `docker version --format …` (движок недоступен).
Также выполнялись попытки `pip index versions` для aiogram, pydantic-settings,
aiosqlite и httpx в ограниченной сети; они не подтвердили версии из-за DNS.

Основные команды установки и фиксации:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --cache-dir /private/tmp/profilabych-pip-cache --retries 1 uv
.venv/bin/uv --cache-dir /private/tmp/profilabych-uv-cache pip compile pyproject.toml --python-version 3.12 --universal --generate-hashes --quiet -o requirements.txt
.venv/bin/uv --cache-dir /private/tmp/profilabych-uv-cache pip compile pyproject.toml --extra dev --python-version 3.12 --universal --generate-hashes --quiet -o requirements-dev.txt
.venv/bin/python -m pip install --cache-dir /private/tmp/profilabych-pip-cache --require-hashes -r requirements-dev.txt
.venv/bin/python -m pip install --cache-dir /private/tmp/profilabych-pip-cache --no-deps -e .
chmod 600 .env
```

Проверки и исправление форматирования:

```bash
.venv/bin/python -m pytest
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
.venv/bin/python -m ruff format .
.venv/bin/python -m mypy src
.venv/bin/python -m pip check
.venv/bin/python -m pip --cache-dir /private/tmp/profilabych-pip-cache check
.venv/bin/python -m compileall -q src
.venv/bin/python -m profilabych
.venv/bin/profilabych
.venv/bin/python -m pip wheel --cache-dir /private/tmp/profilabych-pip-cache --no-deps --wheel-dir /private/tmp/profilabych-wheels .
git check-ignore .env .venv/bin/python data/test.sqlite3 data/test.sqlite3-wal logs/test.log .pytest_cache/a
git diff --check
git status --short --branch
```

Дополнительно выполнялись локальные Python-скрипты: безопасное создание `.env`
из примера без перезаписи, `ast.parse(..., feature_version=(3, 12))`, проверка
содержимого wheel через `zipfile`, проверка всех новых файлов через
`git diff --no-index --check /dev/null <файл>` и проверка исключения `.env`
при сохранении `.env.example` в доступном для Git наборе. Файлы создавались и редактировались инструментом
патчей; секреты не выводились. Читались официальные документы aiogram,
Pydantic Settings и публичный каталог/документация OpenRouter.
