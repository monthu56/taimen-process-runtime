# Contributing to Taimen Process Runtime

Thank you for taking the time to contribute. Taimen is an organizational
runtime in which people, AI agents, workflows and services execute the work
of an organization; the platform is developed in the open under the
Apache License 2.0. This repository holds the Process Runtime: the BPMN 2.0 /
DMN engine (SpiffWorkflow) behind a resource-service API, one component of the
[Taimen umbrella repository](https://github.com/monthu56/taimen).

## Before you start

- Read the [Product Vision](https://github.com/monthu56/taimen/blob/main/docs/product-vision.md)
  and the [ADR registry](https://github.com/monthu56/taimen/blob/main/docs/adr/README.md)
  of the umbrella repository. Architecture decisions are recorded as ADRs (in
  Russian, with an English title line); English summaries are provided on
  request in the ADR's discussion. This service implements the umbrella's
  ADR-0023 (BPMN/DMN as the process definition interface) and ADR-0032 (the
  process runtime as a resource service).
- Decisions local to this component live in [`docs/decisions/`](docs/decisions/)
  (`ADR-001-…`, same format: Russian text with an English title line). A change
  to the engine boundary, the event journal or the Control Plane bridge needs a
  new ADR there.
- Check the [roadmap](https://github.com/monthu56/taimen/blob/main/docs/roadmap.md)
  and open issues before starting a large change. For anything that changes an
  API, a data model or a service boundary, open an issue first and propose an
  ADR.

## Contributor License Agreement

We require a signed Contributor License Agreement (CLA) for every
contribution, so that the project can be relicensed or defended without
tracking down every author. The CLA is checked by cla-assistant on each pull
request; you sign once.

- Individuals: [`cla/CLA-individual.md`](https://github.com/monthu56/taimen/blob/main/cla/CLA-individual.md)
- Companies contributing on behalf of employees: [`cla/CLA-entity.md`](https://github.com/monthu56/taimen/blob/main/cla/CLA-entity.md)

The CLA grants the project a copyright and patent licence to your
contribution; you keep your copyright.

## Development setup

The component is a Python 3.12 project managed with [uv](https://docs.astral.sh/uv/).
It depends by path on two sibling repositories of the umbrella (see
`[tool.uv.sources]` in `pyproject.toml`): `../platform-auth-sdk` (access-token
verification) and `../control-plane/client` (the Control Plane client used by
the bridge). Work from the umbrella checkout, or keep both checked out next to
this repository under those names:

```bash
git clone --recurse-submodules https://github.com/monthu56/taimen.git taimen
cd taimen/process-runtime
uv sync                                   # runtime deps + the `dev` group (pytest, ruff, httpx, pyjwt)
uv run ruff check . && uv run ruff format --check .
uv run pytest -q                          # tests marked `db` are skipped without a database
```

Tests against a real PostgreSQL (store, migrations, runtime, API) read
`PR_TEST_DATABASE_URL`; `compose.test.yml` starts a throw-away PostgreSQL 16 on
`127.0.0.1:5437` for them:

```bash
docker compose -f compose.test.yml up -d --wait db-test
PR_TEST_DATABASE_URL=postgresql+psycopg://process:process@localhost:5437/process_test uv run pytest -q
docker compose -f compose.test.yml down
```

From the umbrella root the same run is `make check-process-runtime` (ruff plus
the full test suite, as in CI); it starts and stops the test database itself.
The Docker image is built from the umbrella root, because the path
dependencies must be inside the build context:
`docker build -f process-runtime/Dockerfile .`.

Things to know when changing the code:

- Runtime settings are `PR_*` variables (`src/process_runtime/config.py`).
- Process definitions are data, not code: `definitions/definitions.json` lists
  the workflows and the BPMN/DMN files next to it. A published version is
  immutable; a change is a new version (`tests/test_definitions.py` checks the
  catalogue).
- Schema changes go through Alembic (`migrations/`, `alembic.ini`): add the
  migration together with the table change and cover it in
  `tests/test_migrations.py`.
- SpiffWorkflow is LGPL-3.0 and is used as an unmodified library. Do not vendor
  or fork it; send fixes upstream and keep engine-specific code in
  `src/process_runtime/engine/spiff/`.

## Pull requests

- One logical change per pull request; keep the history linear (rebase, no
  merge commits).
- Tests and `ruff check` / `ruff format --check` must pass; behaviour changes
  come with tests.
- Commit messages explain *why*, not *what*; reference the ADR or issue.
- Public API changes (routes, schemas, env variables, the event journal
  format) update this component's docs and, when they break compatibility, the
  umbrella's `docs/migration-vX.Y.md`.
- The pull request template asks you to confirm the CLA and that no secrets,
  customer data or internal hostnames are included.

## Reporting bugs and security issues

Bugs: open an issue in this repository with the version, steps to reproduce
and logs. Security issues: see [SECURITY.md](SECURITY.md) and do not open a
public issue.

## Code of conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md).
