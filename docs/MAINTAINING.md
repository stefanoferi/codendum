# Maintainer guide

Tasks that need repository admin rights or access to the GB10 host. Contributors
do not need this file.

## Principles

- Git history is public and permanent. Run `tests/run.sh`, and in particular
  `tests/check-secrets.sh`, **before** every commit. Never commit a secret with
  the intention of removing it later.
- Release tags are created and pushed only by the maintainer, deliberately.
  There is no publishing workflow today: a tag does not build or upload
  anything. Keep it that way unless a release pipeline is reviewed and added on
  purpose.
- Do not claim hardware validation that has not happened. Record the hardware,
  DGX OS version, image digest and model revision with any result.

## Repository settings

Recommended settings for the public repository. All of them need admin rights.

```bash
# Private vulnerability reporting (the channel named in SECURITY.md)
gh api -X PUT repos/stefanoferi/codendum/private-vulnerability-reporting
gh api repos/stefanoferi/codendum/private-vulnerability-reporting

# Read-only default token for workflows
gh api -X PUT repos/stefanoferi/codendum/actions/permissions/workflow \
  -f default_workflow_permissions=read -F can_approve_pull_request_reviews=false

# Require full-length SHA pinning for actions
gh api -X PUT repos/stefanoferi/codendum/actions/permissions \
  -F enabled=true -f allowed_actions=all -F sha_pinning_required=true

# Dependabot alerts and security updates
gh api -X PUT repos/stefanoferi/codendum/vulnerability-alerts
gh api -X PUT repos/stefanoferi/codendum/automated-security-fixes
```

In the web interface, also:

- **Settings → Advanced Security:** confirm that private vulnerability
  reporting is enabled. Enable secret scanning and push protection.
- **Settings → Actions → General:** under the approval setting for fork pull
  request workflows, choose "Require approval for first-time contributors" or
  a stricter option.
- **Settings → Rules:** protect `main`. Require a pull request and the `CI`
  checks, and block force pushes.
- Watch the repository with "All activity" or "Security alerts". GitHub emails
  private vulnerability reports only to maintainers who watch the repository.

## Releases

Codendum uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html). The
public interface is listed in the README section "Versioning and
compatibility". To release:

1. Make sure CI is green on `main`.
2. In `CHANGELOG.md`, rename `[Unreleased]` to `[X.Y.Z] - YYYY-MM-DD` and add a
   new empty `[Unreleased]` section. Update the version shown in the README,
   the Italian translation and `scripts/lib/codendum.py`.
3. If hardware validation was done for this release, record it in the
   changelog: hardware, DGX OS version, image digest, model revision, and a
   summary of `bench.sh` results.
4. Commit, push `main`, then create an annotated tag `vX.Y.Z` and a GitHub
   release from the changelog entry. Tagging is a deliberate, manual step.

## Updating pinned components

### vLLM image

1. Read the release notes of the candidate version, especially anything about
   ARM64/SBSA, DGX Spark, GB10 or SM 12.x, FP8, `qwen3_coder`/`qwen3_xml` and
   changed CLI flags.
2. Resolve and verify the digest, and check that `linux/arm64` is in the index:

   ```bash
   docker buildx imagetools inspect vllm/vllm-openai:vX.Y.Z
   ```

3. Check the CUDA version of the image against the DGX OS driver:

   ```bash
   docker buildx imagetools inspect --format '{{json .Image}}' vllm/vllm-openai:vX.Y.Z
   ```

4. Update `CODENDUM_VLLM_IMAGE` in `.env.example`, the README tables (English
   and translations) and the changelog.
5. On the GB10 host, run preflight, start with `--replace --wait`, and run the
   smoke test, the OpenCode loop and `bench.sh` for each profile.

### nginx image

Use the current stable release, preferably the Alpine variant. Resolve its
digest and check `linux/arm64` in the index, then update
`CODENDUM_PROXY_IMAGE` in `.env.example`, the README tables and the changelog.
CI runs the proxy tests against the pinned image.

```bash
docker buildx imagetools inspect nginx:X.Y.Z-alpine
```

On the host, apply the new image with `scripts/start-proxy.sh --replace`. The
script tests the configuration with the new image before it replaces the
running proxy.

### Model revision

Get the latest commit of the checkpoint and review the changes on the model
page before pinning it:

```bash
curl -s https://huggingface.co/api/models/Qwen/Qwen3-Coder-30B-A3B-Instruct-FP8 | python3 -c 'import json,sys; print(json.load(sys.stdin)["sha"])'
```

### GitHub Actions

Dependabot opens weekly pull requests that update the pinned SHA and the version
comment. Before merging, confirm that the SHA belongs to the tag in the
upstream repository:

```bash
git ls-remote --tags https://github.com/actions/checkout | grep -E 'refs/tags/vX.Y.Z(\^\{\})?$'
```

## Hardware validation checklist

Run this on a GB10 before calling a release "validated on hardware":

- [ ] `scripts/preflight.sh --gpu-test` passes.
- [ ] `scripts/start-vllm.sh --wait` succeeds for each profile. Record the
      `KV cache size` and `Maximum concurrency` log lines.
- [ ] `scripts/smoke-test.sh` and `scripts/smoke-test.sh --proxy` exit 0.
- [ ] The OpenCode read → edit → test loop from the README succeeds from a
      workstation.
- [ ] `scripts/bench.sh` is completed for each profile, and `summary.csv` is
      kept with the environment details.
- [ ] `scripts/metrics.sh` during the benchmark shows no host memory
      exhaustion. Note the preemptions.
