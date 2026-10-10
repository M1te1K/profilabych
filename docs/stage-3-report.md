# Этап 3: Telegram Interface + Live LLM Integration

Дата: 10 октября 2026 года. Репозиторий продолжен от `7ec973d` в ветке `main`.
Исходное рабочее дерево было чистым. Коммит, push, деплой и реальные API-запросы
не выполнялись. Офлайн-реализация завершена; live-проверка ожидает ключей и разрешения.

## 1. Что реализовано

Добавлен `python -m profilabych --telegram` с aiogram polling. Сохранены CLI,
`--check` и `--demo`. Работают `/start`, `/help`, `/reset` и обычные текстовые
запросы в личном чате. Пользовательский текст передаётся существующему Agent.
Через прежний Registry доступны calculator, current_time, save_note и list_notes,
включая последовательную цепочку calculator → save_note.

Добавлены проверки пустого/слишком длинного ввода, подсказка для неподдерживаемого
контента, игнорирование групп/каналов/ботов/редактирований, typing, отправка длинного
plain text и защита от повторных update. Telegram API errors не останавливают
обработку остальных пользователей. Ошибки LLM возвращают заданную нейтральную фразу.
Все три ресурса закрываются после завершения отмены активных обработчиков.

## 2. Структура и изменённые файлы

| Файл | Изменение |
| --- | --- |
| `src/profilabych/interfaces/telegram/handlers.py` (новый) | Команды, проверка ввода, доверенные ID, error boundary |
| `src/profilabych/interfaces/telegram/presentation.py` (новый) | UTF-16 разбиение, plain text, typing, Telegram delivery errors/retry |
| `src/profilabych/interfaces/telegram/middleware.py` (новый) | Фильтрация, резервирование update_id, ограниченный кеш, ожидание shutdown |
| `src/profilabych/interfaces/telegram/polling.py` (новый) | Dispatcher, polling, shield/stop, lifecycle |
| `src/profilabych/interfaces/telegram/__init__.py` | Обновлено описание пакета |
| `src/profilabych/main.py` | Режим Telegram, проверки ключей/токена, сборка одного Agent/Bot, ресурсные контексты |
| `src/profilabych/agent/core.py` | Только нейтральный ответ для LLMError; цикл tools не переписан |
| `src/profilabych/llm/openrouter.py` | Сообщение отсутствующего ключа распространяется на оба интерфейса |
| `src/profilabych/logging.py` | Защита от сырых aiogram сообщений/traceback, подавление aiohttp debug |
| `src/profilabych/interfaces/cli.py` | Обновлён docstring |
| `tests/telegram_fakes.py` (новый) | Mock BaseSession с GetMe/GetUpdates/SendMessage/SendChatAction |
| `tests/test_telegram.py` (новый) | 43 проверки Telegram через реальный Dispatcher |
| `tests/test_telegram_lifecycle.py` (новый) | 18 проверок остановки, ошибок, повторов и безопасности |
| `tests/test_agent.py` | Проверки нейтрального LLM-ответа при сохранении целостности истории |
| `tests/test_cli.py` | Telegram без ключей и stage=3 в check |
| `.env.example` | Назначение ключей и подсказки без секретов |
| `README.md` | BotFather/OpenRouter, запуск, команды, ошибки, ограничения и live-порядок |
| `AGENTS.md` | Актуальный этап и правила Telegram/lifecycle/tests |
| `docs/architecture.md` | Связь интерфейса с ядром, доверенный контекст, shutdown, дедупликация |
| `docs/stage-3-report.md` (новый) | Этот отчёт |

Существующий `.env` не изменён. Новых настроек и зависимостей нет; requirements
lock-файлы и Dockerfile не изменены.

## 3. Как Telegram связан с Agent Core; технологии

Используются Python 3.13.13, установленный aiogram 3.31.0, httpx, aiosqlite,
Pydantic/pydantic-settings, pytest/pytest-asyncio, Ruff и mypy.

Composition root: Settings → logging → HTTP/OpenRouter preflight → SQLite →
один Agent с прежним Registry → Bot/Dispatcher. `from_user.id` передаётся как
user_id, `chat.id` как chat_id. Agent сам управляет историей, tools и лимитами;
адаптер не содержит SQL, промпта или второго цикла LLM.
`/reset` проходит через ту же сериализацию пользователя и удаляет историю
только текущего чата. Заметки сохраняются.

## 4. Команды и принятые решения

| Команда/поведение | Результат |
| --- | --- |
| `/start` | Приветствие, команды и инструменты |
| `/help` | Справка и пример цепочки действий |
| `/reset` | История текущего чата очищена; заметки остаются |
| Обычный текст | Agent.handle с доверенными user_id/chat_id |
| Прочие команды | Подсказка /help без вызова LLM |

Plain text с явным parse_mode=None исключает ошибки Markdown/HTML. Фрагменты
ограничены 4096 UTF-16 units, включая emoji; link preview и платная рассылка
выключены. Короткий Telegram RetryAfter повторяется один раз (до 5 секунд);
неоднозначный результат сетевой отправки не повторяется автоматически.

