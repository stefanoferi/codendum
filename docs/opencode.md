# OpenCode on the workstations

Install OpenCode V2 by following the
[official instructions](https://opencode.ai/v2/docs/). On managed machines,
prefer the organization's software distribution.

## Automatic configuration

`configure-opencode.sh` configures OpenCode on a workstation from the server
itself:

- it asks the server, through the proxy, which model it serves and its maximum
  context length;
- it checks a test completion with the user's key;
- it adds the Codendum provider to the OpenCode configuration, keeping every
  other setting and backing up the previous file.

The key comes from `CODENDUM_API_KEY`, or is asked for without echoing, and is
never written to a file. On Linux and macOS:

```bash
scripts/configure-opencode.sh --base-url https://llm.lab.example:8443
```

The command needs only Python 3, so on Windows you can run
`py scripts\lib\codendum.py opencode-config --base-url https://llm.lab.example:8443`.

| Option | Use |
| --- | --- |
| `--cacert ca.pem` | Certificate from a private CA |
| `--format v1` | OpenCode 1.x configuration format |
| `--output FILE` | Update a project `opencode.json` instead of the user's global file |
| `--dry-run` | Print the resulting configuration without writing it |

Run it again whenever the server switches profile. The context limit then
follows the server, without manual edits on each workstation.

## Manual configuration

To configure the provider by hand:

1. Copy [`config/opencode.example.json`](https://github.com/stefanoferi/codendum/blob/main/config/opencode.example.json) to
   `~/.config/opencode/opencode.json`, or to `opencode.json` in a project.
2. Replace `https://llm.lab.example:8443/v1` with the service URL, including
   the proxy port.
3. Provide the user's key in the `CODENDUM_API_KEY` environment variable. The
   configuration references it as `{env:CODENDUM_API_KEY}`.

```json
"codendum": {
  "name": "Codendum (local)",
  "package": "@opencode/ai/providers/openai-compatible",
  "settings": { "baseURL": "https://llm.lab.example:8443/v1", "apiKey": "{env:CODENDUM_API_KEY}" },
  "models": {
    "coder": {
      "modelID": "coder",
      "capabilities": { "tools": true, "input": ["text"], "output": ["text"] },
      "limit": { "context": 65536, "output": 8192 }
    }
  }
}
```

Why each setting matters:

- **`capabilities.tools: true` is required.** OpenCode's automatic discovery of
  vLLM models cannot see server-side tool support, so discovered models start
  with tools disabled. Declare the model explicitly, and do not name the
  provider `vllm`: that id belongs to the built-in discovery plugin.
- **`limit.context` must match the server's `max-model-len`.** OpenCode uses it
  to manage the size of a conversation, for example to decide when to compact
  it. If it is larger than the server's limit, long sessions fail with HTTP 400.
  The automatic configuration keeps the two aligned; by hand, change it to
  `131072` on every workstation when the server switches to `deep-128k`.
- **OpenCode V2 runs a background service,** which sees an environment variable
  only if the variable was set when the service started. Start OpenCode from a
  shell that exports `CODENDUM_API_KEY`, or follow the V2
  [network documentation](https://opencode.ai/v2/docs/network) to store it in
  the service configuration.
- **Certificates from an internal CA** need
  `NODE_EXTRA_CA_CERTS=/path/to/ca.pem` on the workstation.
- **OpenCode 1.x** uses the older format: see
  [`config/opencode.v1.example.json`](https://github.com/stefanoferi/codendum/blob/main/config/opencode.v1.example.json).
- **Session sharing is disabled** in both examples (`"share": "disabled"`).
  Shared OpenCode sessions are public links hosted outside the organization.
- **Editor warnings on V2 keys are expected.** Editors that validate against
  `https://opencode.ai/config.json` may flag `providers`, `package`, `settings`
  and `capabilities`, because the published schema still describes the V1
  format. OpenCode V2 accepts them.

A chat reply alone does not prove that the agent works. Verify a real
read → edit → test loop in a scratch directory on a workstation. The example
uses Python because it is available on most systems; the same check works in
any language your users work with:

```bash
mkdir -p /tmp/codendum-check && cd /tmp/codendum-check
cat > calc.py <<'EOF'
def add(a, b):
    return a - b


if __name__ == "__main__":
    assert add(2, 3) == 5, "add(2, 3) should be 5"
    print("OK")
EOF
opencode run --auto --model codendum/coder "Run 'python3 calc.py', fix the bug in calc.py, then run it again until it prints OK."
python3 calc.py
```

The check passes when OpenCode has read the file, edited it with its tools and
run the program, and the final `python3 calc.py` prints `OK`. `--auto`
auto-approves tool permissions; use it only in a scratch directory.
