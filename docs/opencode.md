# OpenCode on the workstations

Install OpenCode V2 by following the
[official instructions](https://opencode.ai/v2/docs/). On managed machines,
prefer the organization's software distribution. Then configure the provider:

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
  it. If it is larger than the server's limit, long sessions fail with HTTP 400. For the `deep-128k` profile, change
  it to `131072` on every workstation at the same time as the server.
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
read → edit → test loop in a scratch directory on a workstation with a JDK:

```bash
mkdir -p /tmp/codendum-check && cd /tmp/codendum-check
cat > Calc.java <<'EOF'
public class Calc {
    static int add(int a, int b) { return a - b; }
    public static void main(String[] args) {
        if (add(2, 3) != 5) throw new AssertionError("add(2, 3) should be 5");
        System.out.println("OK");
    }
}
EOF
opencode run --auto --model codendum/coder "Run 'java Calc.java', fix the bug in Calc.java, then run it again until it prints OK."
java Calc.java
```

The check passes when OpenCode has read the file, edited it with its tools and
run the program, and the final `java Calc.java` prints `OK`. `--auto`
auto-approves tool permissions; use it only in a scratch directory.
