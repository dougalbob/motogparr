# Agent improvement plan

This is an agent-facing work plan derived from the repository review on
2026-11-06. It is not a promise that every item is still necessary when a slice
starts: first verify the relevant code and tests at the branch tip. The goal is
to turn improvements into small, independently reviewable changes, not to bundle
a redesign into one PR.

Status labels: **DONE** means completed in the documentation-refresh change that
introduced this plan; **NEXT** is the recommended first code slice; **PLANNED**
means useful follow-up work, not a commitment to implement it regardless of what
new evidence shows; **LATER** is deliberately deferred until prerequisites land.

## Order at a glance

| Order | Slice | Status | Main reason |
|---|---|---|---|
| 0 | Refresh agent-facing documentation and establish this plan | **DONE** | Stale instructions are a source of incorrect code changes |
| 1 | Make activity-event updates one locked, atomic transaction | **NEXT** | Events can be lost or state files corrupted by concurrent processes |
| 2 | Apply the same persistence contract to grabber, watcher and WebUI JSON state | **PLANNED** | These files are also shared or written directly |
| 3 | Replace free-form WebUI command strings with typed, guarded jobs | **PLANNED** | Prevent argument misuse, duplicate launches and unbounded output |
| 4 | Add deterministic tests at Prowlarr/SABnzbd/Plex boundaries | **PLANNED** | Exercise integration behaviour without live home services |
| 5 | Add browser-write defense-in-depth (CSRF/Origin and CSP) | **PLANNED** | Reduce reliance on the outer Access/browser assumptions |
| 6 | Move the WebUI to a production WSGI server | **LATER** | Do this only after job/status state is safe across workers |

## Slice 0 — documentation baseline (**DONE**)

Completed with this plan:

- Updated `AGENTS.md` from the stale v2.6.0 / 377-test snapshot to the
  repository's v2.7.4 release and the 384-test result last verified on
  2026-11-06. Keep the date beside the test count; it is a snapshot, not a
  permanent invariant.
- Corrected `docs/ARCHITECTURE.md`'s stale release/test claims, route-security
  descriptions, test descriptions and Python executable description.
- Added this roadmap and linked it from `AGENTS.md` and `docs/ARCHITECTURE.md`.
- Replaced the README's obsolete v2.3.0 release example with a version pattern.

Follow-up documentation rule: when code changes alter a route, process boundary,
state format, auth behaviour, environment variable or validation requirement,
update the relevant architecture/agent documentation in that same PR. Avoid
copying a changing test count into multiple documents; this plan has no count by
design.

## Slice 1 — activity event transaction (**NEXT**)

**Problem evidence:** `activity_emitter.py` reads the JSON before acquiring its
lock. The lock is taken on the temporary output file only while serialising,
then released before that file is renamed over the destination. That does not
protect the read-modify-write transaction. Multiple emitters can read the same
snapshot and overwrite each other's changes; a shared temporary filename also
makes concurrent writes fragile.

**Scope:** Correct the activity store only. Use a stable sidecar lock file (not
the data file or a temporary file) and hold an exclusive lock across load,
mutation and commit. Write the new JSON to a unique temporary file in the same
directory, flush it, then atomically replace the destination. Keep the existing
JSON shape and public emitter API unchanged. Make failed writes visible through
the existing logger rather than silently dropping the event.

A small reusable storage helper is acceptable if it has a narrow API that makes
it impossible to accidentally lock only the write phase. Do not migrate every
state file in this slice: first prove the transaction semantics on the activity
store.

**Tests / acceptance:**

- Existing `tests/test_activity_feed.py` remains green.
- Add tests showing concurrent independent updates are both retained, the lock
  is released on exceptions, and a failed/interrupted replacement leaves the
  previous valid file readable.
- Run `python -m pytest tests -q` and the normal CI workflows.
- No event schema, ordering, retention-limit or Activity UI behaviour changes.

## Slice 2 — other JSON state files (**PLANNED; split by store if needed**)

After Slice 1, audit each state file's actual readers and writers. Candidate
stores identified in the review are:

- `motogp_grabber.py`: `grabber_state.json` is written directly; scheduled and
  manually requested grabbers can overlap and overwrite state.
- `motogp_watcher.py`: `watcher_state.json` is written directly and read by the
  WebUI as well as the watcher.
- `motogp_webui.py`: `webui_settings.json` is written directly; concurrent
  settings requests can race.

