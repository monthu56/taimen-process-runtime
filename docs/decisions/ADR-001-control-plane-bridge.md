# ADR-001: Мост в Control Plane — activity как Task, завершение Task как команда движку

- Статус: Accepted
- Дата: 2026-09-12
- Исполняет: ADR-0023 суперпроекта (§2 manifest, §3 материализация, §4 корреляция, §5 без
  распределённой транзакции), ADR-0032 (workflow как отдельный исполнитель)

## Контекст

Движок процессов не должен держать вторую вселенную задач: организационная работа живёт в
Control Plane как `Task`, движок только предлагает следующий допустимый шаг. Control Plane
уже даёт всё, что нужно для binding'а без изменений в ядре: generic external references
(CP-ADR-0034/0047), задачи с `customFields`, журнал событий с opaque cursor, idempotent
команды.

## Решение

1. **Мост живёт в process-runtime**, действует в Control Plane как principal вида `service`
   («Taimen Process Runtime», service account IAM, audience `control-plane`, права
   `tasks.read/write`, `events.read`, `projects.read`, `workspaces.read`). Ядро не знает о
   SpiffWorkflow, его API не расширяется.
2. **Binding — external reference ядра**: `external_system=process-runtime`,
   `external_type=activity`, `external_id=<instance>/<task_id>/<event_id>` на `Task`.
   Локальная таблица `process_task_bindings` хранит только reconciliation-метаданные
   (cp_task_id, статус `open|completing|completed|cancelled`, событие-источник) — ровно то,
   что ADR-0023 §4 разрешает держать отдельно.
3. **Два pull-цикла с durable cursor'ами** (`bridge_cursors`), по образцу оркестратора
   bidops: poison-событие останавливает курсор перед собой и повторяется, а не
   пропускается.
   - Outbound (журнал процесса → ядро): `task_created` → `POST /tasks`
     (Idempotency-Key = событие) с `customFields` `processInstanceId`, `processWorkflowId`,
     `processWorkflowVersion`, `processTaskId`, `processLane`, `processSubject` +
     external reference; `task_completed` → `POST /tasks/{id}:complete`, если завершение
     пришло не из ядра; терминал instance → комментарий и переход открытых Task в статус
     категории `terminal_cancelled` (первый такой target из `GET /tasks/{id}/transitions`).
   - Inbound (журнал ядра → процесс): `task.completed` и `task.updated` с
     `systemStatusCategory=terminal_success` → `advance(task_complete)` с `form_payload` из
     `customFields` Task (без ключей `process*`), `dedup_key = cp:<event id>`;
     категория `terminal_cancelled` закрывает binding и оставляет токен ждать оператора — движок
     не двигает процесс без подтверждённой команды.
4. **Эхо не замыкается**: перед advance binding переводится в `completing`, и outbound,
   увидев `task_completed` для такого binding'а, закрывает его без вызова ядра.
5. **Manifest** `GET /api/v1/manifest`: версии стандартов, executable subset, extension
   profile, операции и правила binding'а.

## Что не сделано и почему

- Ролевой контроль human task через binding'и ядра: пока задача с `assigned_role`
  доступна любому писателю tenant'а; роль попадает в `processLane` для маршрутизации в
  ядре, дальше — политика ядра (ADR-0025 суперпроекта).
- Semantic Task с capability requirements и Service Task как технический job без Task —
  все activity, кроме user task, исполняются движком сами; отдельная классификация —
  следующее решение.
- Publish-цикл определений (Draft → Published): каталог — git и code review.

## Последствия

- Оператор видит шаги процессов в консоли ядра как обычные задачи и закрывает их там;
  процесс продвигается сам в пределах `PR_BRIDGE_POLL_SECONDS`.
- Один мост — один tenant IAM (`PR_BRIDGE_TENANT_ID`): инстансы других tenant'ов в ядро не
  попадают, их activity закрываются через API сервиса.
- Аудит двусторонний: в ядре actor завершения — principal оператора, в журнале процесса —
  `actor_user_id` из события ядра.
