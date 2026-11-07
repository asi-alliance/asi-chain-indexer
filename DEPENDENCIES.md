# Dependency maintenance

The supported runtime and CI baseline is CPython 3.13.18 on Linux.
`requirements.txt` contains direct runtime pins; `requirements-dev.txt` adds the
gRPC generator and test/development tools. The development resolution is constrained
by the runtime lock so both environments use the same runtime versions.

Install with `python -m pip install --require-hashes -r requirements.lock` for
production or `-r requirements-dev.lock` for development. `requirements-test.txt`
is a compatibility entry point for the development lock.

Regenerate both locks inside `python:3.13.18-slim-bookworm`, from the repository root:

```sh
python -m pip install pip-tools==7.6.2
pip-compile --generate-hashes --allow-unsafe --strip-extras -o requirements.lock requirements.txt
pip-compile --generate-hashes --allow-unsafe --strip-extras -o requirements-dev.lock requirements-dev.txt
```

Review the resolved versions and hashes. Regenerate both locks after editing pins.
These locks are resolved for Python 3.13/Linux; regenerate and validate for a different
platform. They pin Python packages, not Debian repositories or the container base digest.

`docker build -t asi-indexer:local .` builds the production target without test
packages. `docker build --target test -t asi-indexer-tests:local .` includes the
locked tools for integration tests. Both targets use freshly generated stubs;
local virtual environments and stubs are excluded from the build context.