Дедупликация выполняется до await, активные ID не вытесняются завершёнными.
Кеш 10000 ID ограничивает память и не претендует на долговременную exactly-once
гарантию. До 16 update handlers работают параллельно; Agent сериализует user_id.

Бюджетные проверки OpenRouter сохранены: явный :free, preflight нулевых тарифов
и tools/tool_choice, max_price=0, require_parameters=true, allow_fallbacks=false.
Недоступность модели останавливает startup; runtime LLMError возвращает строго
«Сервис временно недоступен. Попробуйте позже.». Платной замены и Fake fallback нет.

## 5. Пройденные проверки

Исходная база: **191 passed**. Финальная база: **253 passed**, то есть добавлено
62 тестовых случая. Падающих тестов в финальном прогоне нет.

| Требование | Проверено офлайн |
| --- | --- |
| 1. /start | Приветствие, справка, deep-link payload |
| 2. /help | Команды и четыре инструмента |
| 3. /reset | Вызов Agent, чатовая изоляция, безопасная ошибка БД |
| 4. Обычный текст | Ответ, доверенные ID, контекст следующего хода |
| 5. Калькулятор | Настоящий calculator, результат 60 |
| 6. Сохранение заметки | Настоящая SQLite, владелец из Telegram |
| 7. Чтение заметок | Только заметки текущего пользователя |
| 8. Последовательные инструменты | calculator → save_note с сохранением порядка/ID |
| 9. Длинные ответы | Разбиение, emoji, literal Markdown/HTML, blank chunks |
| 10. Telegram API errors | BadRequest, Forbidden, Network, RetryAfter, ошибки typing |
| 11. OpenRouter errors | Fake LLMError и настоящий Provider на MockTransport; paid/no_tools fail closed |
| 12. Некорректные сообщения | Пустые, слишком длинные, неизвестные команды, media, запрещённые события |
| 13. Изоляция | История/заметки, подмена user_id, конкурентные пользователи |
| 14. Повторные update | Завершённые, одновременно активные, после ошибки доставки, границы кеша |
| 15. Закрытие ресурсов | stop/cancel, getMe/preflight/SQLite failure, активная LLM, отмена typing/error reply |
| 16. История | Целые ходы, tool_call_id, повторное открытие SQLite |
| 17. Заметки после reset | Содержимое осталось, чужие чаты/пользователи не затронуты |

Все Telegram/LLM-сценарии проверены **только на моках**. Инструменты, SQLite,
история, логирование, конфигурация, CLI/demo и механизм asyncio выполнялись
локально настоящим кодом. Действительность ключей, Telegram-сервис, текущая
доступность модели и реальный выбор инструментов LLM **не проверены**.

Выполненные команды проверки:

```text
.venv/bin/python -m pytest -q                           191 passed (до изменений)
.venv/bin/python -m pytest tests/test_telegram.py -q     43 passed
.venv/bin/python -m pytest tests/test_telegram_lifecycle.py -q
.venv/bin/python -m pytest                              253 passed (финал)
.venv/bin/python -m ruff format src tests                формат исправлен
.venv/bin/python -m ruff check .                        All checks passed
.venv/bin/python -m ruff format --check .                45 files already formatted
.venv/bin/python -m mypy src                            no issues in 28 source files
.venv/bin/python -m pip check                           No broken requirements found
.venv/bin/python -m profilabych --check                  exit 0, stage=3, без сети/БД
.venv/bin/python -m profilabych --demo                   exit 0, calculator → save_note → reset
.venv/bin/python -m profilabych --telegram               exit 1, ожидаемый отказ: отсутствуют оба ключа
git diff --check                                       exit 0
git check-ignore .env                                  .env исключён
git status
git diff --stat
```

Первичная Ruff-проверка обнаружила длину строк и ASYNC109 у методов mock BaseSession.
Формат исправлен; ASYNC109 адресно исключён для сигнатур, обязательных по контракту
aiogram. После изменений все обязательные проверки повторены и прошли.
pip сообщил о недоступном каталоге кеша в sandbox и отключил кеш; проверка
совместимости зависимостей завершилась успешно. Права каталогов не изменялись.

## 6. Исправленные проблемы

Прежний Agent включал описание LLM-ошибки в ответ. Теперь описание не передаётся
пользователю или истории, код ошибки остаётся в безопасном логе. После ошибки
сохраняются завершённые tool results и уже записанные заметки.

aiogram автоматически не дожидается всех update handlers на shutdown, а прямая
отмена start_polling может оставить внутренний getUpdates worker. Wrapper shield
обеспечивает stop_polling; UpdateGuard отменяет и ждёт обработчики, включая ответ
framework error handler, прежде чем закрываются инфраструктурные ресурсы.

Aiogram способен логировать сырые API-описания/traceback. JSON formatter заменяет
framework payload на безопасное событие, собственные логи содержат только тип
ошибки и идентификаторы. Неизвестные секреты в сырых API-телах также не выводятся.

