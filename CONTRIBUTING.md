# Contributing to Codendum

Thank you for helping. Codendum is a small project: scripts, configuration
examples and documentation for running a shared coding-agent service on an
NVIDIA GB10. Contributions of every size are welcome, from typo fixes and
translations to measurements on real hardware.

By participating you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).
Security issues must **not** be reported in public issues: see
[SECURITY.md](SECURITY.md).

## Ways to contribute

- **Report a problem or propose an improvement** with an issue. Include the
  environment details the template asks for, and remove API keys, host names
  and user data from logs.
- **Share measurements** from a GB10: `bench.sh` summaries with the profile,
  image digest, model revision and DGX OS version. Measurements are especially
  valuable because the project cannot run GPU tests in CI.
- **Improve the documentation or a translation.** See
  [docs/TRANSLATING.md](docs/TRANSLATING.md).
- **Change scripts or configuration.** For anything larger than a small fix,
  open an issue first so that the approach can be agreed on.

## Development setup

The offline test suite needs Linux with bash ≥ 4.4, Python ≥ 3.8, curl,
OpenSSL, Git, ShellCheck, yamllint, nginx and Docker (for the proxy container
test). It needs no GPU, model or network access.

```bash
tests/run.sh
```

On macOS or Windows, run the suite in a Linux container or VM. For example:

```bash
docker run --rm -it -v "$PWD":/src -w /src ubuntu:24.04 bash -c \
  'apt-get update && apt-get install -y --no-install-recommends shellcheck nginx yamllint python3 curl openssl git ca-certificates procps iproute2 && git config --system --add safe.directory /src && useradd -m t && su t -c tests/run.sh'
```

Individual suites:

| Command | What it checks |
| --- | --- |
| `tests/lint.sh` | `bash -n`, ShellCheck, Python compilation, JSON and YAML syntax, SPDX headers, executable bits |
| `tests/test-scripts.sh` | Script behaviour against a mock vLLM server and a fake `docker` |
| `tests/test-proxy.sh docker` | The proxy container started by `start-proxy.sh`, in front of the mock |
| `tests/test-proxy.sh native` | The same configuration with the distribution's nginx, run unprivileged |
| `tests/check-docs.sh` | Required files, license text, EN ⇄ IT links, sections, commands, links, placeholders |
| `tests/check-secrets.sh` | Whitespace, `.gitignore` coverage, forbidden files, literal credentials |

## Guidelines

- **English** for code, comments, messages and documentation. Translations
  follow [docs/TRANSLATING.md](docs/TRANSLATING.md). A change to `README.md`
  should update `docs/it/README.md` in the same pull request when possible.
- **Shell:** bash ≥ 4.4 with `set -Eeuo pipefail`, ShellCheck-clean, long option
  names, and `--help` on every script. Scripts must be safe by default:
  - validate the input;
  - never overwrite or delete without an explicit flag;
  - never print secrets;
  - never accept a secret as a command-line argument.
- **Python:** standard library only, compatible with Python 3.8.
- **Headers:** new source files start with
  `SPDX-License-Identifier: Apache-2.0` and a copyright line.
- **Placeholders only:** use `llm.lab.example`, the documentation address
  ranges (`192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`) and obviously
  fake values. Never include real host names, keys, tokens or certificates, even
  in tests.
- **Honesty about hardware:** say what you tested and on which hardware. Do not
  present estimates as measurements, and do not add badges for checks that do
  not exist.
- **Pinned dependencies:** images by digest, models by revision, and GitHub
  Actions by full commit SHA with a version comment. Verify every SHA and digest
  yourself; never copy them from an unverified source.
- **Small, focused pull requests** with a clear description are reviewed
  faster.
- **AI-assisted contributions** are welcome. You are responsible for reviewing
  and testing them like any other change.

## Licensing of contributions

Codendum is licensed under the [Apache License 2.0](LICENSE). Unless you state
otherwise, any contribution you intentionally submit for inclusion is licensed
under the same terms, without additional conditions, as described in section 5
of the license. Only submit work you have the right to license this way. Do not
add third-party code, documentation or model files unless their license allows
it and they are clearly attributed.

## Review

The maintainer reviews pull requests on a best-effort basis. CI must pass. It
runs with a read-only token and no secrets. Workflows from first-time
contributors may need the maintainer's approval before they run.
