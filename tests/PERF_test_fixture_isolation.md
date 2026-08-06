# Test fixture isolation and performance contract

The test fixtures use one immutable SQLite schema template per pytest worker.
The template is built with the production `init_db` CREATE_TABLES plus migration
chain, then copied with SQLite's backup API into a new `:memory:` connection for
each `db`, `anydb` SQLite parameter, and default SQLite `client` lifespan. A copy
failure raises and never falls back to a shared or partially initialized database.

Resource ownership is intentionally worker- and test-local:

| Resource | No xdist | xdist | Isolation check |
| --- | --- | --- | --- |
| SQLite schema template | pytest session temp root | pytest worker temp root | `test_worker_template_path_is_under_pytest_temp_root` |
| SQLite test data | fresh in-memory connection per test | fresh in-memory connection per test | `test_sqlite_template_copy_is_isolated_and_schema_complete` |
| PostgreSQL database | configured base database | `<base>_gw0`, `<base>_gw1`, ... | `test_postgres_worker_names_are_distinct_and_unsuffixed_without_xdist` |
| TestClient port | no real TCP listener; in-process ASGI transport | same per worker | no port is allocated by the fixture |
| FastAPI app state | shared route/app object, per-test lifespan | worker-local process | lifespan remains per TestClient because it owns a DB, resolver, log handler, and background tasks |
| graph-search resolver | reset before and after each test | worker-local process | existing autouse reset fixture |

The application module is imported once per worker, but the lifespan is not
reused. Reusing it would share database-bound state and background tasks across
tests; the safe optimization is only to clone the schema when the lifespan opens
its SQLite database.

## Controlled benchmark

Using the same ten `tests/test_core.py` HTTP tests with `-p no:cacheprovider`
and no xdist:

| Run | pytest-reported time | measured wall time |
| --- | ---: | ---: |
| baseline | 26.64s | 31.66s |
| template clone | 10.49s | 12.48s |

In a direct three-iteration measurement, fresh SQLite `init_db(":memory:")`
averaged 0.82s per connection, while backup cloning averaged 0.027s after the
one-time worker template build. The benchmark is intentionally focused rather
than a repeated full-suite comparison; the full repository command remains the
final compatibility gate.
