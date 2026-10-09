# Отчёт об этапе 2 — 9 октября 2026 года

## 1. Результат и готовность

В существующем репозитории реализовано рабочее ядро Agent Core, независимое от
Telegram. OpenRouter адаптер, Tool Registry, все четыре инструмента, SQLite,
история, reset, CLI и офлайн-демонстрация готовы. Итоговые автоматические проверки
прошли. Реальные API-запросы, paid API, деплой, коммит и push не выполнялись.
Существующие файлы и границы пакетов сохранены; новые зависимости не добавлены.

## 2. Структура и технологии

| Путь | Реализация |
| --- | --- |
| `agent/core.py` | Agent/AgentError, цикл, история, лимиты и блокировки пользователей |
| `llm/models.py` | Message, FunctionCall, ToolCall, ToolChoice, LLMProvider Protocol, LLMError |
| `llm/openrouter.py` | Асинхронный OpenRouter Chat Completions через httpx |
| `llm/fake.py` | FakeLLM/FakeRequest для скриптовых сценариев |
| `tools/registry.py` | Generic Tool, Registry, ToolContext, ToolResult/ToolError |
| `tools/calculator.py` | AST whitelist + Decimal |
| `tools/current_time.py` | datetime/zoneinfo |
| `tools/notes.py` | save_note/list_notes через NotesRepository |
| `storage/contracts.py` | Protocol истории и заметок, Note/StorageError, доверенные идентификаторы |
| `storage/history.py` | Целостность хода и удаление старых ходов целиком |
| `storage/sqlite.py` | aiosqlite, WAL, schema/indexes, транзакции и закрытие |
| `interfaces/cli.py` | Ввод, ответы, /reset, /exit через то же ядро |
| `main.py` | Composition root, обычный CLI, --check, --demo |
| `demo.py` | Fake LLM -> настоящие инструменты -> временная SQLite |
| `config.py`, `logging.py` | Новые runtime-лимиты, ограничение логов, request ContextVar |

Проверенное окружение: Python 3.13.13 на macOS, pytest 9.1.1,
pytest-asyncio 1.4.0. Стек проекта Python 3.12–3.14 сохранён; отдельные прогоны
на Python 3.12/3.14 и Linux не выполнялись. aiogram установлен, но не участвует
в ядре. Pydantic 2 обеспечивает типы, схемы и строгую валидацию, стандартная
библиотека — AST, Decimal, время и JSON-логи. Новых runtime/dev зависимостей нет,
requirements с хешами не изменялись.

## 3. Работа Agent Core и Tool Calling

`Agent.handle(user_id, text, chat_id=0)` сериализует запросы пользователя,
загружает его историю, добавляет user message и передаёт LLM контекст плюс schemas.
Assistant с tool_calls помещается в буфер целиком; каждый вызов исполняется по
порядку с доверенным ToolContext. Результаты помещаются в role=tool сообщения с
исходным tool_call_id. Затем LLM получает весь текущий ход для следующего шага.
Итоговый текст завершает ход; только после атомарного append он возвращается.

Поддерживаются несколько calls одного ответа и несколько последовательных раундов.
Ограничение — до 5 раундов и отдельно до 5 фактических calls по умолчанию
(можно настроить до 20). После лимита модель получает один completion с none для
финала. Повторённые IDs и новые calls после лимита отклоняются без выполнения.

Схемы и валидация используют одну модель параметров. Встроенные параметры strict,
extra=forbid. Registry умеет register/find/schemas/execute, контролирует timeout
и результат, возвращает `{ok,data,error}` и пишет ограниченные JSON-события.
Новому инструменту нужны параметры, async-handler и регистрация в main;
изменение Agent Core не требуется.

## 4. OpenRouter и бюджет

Адаптер использует `/models` для проверки нулевого тарифа и tools/tool_choice,
затем `/chat/completions` с messages/tools/tool_choice. В каждом completion есть
нулевые prompt/completion/request max_price, require_parameters=true и
allow_fallbacks=false. Модель берётся из LLM_MODEL, без условий по Qwen в ядре.
Отсутствующая/платная модель или неподтверждённые возможности означают отказ.

