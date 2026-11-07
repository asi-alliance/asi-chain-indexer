# ASI Chain Indexer — contributor instructions

## Scope and source selection

- Read [README.md](README.md) and inspect the affected implementation before changes.
  Use this repository's `dev` integration branch as the ordinary base and PR target.
  Fetch and record exact refs; preserve unrelated working-tree edits and checkouts.
- Work in a feature branch and publish reviewed changes through a PR. Never push
  directly to `dev`, `main`, `master` or `asi-main`. Do not force push,
  rebase, amend, reset or stash existing work. PR merge, deployment, image publication
  and destructive operations require the owner's applicable authorization.
- Write new code, comments, documentation, commits and public PR/issue text in English.
  Link related public issues/PRs in PR descriptions; keep private coordination, local
  machine paths, credentials and internal evidence out of public content.
- Read actual files rather than treating another branch or open PR as implemented.
  [DAG.md](DAG.md) preserves a dated feature-revision snapshot; its branch-status table
  is historical. Prefer current implementation and migrations when they differ.

## Environment and generated files

- [Dockerfile](Dockerfile), [requirements.txt](requirements.txt),
  [requirements-dev.txt](requirements-dev.txt), their lockfiles and the workflows
  define the supported environment. Follow [DEPENDENCIES.md](DEPENDENCIES.md).
  Install development dependencies from `requirements-dev.lock` with
  `--require-hashes`. Update and review both locks when changing dependency pins.
- Generate `src/grpc_stubs/` using `make protos PYTHON=python` and the locked
  generator. Do not commit or hand-edit generated stubs. Keep generator/runtime
  compatibility and the Docker builder consistent with the vendored `protos/`.
- Use the Docker `test` target for tests; production images intentionally omit test
  tools. Do not add development dependencies to the runtime image to run a test.
- Keep virtual environments, caches, environment files, downloaded dependencies and
  generated output out of commits. Do not modify vendored dependency directories as
  a side effect of a documentation or application change.

## Data and protocol invariants

- Monetary values must remain exact. Use [src/amounts.py](src/amounts.py) and the
  ORM `Amount` type; do not introduce float conversion, implicit decimal rounding
  or lossy JSON numbers. Raw ledger amounts are uint256-bounded; historical values
  and per-row `token_decimals` must survive migrations and repair unchanged.
  Preserve `HASURA_GRAPHQL_STRINGIFY_NUMERIC_TYPES=true`.
- Keep vendored wire field types distinct from database capacity. Wider numeric
  storage does not authorize widening protobuf fields or changing signing/ABI rules.
  Review the matching node contract and consumer behavior for protocol changes.
- Identify blocks by hash, retain all ordered DAG parent edges and allow multiple
  siblings per height. Keep pending visibility, inclusion, execution and finality
  separate. Preserve cursor retry behavior after unavailable or invalid node data.
- Native transfers come from validated, phase-marked execution reports, not source
  regexes. Preserve pending/rejected/coalesced observations and later completions;
  only successful committed movements belong in the transfer ledger. Missing or
  incompatible reports must not silently become successful zero-transfer results.
- Keep node access read-only. Do not add signing, funding, transfers, propose calls
  or node recovery/state deletion as part of polling, probes or diagnostics.

## Migrations and Hasura

- Read [migrations/README.md](migrations/README.md) and
  [POSTGRES_UPGRADE.md](POSTGRES_UPGRADE.md) before schema or deployment changes.
  Current migrations are explicit SQL; there is no Alembic environment/revision
  runner. Compose init scripts apply only to an empty PostgreSQL volume, and
  SQLAlchemy `create_all()` does not upgrade an existing schema.
- Preserve already shipped migrations. Add reviewed forward migrations when needed;
  include SQL views, functions, triggers, data transitions and Hasura consequences,
  not just ORM columns. Never blindly replay old migrations on an existing database.
- Backups, stopping writes, missing migration selection and native-transfer history
  repair are explicit operator steps. Do not clear repair flags to bypass failures.
- Metadata setup scripts change Hasura permissions and relationships; they are not
  health probes. Preserve exact amount serialization and restricted read access to
  internal lifecycle observations. Never expose admin secrets to browser clients.

## Validation and review

- For Python changes, generate bindings, run `python -m pip check`, and run
  `python -W error::DeprecationWarning -m pytest tests/ -m "not integration" --strict-markers`.
  Use [unit CI](.github/workflows/tests.yml) as the coverage-command reference.
- For storage, lifecycle, protocol or dependency changes, run relevant integration
  checks against disposable PostgreSQL/Hasura services. Follow
  [database CI](.github/workflows/database-compatibility.yml) for clean production/test
  builds, production package isolation, metadata setup, the full suite, SQL amount
  assertions and the metrics client. A local environment with old packages is not
  evidence that a fresh dependency install works.
- Integration settings `TEST_DATABASE_URL` and `TEST_HASURA_*` must never target
  retained application data. Tests create/drop databases or write Hasura fixtures.
  Give test projects unique resources; clean only the task's disposable resources.
- Validate documentation links and claims against configuration/routes/source.
  Do not launch a live network or run migrations merely to check documentation.
- Check `git diff --check` and review the exact staged file list before committing.
  Report passed, skipped, blocked and live-acceptance checks separately. Do not claim
  that health/readiness, green CI or a successful image build proves finalization
  progress, deployment acceptance or release provenance.
