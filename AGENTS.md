# Invoxy bot scope

Read `../AGENTS.md` and `../docs/UPSTREAM_WORKFLOW.md` before editing or changing
Git state.

- Follow the existing FastAPI route -> service -> database/external-client flow.
- Put business rules in the shared service used by every caller, not in one route.
- Add a route module only for a cohesive API surface; register it with the
  smallest possible edit to the existing router.
- Reuse the pricing, payment, cart, subscription, and Remnawave services. Do not
  fork their logic into an `invoxy` copy.
- Traffic accounting by Remnawave squad belongs in a focused service with a thin
  route/admin integration. Store local facts in additive tables/migrations and
  keep remote synchronization idempotent.
- Money paths require focused tests for amount calculation, idempotency, disabled
  methods, and failure before persistence.
- Preserve the existing dirty `uv.lock` unless dependency metadata is explicitly
  in scope.

Verify focused tests with `uv run pytest <tests>` and changed Python files with
`uv run ruff check <files>`.
