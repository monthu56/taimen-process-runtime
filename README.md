# process-runtime

BPMN 2.0 / DMN process runtime платформы Taimen: SpiffWorkflow за границей resource
service. Исполняет ADR-0023 суперпроекта (BPMN/DMN как интерфейс определения, движок
за `ProcessRuntimeAdapter`) и ADR-0032 (workflow как отдельный исполнитель наряду с
человеком, агентом и сервисом). Код движка перенесён из platform-core (EPIC-11,
PC-ADR-023) 2026-09-12; история до переноса — в репозитории `taimen-platform-core`
до коммита `cc01ebd`.

## Что это

- **Свой сервис, своя БД.** Таблицы `workflow_{instances,tasks,timers,transition_log}`
  и журнал `process_events`, Alembic-цепочка в `migrations/`. Ни одной ссылки на
  таблицы platform-core: tenant — tenant IAM из токена, `*_user_id` — principal IAM.
- **Identity как у всех resource services.** Только access token IAM audience
  `process-runtime`, проверка через `platform-auth-sdk`. Scopes: `process:read`,
  `process:write`, `process:admin` (миграция версий и действие за любого assignee).
- **Определения — данные, не код.** `definitions/definitions.json` перечисляет
  workflow (`workflow_id`, `version`, `bpmn_path`, `dmn_paths`, `task_handlers`,
  `signal_correlations`, `is_deprecated`, `is_retired`); файлы BPMN/DMN лежат рядом и
  проходят code review. Опубликованная версия неизменяема, правка — новая версия.
- **Таймеры внутри процесса.** Asyncio-цикл в lifespan (`PR_TIMER_TICK_SECONDS`)
  вместо ARQ-cron и брокера; каждый timer fire идемпотентен (`timer:<id>`), advance
  под advisory-lock на instance.
- **События — журнал с курсором.** `GET /api/v1/events?cursor=&limit=` в стиле журналов
  Control Plane и IAM; ничего не пушится, потребитель (reconciliation Control Plane по
  ADR-0023 §4) читает сам.

## Чего здесь пока нет (следующие задачи, ряд control-plane)

- `ProcessTaskBinding`: материализация User/Semantic Task в `Task` Control Plane и
  доставка завершений обратно (ADR-0023 §3, §5);
- `ProcessReconciliationController` и capability manifest адаптера (§2, §4);
- ролевой контроль human task через binding'и Control Plane — сейчас задача с
  `assigned_role` доступна любому писателю tenant'а, с `assigned_user_id` — только ему
  и `process:admin`;
- сигналы из событий других сервисов (в platform-core это делал FastStream-мост):
  сигнал подаётся через `POST /instances/{id}/signal`.

## Запуск

```bash
uv sync
docker compose -f compose.test.yml up -d --wait db-test
PR_TEST_DATABASE_URL=postgresql+psycopg://process:process@localhost:5437/process_test uv run pytest -q
uv run ruff check . && uv run ruff format --check .
# сервис: профиль process корневого compose суперпроекта (порт 18030 на ноутбуке)
```

Переменные — `PR_*` (`src/process_runtime/config.py`): `PR_DATABASE_URL`, `PR_IAM_ISSUER`,
`PR_IAM_JWKS_URL` (или `PR_IAM_PUBLIC_KEY[_FILE]`), `PR_IAM_AUDIENCE`, `PR_DEFINITIONS_DIR`,
`PR_ADAPTER` (`spiff` | `dummy`), `PR_SCRIPT_TASK_SANDBOX` (`strict` | `relaxed` | `disabled`,
последние два только с `PR_ALLOW_SANDBOX_DISABLED=1`), `PR_TIMER_WORKER_ENABLED`.
Образ собирается из корня суперпроекта: `docker build -f process-runtime/Dockerfile .`.

## Лицензия SpiffWorkflow

LGPL-3.0, используется как немодифицированная библиотека: форк не делаем, исправления
отправляем upstream, специфика живёт в `engine/spiff/adapter.py`.
