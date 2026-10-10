# Московское время и безопасное форматирование Telegram

Дата: 10 октября 2026 года. Коммит, push и деплой не выполнялись.

## 1. Что реализовано

Добавлен DEFAULT_TIMEZONE с проверкой IANA timezone и значением Europe/Moscow
по умолчанию. Без явного timezone current_time использует настройку приложения;
явный пояс имеет приоритет. Инструмент отдаёт часы/минуты и готовую фразу,
например «Сейчас в Москве 15:12». ISO 8601, секунды и микросекунды в этом
пользовательском результате отсутствуют.

NoteTools преобразует даты создания в DEFAULT_TIMEZONE для показа:
ДД.ММ.ГГГГ ЧЧ:ММ. SQLite продолжает записывать и возвращать исходные даты в UTC;
миграции и перезаписи существующих заметок нет.

Корректные парные **bold** и inline code преобразуются в явные Telegram entities
при parse_mode=None. HTML, ссылки, кодовые блоки и неполная/неподдерживаемая
разметка остаются буквальным текстом. Смещения считаются в UTF-16 и обрезаются
для каждого фрагмента до 4096 units, включая emoji и пропускаемый whitespace.

## 2. Структура и изменённые файлы этой задачи

| Файл | Изменение |
| --- | --- |
| `.env.example` | DEFAULT_TIMEZONE=Europe/Moscow |
| `src/profilabych/config.py` | Настройка и проверка доступности IANA timezone |
| `src/profilabych/timezones.py` (новый) | Общие преобразования и фразы о времени |
| `src/profilabych/tools/current_time.py` | Настроенный default, явный timezone, ЧЧ:ММ/answer |
| `src/profilabych/tools/notes.py` | Отображение UTC created_at в локальном формате |
| `src/profilabych/main.py` | Передача настройки инструментам через partial/конструктор |
| `src/profilabych/agent/core.py` | Только инструкции о времени и датах; цикл tools не изменён |
| `src/profilabych/interfaces/telegram/formatting.py` (новый) | Ограниченная разметка и clipping entities |
| `src/profilabych/interfaces/telegram/presentation.py` | Отправка entities и сохранение source offsets |
| `tests/test_config.py` | Москва по умолчанию, override, неверные timezone |
| `tests/test_tools.py` | Новый человекочитаемый контракт current_time |
| `tests/test_timezones.py` (новый) | UTC → MSK, смена даты, DST, настройки и хранение UTC |
| `tests/test_telegram.py` | Обновлённый time result и показ дат/ID через Dispatcher |
| `tests/test_telegram_formatting.py` (новый) | Безопасные entities, emoji, длинные ответы, malformed markup |
| `AGENTS.md` | Правила времени и контролируемых entities |
| `README.md` | Настройка, формат времени/дат и описание Telegram formatting |
| `docs/architecture.md` | Границы преобразования дат и отправки entities |
| `docs/timezone-and-formatting-report.md` (новый) | Этот отчёт |

Предыдущие несвязанные с этой задачей изменения рабочего дерева сохранены.
Локальный `.env` в этой задаче не менялся; отсутствие DEFAULT_TIMEZONE использует
новый default Europe/Moscow. Значения секретов не выводились. `.env`, SQLite
и локальные логи исключены из Git. Зависимости и lock-файлы не изменены.

## 3. Технологии

Python 3.13.13, datetime/zoneinfo из стандартной библиотеки, Pydantic Settings,
aiogram MessageEntity, существующие httpx/aiosqlite. Новых зависимостей нет.

## 4. Решения и причины

timezone в схеме инструмента опционален: отсутствие/null означает настройку,
а не жёстко записанный в JSON Schema UTC. Composition root передаёт её через
partial, поэтому инструменты не читают настройки на импорте. Инструкции модели
требуют нового вызова current_time на каждый вопрос о времени и не переносят
явный город предыдущего запроса в следующий общий вопрос.

Даты форматируются перед передачей результата инструмента модели. Ей не нужно
самостоятельно прибавлять три часа или переписывать ISO timestamp. Изменения
в хранилище не требуются. Результаты current_time.time и list_notes.created_at
теперь имеют пользовательский формат; внутренний Note.created_at остаётся ISO UTC.

Telegram получает собственные entities вместо parse_mode=Markdown/HTML.
Поддерживается небольшой набор разметки без вложенных styles и автоматических
ссылок. Порядок фрагментов, link preview=false и allow_paid_broadcast=false сохранены.

