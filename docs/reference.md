# Reference

## Components, versions and licenses

This repository is licensed under the Apache License 2.0: see [LICENSE](https://github.com/stefanoferi/codendum/blob/main/LICENSE)
and [NOTICE](https://github.com/stefanoferi/codendum/blob/main/NOTICE). That license covers only the files in this repository:
scripts, configuration examples and documentation. The repository does not
contain or redistribute container images, model weights or third-party
software. The operator downloads those separately, and they come under their
own licenses and terms, which the operator must review and comply with.

| Component | Pinned version | License | Where it runs |
| --- | --- | --- | --- |
| Codendum (this repository) | 0.1.0 (unreleased) | Apache-2.0 | GB10 host, workstations |
| vLLM container image | `vllm/vllm-openai:v0.30.0@sha256:8a69ffad015f138d7170c4ddc429e230a3bc1c1719f67e14324749df200a4b90` (index; the linux/arm64 manifest is `sha256:4864d466…`), CUDA 13.0.2 | vLLM: Apache-2.0; the image bundles third-party software (for example the CUDA runtime and PyTorch) under their own licenses | GB10 host |
| Model | `Qwen/Qwen3-Coder-30B-A3B-Instruct-FP8` at revision `dcaee4d4dfc5ee71ad501f01f530e5652438fde0` | Apache-2.0, according to the model card; check the model's license file | GB10 host |
| OpenCode | V2 (2.0.x); 1.x with the V1 example | MIT | Workstations |
| nginx container image | `nginx:1.30.5-alpine@sha256:0985e772fb9f729e6fa0980da05fca5d9c468e870eed43071545afa9d2e27d94` | nginx: BSD-2-Clause; the Alpine base includes packages under their own licenses | GB10 host |
| Docker Engine, NVIDIA Container Toolkit | Preinstalled with DGX OS | Apache-2.0 | GB10 host |
| `actions/checkout` | v7.0.1 @ `3d3c42e5aac5ba805825da76410c181273ba90b1` | MIT | CI only |
| Code of conduct | Contributor Covenant 3.0 | CC BY-SA 4.0 | Documentation |

## Versioning and compatibility

Codendum follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
The public interface is:

- script names and options;
- the `CODENDUM_*` variable names;
- the profile names;
- the layout of the configuration examples.

Changes are recorded in [CHANGELOG.md](https://github.com/stefanoferi/codendum/blob/main/CHANGELOG.md).

| Area | Status |
| --- | --- |
| Offline tests (CI) | Ubuntu 24.04 on x64 and ARM64: lint, script tests against a mock vLLM, proxy tests (nginx container and distribution package), documentation and secret checks |
| Target host | DGX OS 7 on NVIDIA GB10. Run on a Lenovo ThinkStation PGX (DGX OS 7.2.3) on 2026-09-29: startup, smoke tests, proxy, a real OpenCode session and 40-user simulations |
| vLLM | Built for v0.30.0. `qwen3_coder` requires ≥ 0.25 (earlier releases had a different parser implementation); `CODENDUM_MAX_NUM_QUEUED_REQS` requires ≥ 0.29 |
| OpenCode | V2 native configuration; V1 format provided separately |

## Sources

- NVIDIA, DGX Spark: <https://www.nvidia.com/en-us/products/workstations/dgx-spark/> and the hardware overview <https://docs.nvidia.com/dgx/dgx-spark/hardware.html>
- NVIDIA, vLLM on DGX Spark (playbook): <https://build.nvidia.com/spark/vllm/instructions>
- NVIDIA, container runtime for Docker on DGX Spark: <https://docs.nvidia.com/dgx/dgx-spark/nvidia-container-runtime-for-docker.html>
- NVIDIA, vLLM container release notes: <https://docs.nvidia.com/deeplearning/frameworks/vllm-release-notes/>
- vLLM on DGX Spark (vLLM blog): <https://vllm.ai/blog/2026-06-01-vllm-dgx-spark>
- Qwen, FP8 checkpoint: <https://huggingface.co/Qwen/Qwen3-Coder-30B-A3B-Instruct-FP8>
- vLLM, `serve` options: <https://docs.vllm.ai/en/latest/cli/serve/>
- vLLM, tool calling: <https://docs.vllm.ai/en/latest/features/tool_calling/>
- vLLM, metrics: <https://docs.vllm.ai/en/latest/usage/metrics/>
- vLLM, security: <https://docs.vllm.ai/en/latest/usage/security/>
- OpenCode V2, models and local servers: <https://opencode.ai/v2/docs/models>
- OpenCode V2, providers: <https://opencode.ai/v2/docs/providers>
- Open Source Guides, starting a project: <https://opensource.guide/starting-a-project/>
- GitHub, licensing a repository: <https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/licensing-a-repository>
- Apache License 2.0: <https://choosealicense.com/licenses/apache-2.0/>
- GitHub Actions, secure use: <https://docs.github.com/en/actions/reference/security/secure-use>
- GitHub, private vulnerability reporting: <https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/configure-vulnerability-reporting/configure-for-a-repository>

## Author

Codendum is created and maintained by [Stefano Noferi](https://noferi.it/)
(GitHub: [`stefanoferi`](https://github.com/stefanoferi)).
