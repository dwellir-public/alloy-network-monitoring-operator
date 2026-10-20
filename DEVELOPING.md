# Developing alloy-nm

Run commands from the checkout root. See the
[architecture guide](docs/charm-architecture.md) for runtime boundaries and the
[build/test guide](docs/build-test-deploy.md) for artifacts and live acceptance.

## Dependency management (uv)

The charm uses `uv` for dependency management. Update Python dependencies in
`pyproject.toml`, then refresh the lockfile:

```bash
uv lock
```

Install the locked development environment:

```bash
uv sync --locked --group dev
```

## Running tests

Run the local checks:

```bash
uv run tox -e lint,static,unit,provenance
```

To focus on charm unit tests without repeating every check:

```bash
uv run pytest tests/unit/test_charm.py
```

Native Alloy checks use the checksum-pinned executable:

```bash
python3 tools/fetch_alloy.py --output build/alloy-linux-amd64
ALLOY_BINARY=build/alloy-linux-amd64 uv run tox -e binary
```

Jubilant integration tests require an explicit controller-qualified nonproduction
`TEST_MODEL`, `TEST_MODEL_IS_NONPRODUCTION=yes`, matching `CHARM_PATH`/`TEST_BASE`
and additional provider/query inputs for backend checks. Rebuild after runtime
changes. The suite skips absent required inputs; it never creates or destroys
the selected model. It creates and removes only owned test applications,
synthetic secrets and newly allocated empty machines. Follow the
[live-test instructions](docs/build-test-deploy.md#explicit-live-artifact-test).

## Dependency ownership and BOMs

`pyproject.toml` and `uv.lock` define Python dependencies. `dependencies/upstreams.json`
records upstream ownership; `dependencies/vendored.json` pins the exact source,
commit, hash, license and LIB metadata of each shipped library. Locally modified
libraries identify their downstream source and upstream owner explicitly. An
upstream catalog entry alone does not install a dependency.

Review dependency updates together with their locks, source pins and compatibility
tests. Do not replace a locally patched library without reviewing its documented
changes. Run these checks from this repository:

```bash
uv lock --check
uv run tox -e provenance
python3 tools/dependency_bom.py --verify-upstream
python3 tools/dependency_bom.py --output build/development.cdx.json
# Set CHARM_PATH and CHARM_BASE to the artifact and base actually built.
python3 tools/dependency_bom.py --artifact "$CHARM_PATH" --base "$CHARM_BASE" \
  --arch amd64 --output build/runtime.cdx.json
```

The development CycloneDX BOM describes the locked development/test dependency
graph. The runtime BOM checks installed distribution versions and vendored bytes
against the built archive and records its checksum, source revision and input
hashes. These BOMs do not cover OS packages, downloaded workload binaries or
transitive build-tool environments. CI verifies provenance and keeps generated
BOMs as artifacts rather than source files.

Rule compression uses Canonical's public `cosl` API. Rule acceptance, retention
and size policy belong to this charm; no shared Dwellir transport package or
cross-repository source synchronization is required.

## Documentation and local records

Maintain the operator, architecture and build/test guides alongside behavior
changes. Keep public placeholder examples under `examples/`. The original
`PLAN.md` remains a design reference; current behavior is described by the guides.

Historical audits, plans, investigations and captured execution evidence are
kept in the ignored `.local-docs/` archive for this workspace. New runtime test
observations go under `build/`. These local records are not part of a fresh clone
or the charm artifact. Do not commit credentials or private operator inputs.
