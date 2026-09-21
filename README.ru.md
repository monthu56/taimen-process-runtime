*Русская версия. English: [README.md](README.md)*

# process-runtime

BPMN 2.0 / DMN process runtime платформы Taimen: SpiffWorkflow за границей resource
service. Исполняет ADR-0023 суперпроекта (BPMN/DMN как интерфейс определения, движок
за `ProcessRuntimeAdapter`) и ADR-0032 (workflow как отдельный исполнитель наряду с
человеком, агентом и сервисом). Код движка перенесён из platform-core (EPIC-11,
PC-ADR-023) 2026-09-12; история до переноса — в репозитории `taimen-platform-core`
до коммита `cc01ebd`.

В открытой сборке платформы сервис поставляется как опциональный
**experimental**-профиль `process` (ADR-0040 umbrella): в профиль `core` не входит,
набором по умолчанию (`make up PROFILES="core edge"`) не поднимается; новых
возможностей в нём не делается, дефекты чинятся только если ломают сборку профиля
или CI. Его судьба решается на вехе M4.

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

## Мост в Control Plane ([docs/decisions/ADR-001](docs/decisions/ADR-001-control-plane-bridge.md))

Сервис действует в ядре своим service account («Taimen Process Runtime», principal вида
`service`): каждая активная user task процесса становится `Task` ядра с external reference
`process-runtime/activity` и `customFields` `process*`; завершение этой задачи в ядре
(`task.completed`, `task.updated` с категорией `terminal_success`) продвигает процесс, `customFields`
уходят в BPMN как данные формы. Два pull-цикла с durable cursor'ами, poison-событие
останавливает курсор, а не пропускается. Включается автоматически, когда bootstrap
суперпроекта положил `secrets/process-runtime-iam.env` (`PR_CP_BRIDGE=auto`).

Ещё не сделано: ролевой контроль human task через binding'и ядра (роль идёт в
`processLane`), классификация Semantic/Service Task как отдельных сущностей ядра,
publish-цикл определений (пока git и code review).

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