Migrate one store per PR, or group only stores that share the same transaction
model. The operation must reload current state while holding the sidecar lock;
atomic replacement alone prevents partial JSON but does not prevent lost updates
from stale in-memory snapshots. Preserve existing file names and schemas. Do not
change `settings.yml` ownership/precedence or the one-time migration behaviour
without a separate migration plan and tests.

**Tests / acceptance:** exercise simultaneous writers, corrupt-file recovery,
permission errors, and old-format compatibility where applicable. Ensure a
reader never sees partially written JSON and that the WebUI's settings-store
ownership remains as documented in `AGENTS.md`.

## Slice 3 — typed WebUI jobs (**PLANNED**)

**Problem evidence:** `/run` accepts a command string and routes it to a
subprocess after `str.split()` (`motogp_webui.py`). `shell=True` is not used, so
do not describe this as shell injection; the remaining concerns are unexpected
CLI arguments, weak request validation, duplicate launches racing through the
`running` check, and output retained without a bound.

**Scope:** Replace the public string contract with a small set of named actions
and typed fields (for example action, season, round, session selection and
validated overlay options). Map each action to an explicit argv list. Validate
ranges and supported options before starting any work. Reserve the singleton
job synchronously under a lock before creating its worker. Store job status in a
bounded output buffer, report exit status, and define timeout/cancellation
behaviour. Keep the existing user workflows working; update `static/js/app.js`
and templates in the same PR as the endpoint contract.

Do not combine this with switching WSGI servers. The current process-local job
state is one of the reasons a multi-worker server is not yet a safe drop-in.

**Tests / acceptance:** reject malformed payloads and unsupported actions;
verify exactly one job can start under concurrent requests; assert the generated
argv for every supported action; test bounded output, nonzero exit and timeout;
retain auth coverage for every write route.

## Slice 4 — integration-boundary tests (**PLANNED**)

Add local deterministic fakes/fixtures for external services rather than tests
that require a real Prowlarr, SABnzbd or Plex server. Start with contract tests
for search/download submission and Plex metadata calls, then add a focused
pipeline test for the parts that can run without Docker or actual media services.

Cover success, authentication failure, malformed responses, timeouts, retry
behaviour, duplicate/idempotent processing, and service unavailability. Tests
must never contain live API keys, LAN addresses or dependencies on a user's
library. Reuse the project's existing `pytest tests` invocation and preserve the
real-image smoke test as a distinct CI layer.

Keep this slice focused: do not attempt to simulate the entire home network or
rewrite all service access behind a new framework just to get tests in place.

## Slice 5 — browser-write hardening (**PLANNED**)

Review every mutating route and its browser call site. Add a consistent Origin
(or Referer fallback) policy and/or CSRF token for browser-originated writes;
maintain JSON clients and the Cloudflare Access verification model. Add a
Content Security Policy in report-only mode first if existing inline scripts or
styles prevent a strict policy, then tighten it with targeted template changes.

Test allowed same-origin writes, rejected cross-origin writes, token handling,
and the public health/static endpoints. Do not assume that adding CSRF controls
replaces server-side auth or that `AUTH_MODE=none` is safe outside a trusted
network.

## Slice 6 — production WSGI server (**LATER; prerequisite: Slice 3 and state audit**)

Flask's built-in server is currently started directly in `motogp_webui.py`. A
production WSGI server is a reasonable operational improvement, but do not add
multiple workers until process-local state (especially job status) is either
externalised or the worker model is proven safe. First document the required
SSE behaviour, signal/shutdown handling, container healthcheck and restart
semantics. Then make the smallest deployment change and exercise it in the real
container smoke workflow.

## Rules for every slice

1. Re-read the target code and relevant tests; this plan records review findings,
   not immutable facts.
2. Keep persistent formats, calendar/session identifiers and library paths
   unchanged unless the slice explicitly includes a tested migration.
3. Add a regression test for the behaviour being changed; do not weaken an
   existing security or correctness assertion to make a change pass.
4. Run `.venv/bin/python -m pytest tests -q` (never bare root-level `pytest`;
   see `SANDBOX.md`). If dependencies are missing, install
   `requirements-dev.txt` in the repository's ignored `.venv` first.
5. For Dockerfile, entrypoint, dependency or runtime changes, rely on and inspect
   `.github/workflows/container-smoke.yml`; unit tests do not prove the built
   image works.
6. Update `AGENTS.md` and `docs/ARCHITECTURE.md` when their claims change, and
   mark the relevant slice complete only after tests/CI verify it.