## 7. Что требуется для live-тестирования

Безопасная проверка Settings вывела только признаки наличия. Результат:
TELEGRAM_BOT_TOKEN отсутствует, OPENROUTER_API_KEY отсутствует, LLM_MODEL проходит
проверку формата бесплатного ID. `.env` существует, права уже `600`, файл игнорируется
Git. Его содержимое и реальные секреты не выводились и не изменялись.

Токен получает Product Owner у [BotFather](https://t.me/BotFather) через `/newbot`
([официальная инструкция](https://core.telegram.org/bots/tutorial#obtain-your-bot-token)).
OpenRouter-ключ создаётся на [API Keys](https://openrouter.ai/settings/keys)
([документация](https://openrouter.ai/docs/api/reference/authentication)).
Ключи следует вводить только через локальный редактор в существующий `.env`,
сохранив остальные настройки и права 600. В чат, shell history и отчёт они не нужны.

После локального добавления ключей необходимо отдельное подтверждение Product Owner
перед любыми реальными API-запросами — прямое требование этапа 3, пункт 8.
Следующая проверка: бесплатный preflight, /start, вопрос, время, калькулятор,
save_note, list_notes, calculator → save_note, /reset и контроль сохранности заметки.
Реальный Telegram-сценарий требует входящих сообщений человека; бот не может
отправить команду самому себе как пользователь. Технический запуск и диагностика
выполняются разработчиком; проверка личного чата согласуется с Product Owner.
Если tools не поддерживаются, live-проверка прекращается с явным отказом;
альтернативный бесплатный ID выбирается по актуальному каталогу после разрешения.

## 8. Оставшиеся ограничения

Поддерживаются личные текстовые чаты, один процесс, один polling-процесс на токен.
Нет durable deduplication, общей транзакции хода и заметок, crash recovery,
распределённой сериализации или гарантированной доставки Telegram. Уже выполненная
операция может сохраниться до LLM-ошибки, отмены или недоставленного ответа.
Недоставленный ответ остаётся в истории; перезапрос с новым ID может повторить действие.
Бесплатные endpoints/квоты меняются, текущая пригодность Qwen не подтверждена.
RAG, PDF, голос, дополнительные агенты и платные fallback не добавлялись.

## 9. Готовность к деплою и следующий этап

Код Telegram/OpenRouter готов к согласованной live-интеграционной проверке.
Готовность к деплою пока не подтверждена: ключей нет, live и Docker build не
выполнялись. Деплой/изменения аккаунтов не разрешены и не выполнялись.
Следующий шаг — ключи, разрешение и минимальный live smoke test; дальнейший этап
и архитектурные расширения определяет Product Owner.

Предложение следующего коммита (не создан):
`feat: add Telegram polling interface with safe LLM integration`

## Git после завершения

Ниже приведён фактический вывод команд. `git diff --stat` показывает только
отслеживаемые изменения: новые файлы остаются untracked, индекс не изменён.

```text
$ git status
On branch main
Changes not staged for commit:
  (use "git add <file>..." to update what will be committed)
  (use "git restore <file>..." to discard changes in working directory)
	modified:   .env.example
	modified:   AGENTS.md
	modified:   README.md
	modified:   docs/architecture.md
	modified:   src/profilabych/agent/core.py
	modified:   src/profilabych/interfaces/cli.py
	modified:   src/profilabych/interfaces/telegram/__init__.py
	modified:   src/profilabych/llm/openrouter.py
	modified:   src/profilabych/logging.py
	modified:   src/profilabych/main.py
	modified:   tests/test_agent.py
	modified:   tests/test_cli.py

Untracked files:
  (use "git add <file>..." to include in what will be committed)
	docs/stage-3-report.md
	src/profilabych/interfaces/telegram/handlers.py
	src/profilabych/interfaces/telegram/middleware.py
	src/profilabych/interfaces/telegram/polling.py
	src/profilabych/interfaces/telegram/presentation.py
	tests/telegram_fakes.py
	tests/test_telegram.py
	tests/test_telegram_lifecycle.py

no changes added to commit (use "git add" and/or "git commit -a")

$ git diff --stat
 .env.example                                    |   5 +-
 AGENTS.md                                       |  28 ++++-
 README.md                                       | 131 ++++++++++++++++++++----
 docs/architecture.md                            |  87 ++++++++++++++--
 src/profilabych/agent/core.py                   |  10 +-
 src/profilabych/interfaces/cli.py               |   2 +-
 src/profilabych/interfaces/telegram/__init__.py |   5 +-
 src/profilabych/llm/openrouter.py               |   2 +-
 src/profilabych/logging.py                      |   9 +-
 src/profilabych/main.py                         |  62 ++++++++---
 tests/test_agent.py                             |   9 +-
 tests/test_cli.py                               |   8 +-
 12 files changed, 292 insertions(+), 66 deletions(-)
```