Контракт проверен по [официальному API](https://openrouter.ai/docs/api/reference/overview),
[Tool Calling](https://openrouter.ai/docs/guides/features/tool-calling) и
[provider routing/max_price](https://openrouter.ai/docs/guides/routing/provider-selection#max-price).
Работа HTTP-кода подтверждена MockTransport; реальная доступность модели,
интерпретация tools живой LLM и успешный обмен с API не подтверждены.

Общий timeout одной попытки, ограниченный retry и capped Retry-After защищают
от зависания. 429, 500/502/503/504 и transport errors повторяются; другие HTTP-ошибки,
redirect, неверный JSON/schema и незавершённый ответ контролируемо отвергаются.
Сырые сообщения ошибок и ключ не входят в публичную ошибку или лог.
Клиент закрывается через context manager, включая ошибку preflight и отмену.

## 5. Инструменты и данные

Calculator поддерживает +, -, *, /, скобки, десятичные и унарные знаки.
Не использует eval/exec. Пределы: 256 символов, 64 узла, глубина 16, модуль числа
до 10^15; результат Decimal с точностью 28 цифр возвращается строкой.
Синтаксис, запрещённые операции и деление на ноль дают ToolError.
current_time возвращает ISO 8601, по умолчанию UTC, принимает IANA timezone;
неизвестный timezone контролируемо отклоняется.

save_note принимает текст до 2000 символов и возвращает ID. list_notes выбирает
только текущего пользователя, сортирует по created_at и ID, поддерживает limit/offset.
Пользовательская идентичность не входит в аргументы инструмента; подмена user_id
отклоняется. Текст с SQL-командами сохраняется как обычное значение.

SQLiteStorage создаёт схему и каталоги, использует параметризованный SQL,
BEGIN IMMEDIATE/commit/rollback, WAL и busy timeout. Одна connection сериализуется
lock. История разделена по `(user_id, chat_id)`, заметки — по user_id.
Ход содержит user/assistant/tool сообщения; перед записью проверяются порядок,
IDs и завершённость. Запись и retention атомарны; старые ходы удаляются целиком.

reset_history(user_id) удаляет историю всех чатов пользователя, optional chat_id
ограничивает удаление одним чатом. Заметки не меняются. CLI /reset очищает текущий
чат. Максимальный размер истории действует на число сообщений и сериализованные
символы; system prompt добавляется отдельно. Текущий ход не обрезается.

## 6. Ошибки, решения и безопасность

- Ошибка инструмента становится role=tool результатом и позволяет LLM исправить
  аргументы или объяснить проблему.
- Ошибка LLM/лимит завершает ход локальным ответом, сохраняя предыдущие результаты.
  Отклонённые необслуженные calls не записываются.
- Ошибка SQLite не выдаёт успешный ответ. Текст ошибки предупреждает, что предыдущие
  операции с заметками могли успеть сохраниться.
- Отмена посреди группы закрывает оставшиеся ID результатом interrupted и
  распространяет CancelledError. Отмена финального commit не вызывает повторную
  запись: commit мог уже завершиться. SQLite гарантирует целый ход или его отсутствие.
- Lock одного пользователя охватывает handle и reset; idle lock удаляется.
  Для разных пользователей нет общей блокировки ожидания LLM.
- Request ContextVar передаёт метаданные во вложенные логи без смешения asyncio tasks.
  Маскирование секретов выполняется до усечения строк/коллекций/вложенности;
  HTTP/aiosqlite debug подавлен. Конфигурация и сырые HTTP-тела не логируются.
- По явному требованию этапа 2 логи включают ограниченные тексты пользователя,
  аргументы и результаты. Полное распознавание произвольных пользовательских секретов
  не гарантируется; доступ к логам должен оставаться локальным.

## 7. Проверки и сценарии

Финальный полный прогон: **191 passed**, ошибок и предупреждений pytest нет.

| Проверка | Результат |
| --- | --- |
| `.venv/bin/python -m pytest` | 191 passed, 3.07 с |
| `.venv/bin/python -m ruff check .` | All checks passed |
| `.venv/bin/python -m ruff format --check .` | 37 files already formatted |
| `.venv/bin/python -m mypy src` | Success, 24 source files |
| `.venv/bin/python -m pip check` | No broken requirements found |
| `.venv/bin/python -m compileall -q src` | Код 0 |
| `.venv/bin/python -m profilabych --demo` | Код 0, calculator/save_note/reset подтверждены |
| `--check`, обычный CLI без ключа, --demo | Проверены subprocess-тестами; без постоянных файлов |
| `git diff --check` | Код 0; tracked diff отсутствует, так как файлы untracked |
| `git diff --no-index --check /dev/null <file>` для source/docs | 40 файлов, whitespace errors отсутствуют |

Обязательные сценарии покрыты:

| Сценарии | Тесты |
| --- | --- |
| Обычный ответ, контекст | test_plain_answer_and_preserved_context |
| Калькулятор, время, запись, чтение | test_each_builtin_through_core, test_current_time |
| calculate -> save_note | test_calculate_save_note_demo_and_reset |
| Несколько calls и порядок | test_multiple_calls_execute_in_order |
| Неизвестный инструмент, аргументы, деление на ноль | test_controlled_errors, test_tool_failure_can_be_repaired |
| SQLite-ошибка, rollback | test_sqlite_failure_rolls_back, test_sqlite_tool_error_is_returned_to_llm |
| OpenRouter, 429, 500 | test_non_retryable_http_errors, test_retry_exhaustion, test_transport_unavailability |
| Неверный JSON/ответ/ID | test_invalid_completion, test_malformed_or_oversized_json, test_rejects_repeated_call_ids_and_wrong_role |
| Лимит раундов и количества calls | test_limits_rounds_total_calls_and_final_response |
| Изоляция, параллельные пользователи | test_persistence_ownership_order_and_reset, test_different_users_can_wait_for_llm_concurrently |
| reset, сохранность заметок | test_calculate_save_note_demo_and_reset, test_cli_commands_use_core_and_keep_notes |
| Секреты, размер и контекст логов | test_log_secrets_size_and_user_isolation, test_redaction_precedes_truncation_and_nested_values_are_bounded |
| Ошибка посреди выполнения | test_llm_failure_mid_turn_preserves_completed_action |
| Ограничение истории | test_history_limits_preserve_current_turn, test_whole_turn_retention |
| Закрытие ресурсов | test_composition_closes_clients_and_database_on_cancel, test_missing_key_and_cancelled_request |
| Конкурентные запросы/reset/отмена очереди | test_isolation_and_concurrent_same_user_requests, test_reset_waits_for_active_request_and_clears_its_history, test_cancelled_queued_request_does_not_leak_lock |
| Отмена посреди tools и после commit | test_cancel_mid_tool_group_persists_valid_history, test_cancel_after_commit_does_not_duplicate_turn |
| Запрет реальной сети | autouse forbid_network, test_demo_runs_with_network_connections_forbidden |

Не выполнялись: реальный OpenRouter, Telegram polling, Docker build/run,
тесты иных Python/OS, нагрузочные тесты и проверка multi-process.
`pip check` вывел предупреждение о недоступном общем cache pip; проверка зависимостей
прошла. Кеш отключён, состояние глобального Python не изменялось.

## 8. Возникавшие проблемы и исправления

1. Исходный pytest завершался ImportError: проект не импортировался из .venv.
   Выполнена editable-установка существующего пакета. Установка без build isolation
   сначала не прошла из-за отсутствующего hatchling; обычная установка в sandbox
   не имела доступа к PyPI. Разрешённый повтор с доступом к PyPI успешно установил
   бесплатные build-зависимости и пакет.
2. macOS hidden-флаг .pth приводил к его пропуску Python 3.13; флаг возвращался
   между инструментальными сессиями. Для существующей editable-установки добавлена
   локальная ссылка `.venv/.../site-packages/profilabych -> src/profilabych`.
   Это вспомогательная настройка игнорируемого окружения; исходники/requirements
   не подменялись, тесты импортируют актуальный пакет обычными командами.
3. Первые Ruff проверки обнаружили длинные строки и упрощения context managers;
   исправлено форматированием и корректировкой кода.
4. Первый mypy обнаружил Any в расчёте retry delay; добавлено явное float-преобразование.
5. Первый полный прогон дал 170 passed / 1 failed: тест пытался индексировать coroutine
   до await. Исправлена проверка; последующие полные прогоны прошли.
6. При ревью обнаружена возможность повторной записи истории при отмене сразу после
   успешного commit. Исправлена обработка отмены, добавлен регрессионный тест.
7. Нулевой Decimal с огромным отрицательным exponent теперь возвращает `0` до
   форматирования, исключая опасное раздувание строки.
8. Ответы OpenRouter с provider-specific reasoning нормализуются без сохранения
   лишних полей; malformed tool_calls и обрезанные ответы отклоняются.

Дополнительно выполнялись `git status --short`, `rg --files`, чтение AGENTS/README/
architecture/исходников/тестов, `ruff format .`, `ruff check . --fix`, диагностические
Python-команды окружения и установка:

```bash
.venv/bin/python -m pip install --no-deps --no-build-isolation -e .  # первоначально не прошла
.venv/bin/python -m pip install --no-deps --no-cache-dir -e .       # sandbox: не прошла; разрешённый повтор: прошла
```

## 9. Демонстрация Fake LLM

Фактический запуск `.venv/bin/python -m profilabych --demo`:

```text
Пользователь: Посчитай 15 * 4 и сохрани результат как заметку
Fake LLM -> calculator: {"expression":"15 * 4"}
Инструмент [calculate_1] -> {"ok":true,"data":{"result":"60"},"error":null}
Fake LLM -> save_note: {"text":"60"}
Инструмент [save_1] -> {"ok":true,"data":{"note_id":1},"error":null}
Итог: 15 * 4 = 60. Сохранено как заметка №1.
SQLite: Note(id=1, text='60', created_at=...)
/reset: история пуста, заметка 60 сохранена. Сеть не использовалась.
```

Второй ответ Fake LLM строит текст заметки из настоящего результата calculator;
финальный ответ использует настоящий note_id. Затем читается SQLite и проверяется
сохранность заметки после reset. Fake здесь демонстрирует протокол, не качество
рассуждения живой модели.

## 10. Ограничения и следующий этап

- История одного хода атомарна, но инструменты и весь LLM-цикл не образуют одну
  транзакцию. Повтор запроса может создать ещё одну заметку; crash recovery и
  идемпотентность инструментов не реализованы. При отмене commit состояние
  целого хода может быть неопределённым для вызывающего кода.
- Сериализация одного пользователя действует в одном экземпляре Agent/процессе.
- История ограничена сообщениями и символами, не точными токенами конкретной LLM.
- Предварительное резервирование максимальных tool results консервативно: слишком
  маленький лимит истории может отклонить группу даже с потенциально коротким результатом.
- list_notes ограничен страницами и максимальным результатом; для больших заметок
  необходимо уменьшать limit и использовать offset.
- CLI локальный; его user-id флаг не заменяет аутентификацию.
- Каталог и API могут измениться; фактическая бесплатная доступность Qwen не подтверждена.

Готовность к Telegram: ядро и точки сборки готовы. Следует добавить aiogram routers,
команды, получение user_id/chat_id из Telegram-события, представление ответов,
startup/shutdown тех же ресурсов и отдельные интеграционные тесты адаптера.
Для OpenRouter остаётся отдельно разрешённая проверка реального бесплатного endpoint
с ключом Product Owner. Деплой и дополнительные AI-возможности вне этапа 2.

## 11. Файлы и Git

На старте **все проектные файлы уже были untracked**. Staging/commit/push не выполнялись;
итоговый краткий status сохраняет это состояние:

```text
?? .dockerignore
?? .env.example
?? .gitignore
?? AGENTS.md
?? Dockerfile
?? README.md
?? docs/
?? pyproject.toml
?? requirements-dev.txt
?? requirements.txt
?? src/
?? tests/
```

Изменены существующие:

- `.env.example`, `AGENTS.md`, `README.md`, `docs/architecture.md`, `pyproject.toml`;
- `src/profilabych/config.py`, `logging.py`, `main.py`, `__main__.py`;
- `src/profilabych/agent/__init__.py`, `llm/__init__.py`, `storage/__init__.py`, `tools/__init__.py`;
- `tests/conftest.py`, `test_config.py`, `test_logging.py`, `test_structure.py`.

Добавлены:

- `src/profilabych/agent/core.py`;
- `src/profilabych/llm/models.py`, `openrouter.py`, `fake.py`;
- `src/profilabych/tools/registry.py`, `calculator.py`, `current_time.py`, `notes.py`;
- `src/profilabych/storage/contracts.py`, `history.py`, `sqlite.py`;
- `src/profilabych/interfaces/cli.py`, `src/profilabych/demo.py`;
- `tests/test_agent.py`, `test_openrouter.py`, `test_tools.py`, `test_storage.py`, `test_cli.py`;
- `docs/stage-2-report.md`.

Существующий `.env` не читался/не перезаписывался при диагностике; секреты не выводились.
Dockerfile, requirements, gitignore и отчёт этапа 1 сохранены.
Предлагаемое название коммита: `feat: implement stage 2 agent core, tools and SQLite storage`.
Из-за исходного untracked состояния первый коммит потребует отдельного согласования
состава, а не только фиксации tracked diff этапа 2.
