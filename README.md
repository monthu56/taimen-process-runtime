# process-runtime

*English. Russian version: [README.ru.md](README.ru.md)*

BPMN 2.0 / DMN process runtime of the Taimen platform: SpiffWorkflow behind a
resource-service boundary. It implements superproject ADR-0023 (BPMN/DMN as the
definition interface, the engine behind `ProcessRuntimeAdapter`) and ADR-0032 (a workflow
as a separate executor alongside a human, an agent and a service). The engine code was
moved out of platform-core (EPIC-11, PC-ADR-023) on 2026-09-12; the history before the
move lives in the `taimen-platform-core` repository up to commit `cc01ebd`.

In the open-source build of the platform this service ships as the optional
**experimental** `process` profile (umbrella ADR-0040): it is not part of the `core`
profile and is not started by the default set (`make up PROFILES="core edge"`); no new
capabilities are added to it, and defects are fixed only when they break the profile
build or CI. Its future is decided at milestone M4.

## What it is

- **Its own service, its own database.** Tables `workflow_{instances,tasks,timers,transition_log}`
  and the `process_events` journal, an Alembic chain in `migrations/`. Not a single
  reference to platform-core tables: the tenant is the IAM tenant from the token,
  `*_user_id` is the IAM principal.
- **Identity like every other resource service.** Only an IAM access token with audience
  `process-runtime`, verified through `platform-auth-sdk`. Scopes: `process:read`,
  `process:write`, `process:admin` (version migration and acting on behalf of any assignee).
- **Definitions are data, not code.** `definitions/definitions.json` lists the
  workflows (`workflow_id`, `version`, `bpmn_path`, `dmn_paths`, `task_handlers`,
  `signal_correlations`, `is_deprecated`, `is_retired`); the BPMN/DMN files live next to
  it and go through code review. A published version is immutable; a change is a new version.
- **Timers inside the process.** An asyncio loop in the lifespan (`PR_TIMER_TICK_SECONDS`)
  instead of an ARQ cron and a broker; every timer fire is idempotent (`timer:<id>`), advance
  runs under an advisory lock on the instance.
- **Events are a journal with a cursor.** `GET /api/v1/events?cursor=&limit=` in the style
  of the Control Plane and IAM journals; nothing is pushed, the consumer (Control Plane
  reconciliation per ADR-0023 §4) reads on its own.

## Control Plane bridge ([docs/decisions/ADR-001](docs/decisions/ADR-001-control-plane-bridge.md))

The service acts in the core under its own service account ("Taimen Process Runtime", a
principal of kind `service`): every active user task of a process becomes a core `Task` with
the external reference `process-runtime/activity` and `process*` `customFields`; completing
that task in the core (`task.completed`, or `task.updated` with the `terminal_success`
category) advances the process, and the `customFields` go into BPMN as form data. Two pull
loops with durable cursors; a poison event stops the cursor rather than being skipped. It is
enabled automatically once the superproject bootstrap has written
`secrets/process-runtime-iam.env` (`PR_CP_BRIDGE=auto`).

Not done yet: role-based control of human tasks through core bindings (the role goes into
`processLane`), classification of Semantic/Service Tasks as separate core entities, a
publish cycle for definitions (for now: git and code review).

## Running

```bash
uv sync
docker compose -f compose.test.yml up -d --wait db-test
PR_TEST_DATABASE_URL=postgresql+psycopg://process:process@localhost:5437/process_test uv run pytest -q
uv run ruff check . && uv run ruff format --check .
# service: the `process` profile of the superproject root compose (port 18030 on a laptop)
```

Settings are `PR_*` variables (`src/process_runtime/config.py`): `PR_DATABASE_URL`, `PR_IAM_ISSUER`,
`PR_IAM_JWKS_URL` (or `PR_IAM_PUBLIC_KEY[_FILE]`), `PR_IAM_AUDIENCE`, `PR_DEFINITIONS_DIR`,
`PR_ADAPTER` (`spiff` | `dummy`), `PR_SCRIPT_TASK_SANDBOX` (`strict` | `relaxed` | `disabled`,
the latter two only with `PR_ALLOW_SANDBOX_DISABLED=1`), `PR_TIMER_WORKER_ENABLED`.
The image is built from the superproject root: `docker build -f process-runtime/Dockerfile .`.

## SpiffWorkflow license

LGPL-3.0, used as an unmodified library: we do not fork it, fixes are sent upstream, and
the project-specific parts live in `engine/spiff/adapter.py`.