## 5. Проверки и выполненные команды

Исходная база для этой задачи: 255 тестов. Финальная: **292 passed**,
добавлено 37 тестовых случаев. В финальном прогоне нет падающих тестов.

```text
.venv/bin/python -m pytest tests/test_timezones.py tests/test_telegram_formatting.py tests/test_tools.py tests/test_config.py tests/test_telegram.py -q
    164 passed (до последних трёх проверок пограничной разметки)
.venv/bin/python -m pytest
    292 passed in 14.60s (финал)
.venv/bin/python -m ruff check .
    All checks passed
.venv/bin/python -m ruff format --check .
    50 files already formatted
.venv/bin/python -m mypy src
    no issues in 30 source files
.venv/bin/python -m pip check
    No broken requirements found
.venv/bin/python -m profilabych --check
    exit 0; конфигурация без сети/БД
.venv/bin/python -m profilabych --demo
    exit 0; calculator → save_note → reset, без сети
git diff --check
    exit 0
```

Ruff format применён к четырём изменённым файлам. pip отключил недоступный
в sandbox каталог кеша; проверка совместимости завершилась успешно.
Проверка кандидатов Git на точные значения известных секретов: совпадений нет.

Реальный smoke через прежний Agent/OpenRouterProvider и временную SQLite:

- «Сколько сейчас времени в Токио?» — модель вызвала current_time с Asia/Tokyo;
  финальный ответ соответствует «Сейчас в Токио ЧЧ:ММ».
- Следующий вопрос в той же истории «Че по времени?» — модель снова вызвала
  current_time, не передавала timezone; использована Europe/Moscow, ответ
  соответствует «Сейчас в Москве ЧЧ:ММ».
- Четыре completion-запроса: OpenRouter сообщил стоимость 0 для каждого.
  provider.max_price=0, require_parameters=true и allow_fallbacks=false сохранены.

Этот smoke не является подтверждением новых пользовательских сценариев Telegram.
Telegram-интеграционные тесты используют настоящий Dispatcher и mock BaseSession
без сети. Проверка создания/дат/форматирования новых ответов в живом чате
ожидает сообщений Product Owner.

Локальный polling после обновления перезапущен. getMe и первый getUpdates
успешны, ошибок запуска нет. Новые входящие сообщения на момент отчёта
ещё не получены. Логи хранятся локально с правами 600 и исключены из Git.

## 6. Обнаруженные проблемы и исправления

UTC был default инструмента; NoteTools отдавал исходный ISO timestamp;
parse_mode=None оставлял **ID:** видимым текстом. Исправлены настройки,
формирование результата инструментов и контролируемый entity renderer.

Первый целевой прогон выявил интерпретацию тройных звёздочек как bold;
границы распознавания исправлены. Первые Ruff проверки выявили длинные строки;
они отформатированы. Дополнительно исправлено смещение entities при пробелах
в начале/конце inline code и сохранена буквальная незакрытая fenced-разметка.
Все эти случаи покрыты тестами и прошли финальный прогон.

## 7. Оставшаяся работа и готовность

Повторить новые сценарии в Telegram. Полный Markdown не реализован:
форматируются bold и inline code, остальные конструкции видны обычным текстом.
Качество выбора IANA timezone для города зависит от модели; поддерживаемые IANA
пояса проверяются ZoneInfo. Москва и Токио проверены также реальной моделью.

Код готов к повторной локальной Telegram/OpenRouter интеграционной проверке.
Готовность к деплою этой задачей не подтверждается: деплой и Docker build
не выполнялись. Git-коммит требует отдельного подтверждения Product Owner.

## 8. Следующий этап: повторить в Telegram

1. «Сколько сейчас времени?» — Москва, только часы и минуты.
2. «Который час?» — Москва.
3. «Че по времени?» — Москва.
4. «Сколько сейчас времени в Токио?» или «Который час в Europe/London?» — явный пояс.
5. После другого города снова общий вопрос о времени — возврат к Москве.
6. Сохранить тестовую заметку и вывести заметки — дата ДД.ММ.ГГГГ ЧЧ:ММ по Москве;
   если ответ содержит **ID:**, должна отображаться жирная метка ID: без звёздочек.
7. /reset и повторный вывод заметок — заметки и даты сохранились.
