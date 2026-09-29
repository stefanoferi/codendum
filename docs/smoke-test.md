# Smoke test

On the GB10 host, test vLLM directly:

```bash
scripts/smoke-test.sh
```

Then test through the proxy with a user key. Here the script also checks that
`/health`, `/metrics` and the other private endpoints are **not** reachable, and
that requests without a key are rejected:

```bash
read -rsp "API key: " CODENDUM_API_KEY && export CODENDUM_API_KEY
scripts/smoke-test.sh --proxy --base-url https://llm.lab.example:8443
```

Add `--cacert /path/to/ca.pem` for a certificate from a private CA.

The checks run in this order:

1. Health, and the model list containing `coder`.
2. A chat completion.
3. A streamed completion, which must arrive in chunks and end with `[DONE]`.
4. A tool call with `tool_choice: "auto"`. This is the path OpenCode uses and the
   one that exercises the `qwen3_coder` parser.
5. A tool call with `tool_choice: "required"`. This goes through structured
   outputs and does not test the parser.
6. A round trip in which a tool result is sent back and a text answer is
   expected.

Exit status is `0` when every check passes and `1` when a check fails.
`2` means inconclusive: for example, the model legitimately answered without
calling the tool under `"auto"`. An inconclusive result is not a success.
Repeat it, and use the OpenCode loop above to decide. If raw `<tool_call>`
markup appears in the message text, the parser is misconfigured and the check
fails.
