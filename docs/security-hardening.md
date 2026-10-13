# Sandbox security hardening (post-v0.1.0 proposal)

This document describes the **proposed** hardening in this change set. The published
v0.1.0 tag is unchanged. Do not infer production-grade isolation from passing unit tests.

## Trust boundaries

1. `DockerSandbox` runs repo tests/probes in a container with no default network,
   read-only worktree/root filesystem, dropped Linux capabilities and resource limits.
2. The single writable `out` mount stays owned by the host user and is accessed using
   that same **non-root** UID/GID in the container. No `chmod 0777` is performed.
3. `EnvironmentProvisioner` passes `--no-build` to `uv pip compile` and `uv pip install`
   to disallow PEP 517 source builds on the host. Repositories depending on packages with
   no compatible wheels will fail rather than executing a build hook.
4. Docker becomes the default sandbox. Explicit `--sandbox local` or `--sandbox auto`
   selects weaker isolation/fallback. Repository-provided configuration remains a trust
   input; force `--sandbox docker` for unknown repos.

## Unresolved risks

- The host still performs Python interpreter acquisition, wheel download and wheel
  installation. This is not a full supply-chain isolation boundary. Use a disposable VM
  and a trusted package index for truly untrusted dependencies.
- Docker bind mounts and resource limits depend on the local Docker daemon/platform;
  macOS and rootless Docker require independent validation.
- A repository's configuration can request `network = true` and a weaker sandbox;
  operators should force Docker and review config before accepting untrusted input.
- Runtime/permissions tests should be reviewed on Linux with Docker; no claim is made
  for an environment where Docker is unavailable.

## Reproducible checks

```bash
pip install -e '.[dev]'
ruff check src tests
pytest tests/unit -q
migrationguard sandbox build-image
pytest tests/integration/test_sandbox.py -q
pytest tests/integration -q
```

No new Claude-powered benchmark numbers are asserted by these changes. The published
benchmark remains the original v0.1.0 experiment unless rerun and independently checked.
