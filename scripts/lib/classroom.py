# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Stefano Noferi
"""Classroom simulation: many users running OpenCode-like agent sessions.

Each virtual user works on a small Java client-server project held in memory.
The real model receives an agent system prompt, OpenCode-style tool
definitions and a coding exercise. It calls tools (read, write, edit, bash,
grep, ...), which this module executes against the in-memory project with
simulated build and test times. Users pause between requests as a person
reading and testing would, and follow-up requests make the context grow as in
a real session.

The model's output is real; the tools, the file system and the build are
simulated. With --prompt-file the system prompt and tool definitions can be
replaced by ones captured from a real OpenCode session.
"""

from __future__ import annotations

import csv
import fnmatch
import json
import os
import random
import re
import sys
import threading
import time
import urllib.error
from typing import Any, Dict, List, Optional, Tuple

import codendum as core

ROOT = "/home/student/chat-lab"

PROJECT_FILES: Dict[str, str] = {
    "pom.xml": """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>edu.lab</groupId>
  <artifactId>chat-lab</artifactId>
  <version>0.1.0</version>
  <properties>
    <maven.compiler.release>21</maven.compiler.release>
    <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>
  </properties>
  <dependencies>
    <dependency>
      <groupId>org.junit.jupiter</groupId>
      <artifactId>junit-jupiter</artifactId>
      <version>5.11.0</version>
      <scope>test</scope>
    </dependency>
  </dependencies>
</project>
""",
    "README.md": """# chat-lab

A minimal multi-user chat used in the networking lab.

- `ChatServer` accepts TCP clients on port 5000 and starts a `ClientHandler` per client.
- `ChatClient` connects, reads lines from standard input and prints what the server sends.
- `Message` is the wire format: `SENDER|TIMESTAMP|TEXT`, one message per line.

Build and test with `mvn -q test`.
""",
    "src/main/java/edu/lab/chat/Message.java": """package edu.lab.chat;

import java.time.Instant;
import java.util.Objects;

/** A chat message in the wire format SENDER|TIMESTAMP|TEXT. */
public final class Message {
    private final String sender;
    private final Instant timestamp;
    private final String text;

    public Message(String sender, Instant timestamp, String text) {
        this.sender = Objects.requireNonNull(sender);
        this.timestamp = Objects.requireNonNull(timestamp);
        this.text = Objects.requireNonNull(text);
    }

    public static Message parse(String line) {
        String[] parts = line.split("\\\\|", 3);
        return new Message(parts[0], Instant.parse(parts[1]), parts[2]);
    }

    public String format() {
        return sender + "|" + timestamp + "|" + text;
    }

    public String sender() { return sender; }
    public Instant timestamp() { return timestamp; }
    public String text() { return text; }
}
""",
    "src/main/java/edu/lab/chat/ChatServer.java": """package edu.lab.chat;

import java.io.IOException;
import java.net.ServerSocket;
import java.net.Socket;
import java.util.ArrayList;
import java.util.List;

/** Accepts clients and keeps track of their handlers. */
public class ChatServer {
    private final int port;
    private final List<ClientHandler> clients = new ArrayList<>();

    public ChatServer(int port) {
        this.port = port;
    }

    public void start() throws IOException {
        try (ServerSocket server = new ServerSocket(port)) {
            System.out.println("Chat server listening on " + port);
            while (true) {
                Socket socket = server.accept();
                ClientHandler handler = new ClientHandler(socket, this);
                clients.add(handler);
                new Thread(handler).start();
            }
        }
    }

    /** Sends a message to every connected client. Not implemented yet. */
    public void broadcast(Message message, ClientHandler from) {
        // TODO(lab): deliver the message to the other clients
    }

    public void remove(ClientHandler handler) {
        clients.remove(handler);
    }

    public static void main(String[] args) throws IOException {
        new ChatServer(5000).start();
    }
}
""",
    "src/main/java/edu/lab/chat/ClientHandler.java": """package edu.lab.chat;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.io.PrintWriter;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.time.Instant;

/** Reads lines from one client and forwards them to the server. */
public class ClientHandler implements Runnable {
    private final Socket socket;
    private final ChatServer server;
    private PrintWriter out;
    private String nickname = "anonymous";

    public ClientHandler(Socket socket, ChatServer server) {
        this.socket = socket;
        this.server = server;
    }

    @Override
    public void run() {
        try (BufferedReader in = new BufferedReader(
                new InputStreamReader(socket.getInputStream(), StandardCharsets.UTF_8))) {
            out = new PrintWriter(socket.getOutputStream(), true, StandardCharsets.UTF_8);
            String line;
            while ((line = in.readLine()) != null) {
                server.broadcast(new Message(nickname, Instant.now(), line), this);
            }
        } catch (IOException e) {
            System.err.println("Client error: " + e.getMessage());
        } finally {
            server.remove(this);
        }
    }

    public void send(Message message) {
        out.println(message.format());
    }

    public String nickname() {
        return nickname;
    }
}
""",
    "src/main/java/edu/lab/chat/ChatClient.java": """package edu.lab.chat;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.io.PrintWriter;
import java.net.Socket;
import java.nio.charset.StandardCharsets;

/** Console client: sends typed lines, prints received messages. */
public class ChatClient {
    public static void main(String[] args) throws IOException {
        String host = args.length > 0 ? args[0] : "localhost";
        try (Socket socket = new Socket(host, 5000);
             BufferedReader console = new BufferedReader(new InputStreamReader(System.in));
             BufferedReader in = new BufferedReader(
                     new InputStreamReader(socket.getInputStream(), StandardCharsets.UTF_8));
             PrintWriter out = new PrintWriter(socket.getOutputStream(), true, StandardCharsets.UTF_8)) {
            String line;
            while ((line = console.readLine()) != null) {
                out.println(line);
                System.out.println(in.readLine());
            }
        }
    }
}
""",
    "src/test/java/edu/lab/chat/MessageTest.java": """package edu.lab.chat;

import static org.junit.jupiter.api.Assertions.assertEquals;

import java.time.Instant;
import org.junit.jupiter.api.Test;

class MessageTest {
    @Test
    void roundTrip() {
        Message m = new Message("ada", Instant.parse("2026-01-01T10:00:00Z"), "hello | world");
        Message parsed = Message.parse(m.format());
        assertEquals("ada", parsed.sender());
        assertEquals("hello | world", parsed.text());
    }
}
""",
}

TASKS = [
    "The chat server does not deliver messages yet. Implement broadcast in ChatServer so that every other "
    "connected client receives each message, make the client list thread-safe, then run the tests.",
    "Add a /nick NAME command: a client can change its nickname. Validate the name (3-16 letters or digits) "
    "and add unit tests for the validation.",
    "Add a /list command that returns the nicknames of the connected clients to the requesting client only. "
    "Add a test.",
    "Message.parse fails on malformed lines. Make it throw a clear IllegalArgumentException for invalid input "
    "and add tests for missing fields and bad timestamps.",
    "Keep a chat history: the server appends every message to history.log and sends the last 20 lines to each "
    "new client. Keep file access simple and safe.",
    "ChatClient blocks while waiting for server messages. Read server messages on a separate thread so that "
    "incoming messages are printed while the user types.",
    "Add a graceful shutdown to ChatServer: on Ctrl-C close the server socket and all client connections.",
    "Write JUnit tests for ClientHandler without real sockets, for example with a fake socket or with "
    "in-memory streams.",
]

FOLLOW_UPS = [
    "Run the tests and fix any failure.",
    "Review your change for thread-safety problems and fix them.",
    "Add one more unit test for an edge case you have not covered.",
    "Add Javadoc to the public methods you changed.",
    "Explain in three bullet points what you changed and why.",
    "The teacher asks to keep methods short. Refactor the longest method you wrote.",
]

SYSTEM_PROMPT = """You are a coding agent that helps a student with a software project in their terminal.

# How to work
- Use the available tools to inspect the project before changing it. Read a file before editing it.
- Prefer small, focused edits with the edit tool; use write only for new files.
- After changing code, run the build and the tests with the bash tool and fix failures.
- Follow the existing style of the project. Do not add dependencies unless asked.
- Keep answers short: explain what you changed in a few lines. Do not repeat whole files in the answer.
- Never invent file contents: if a file is needed, read it.
- If a request is ambiguous, make a reasonable assumption and state it.

# Tool usage
- Paths are absolute and start with the project root.
- Batch independent reads when possible.
- Long outputs are truncated; use grep or read with offset and limit for large files.

# Environment
Working directory: {root}
Platform: linux
Build tool: Maven (mvn), Java 21
Is the directory a git repository: yes

# Project files
{tree}
"""

TOOLS: List[Dict[str, Any]] = [
    {"type": "function", "function": {
        "name": "read",
        "description": "Read a file from the project. Returns the content with line numbers. Use offset and "
                       "limit to read part of a large file. Always read a file before editing it.",
        "parameters": {"type": "object", "properties": {
            "filePath": {"type": "string", "description": "Absolute path of the file to read"},
            "offset": {"type": "integer", "description": "Line number to start from (0-based)"},
            "limit": {"type": "integer", "description": "Number of lines to read"}},
            "required": ["filePath"]}}},
    {"type": "function", "function": {
        "name": "write",
        "description": "Create or overwrite a file with the given content. Prefer edit for existing files.",
        "parameters": {"type": "object", "properties": {
            "filePath": {"type": "string", "description": "Absolute path of the file to write"},
            "content": {"type": "string", "description": "Full content of the file"}},
            "required": ["filePath", "content"]}}},
    {"type": "function", "function": {
        "name": "edit",
        "description": "Replace an exact string in a file. oldString must match the file content exactly, "
                       "including indentation. Fails if oldString is not found or is not unique unless "
                       "replaceAll is true.",
        "parameters": {"type": "object", "properties": {
            "filePath": {"type": "string", "description": "Absolute path of the file to modify"},
            "oldString": {"type": "string", "description": "Text to replace"},
            "newString": {"type": "string", "description": "Replacement text"},
            "replaceAll": {"type": "boolean", "description": "Replace every occurrence"}},
            "required": ["filePath", "oldString", "newString"]}}},
    {"type": "function", "function": {
        "name": "bash",
        "description": "Run a shell command in the project directory, for example to build or test with "
                       "Maven. Output is truncated to the last 200 lines.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string", "description": "The command to run"},
            "description": {"type": "string", "description": "Short description of what the command does"},
            "timeout": {"type": "integer", "description": "Timeout in milliseconds"}},
            "required": ["command"]}}},
    {"type": "function", "function": {
        "name": "glob",
        "description": "Find files by name pattern, for example **/*.java.",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string", "description": "Glob pattern"},
            "path": {"type": "string", "description": "Directory to search in"}},
            "required": ["pattern"]}}},
    {"type": "function", "function": {
        "name": "grep",
        "description": "Search file contents with a regular expression. Returns matching lines with paths "
                       "and line numbers.",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string", "description": "Regular expression"},
            "path": {"type": "string", "description": "Directory to search in"},
            "include": {"type": "string", "description": "File pattern filter, for example *.java"}},
            "required": ["pattern"]}}},
    {"type": "function", "function": {
        "name": "list",
        "description": "List files and directories under a path.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Absolute directory path"}},
            "required": []}}},
]

BUILD_COMMAND = re.compile(r"\b(mvn|mvnw|gradle|gradlew|javac|java|junit)\b")
MAX_TOOL_OUTPUT = 12000


class Workspace:
    """In-memory project on which tool calls are executed."""

    def __init__(self, rng: random.Random, tool_time: Tuple[float, float], fail_rate: float) -> None:
        self.files = dict(PROJECT_FILES)
        self.rng = rng
        self.tool_time = tool_time
        self.fail_rate = fail_rate
        self.failed_once = False

    def tree(self) -> str:
        return "\n".join("{}/{}".format(ROOT, name) for name in sorted(self.files))

    def _rel(self, path: Any) -> str:
        value = str(path or "").strip()
        if value.startswith(ROOT):
            value = value[len(ROOT):]
        return value.lstrip("./").lstrip("/")

    def execute(self, name: str, raw_arguments: str) -> Tuple[str, float, bool]:
        """Return (output, simulated seconds, ok)."""
        try:
            args = json.loads(raw_arguments or "{}")
            if not isinstance(args, dict):
                raise ValueError("arguments must be a JSON object")
        except ValueError as exc:
            return "Error: invalid tool arguments ({}).".format(exc), 0.0, False
        handler = getattr(self, "tool_" + name, None)
        if handler is None:
            # Tools this simulation does not model (todo lists, web fetch, ...).
            return "OK", 0.2, True
        try:
            return handler(args)
        except Exception as exc:  # a malformed call must not stop the simulation
            return "Error: {}".format(exc), 0.1, False

    def _path(self, args: dict) -> str:
        # OpenCode 1.x uses filePath, OpenCode 2 uses path.
        return self._rel(args.get("filePath") or args.get("path"))

    def tool_read(self, args: dict) -> Tuple[str, float, bool]:
        rel = self._path(args)
        if rel not in self.files:
            return "Error: file not found: {}/{}".format(ROOT, rel), 0.1, False
        lines = self.files[rel].splitlines()
        offset = int(args.get("offset") or 0)
        limit = int(args.get("limit") or 2000)
        body = "\n".join("{:05d}| {}".format(i + 1, line) for i, line in enumerate(lines[offset:offset + limit], offset))
        return "<file>\n{}\n</file>".format(body), 0.1, True

    def tool_write(self, args: dict) -> Tuple[str, float, bool]:
        rel = self._path(args)
        if not rel:
            return "Error: filePath is required", 0.1, False
        self.files[rel] = str(args.get("content") or "")
        return "Wrote {} lines to {}/{}".format(self.files[rel].count("\n") + 1, ROOT, rel), 0.2, True

    def tool_edit(self, args: dict) -> Tuple[str, float, bool]:
        rel = self._path(args)
        if rel not in self.files:
            return "Error: file not found: {}/{}".format(ROOT, rel), 0.1, False
        old, new = str(args.get("oldString") or ""), str(args.get("newString") or "")
        content = self.files[rel]
        count = content.count(old) if old else 0
        if count == 0:
            return "Error: oldString not found in {}. Read the file and retry.".format(rel), 0.1, False
        if count > 1 and not args.get("replaceAll"):
            return "Error: oldString found {} times; add context or use replaceAll.".format(count), 0.1, False
        self.files[rel] = content.replace(old, new) if args.get("replaceAll") else content.replace(old, new, 1)
        return "Edit applied to {}/{}".format(ROOT, rel), 0.2, True

    def tool_list(self, args: dict) -> Tuple[str, float, bool]:
        prefix = self._rel(args.get("path"))
        names = [n for n in sorted(self.files) if n.startswith(prefix)]
        return "\n".join(names) or "(empty)", 0.1, True

    def tool_glob(self, args: dict) -> Tuple[str, float, bool]:
        pattern = str(args.get("pattern") or "*").replace("**/", "*")
        names = [n for n in sorted(self.files) if fnmatch.fnmatch(n, pattern) or fnmatch.fnmatch(n.split("/")[-1], pattern)]
        return "\n".join("{}/{}".format(ROOT, n) for n in names) or "No files found", 0.1, True

    def tool_grep(self, args: dict) -> Tuple[str, float, bool]:
        try:
            regex = re.compile(str(args.get("pattern") or ""))
        except re.error as exc:
            return "Error: invalid pattern: {}".format(exc), 0.1, False
        include = str(args.get("include") or "*")
        hits = []
        for name in sorted(self.files):
            if not fnmatch.fnmatch(name.split("/")[-1], include):
                continue
            for number, line in enumerate(self.files[name].splitlines(), 1):
                if regex.search(line):
                    hits.append("{}/{}:{}: {}".format(ROOT, name, number, line.strip()))
        return "\n".join(hits[:100]) or "No matches", 0.2, True

    def tool_bash(self, args: dict) -> Tuple[str, float, bool]:
        command = str(args.get("command") or "")
        if BUILD_COMMAND.search(command):
            seconds = self.rng.uniform(*self.tool_time)
            java_files = [n for n in self.files if n.endswith(".java")]
            tests = sum(self.files[n].count("@Test") for n in java_files if "/test/" in n)
            if not self.failed_once and self.rng.random() < self.fail_rate:
                self.failed_once = True
                output = ("[INFO] Running edu.lab.chat.MessageTest\n"
                          "[ERROR] Tests run: {0}, Failures: 1, Errors: 0, Skipped: 0\n"
                          "[ERROR] MessageTest.roundTrip:14 expected: <hello | world> but was: <hello >\n"
                          "[ERROR] BUILD FAILURE").format(max(tests, 1))
                return output, seconds, True
            output = ("[INFO] Compiling {} source files with javac [debug release 21]\n"
                      "[INFO] Tests run: {}, Failures: 0, Errors: 0, Skipped: 0\n"
                      "[INFO] BUILD SUCCESS").format(len(java_files), max(tests, 1))
            return output, seconds, True
        if command.startswith(("ls", "find", "tree")):
            return "\n".join(sorted(self.files)), 0.2, True
        if command.startswith("cat "):
            return self.files.get(self._rel(command[4:].strip()), "cat: no such file"), 0.1, True
        if command.startswith("git"):
            return "On branch main\nChanges not staged for commit:\n  modified: src/main/java/edu/lab/chat", 0.3, True
        return "", 0.3, True

    # OpenCode 2 tool names.
    def tool_shell(self, args: dict) -> Tuple[str, float, bool]:
        return self.tool_bash(args)

    def tool_execute(self, args: dict) -> Tuple[str, float, bool]:
        return self.tool_bash({"command": str(args.get("code") or "")})

    def tool_question(self, args: dict) -> Tuple[str, float, bool]:
        return "The user is busy; proceed with reasonable assumptions and state them.", 5.0, True

    def tool_webfetch(self, args: dict) -> Tuple[str, float, bool]:
        return "Error: network access is not available in this environment.", 0.5, False

    tool_websearch = tool_webfetch
    tool_skill = tool_webfetch


def load_prompt(path: Optional[str]) -> Dict[str, Any]:
    """Load {"system", "tools", "subagent": {"system", "tools"}} captured from a real client."""
    if not path:
        return {"system": None, "tools": TOOLS, "subagent": None}
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    return {"system": data.get("system"), "tools": data.get("tools") or TOOLS, "subagent": data.get("subagent")}


def load_keys(path: Optional[str]) -> List[Tuple[str, str]]:
    if not path:
        key = core.api_key_from_env()
        return [("user", key)] if key else [("user", "")]
    with open(path, encoding="utf-8") as handle:
        rows = [row for row in csv.DictReader(handle) if row.get("api_key")]
    if not rows:
        raise ValueError("no keys in {}".format(path))
    return [(row["user_id"], row["api_key"]) for row in rows]


def agent_request(client: core.Client, model: str, messages: List[dict], tools: List[dict],
                  max_tokens: int, timeout: float) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"model": model, "messages": messages, "max_tokens": max_tokens,
                               "stream": True, "stream_options": {"include_usage": True}}
    if tools:  # vLLM rejects an empty tools list
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    result: Dict[str, Any] = {"ok": False, "error": None, "status": None, "ttft_s": None, "e2e_s": None,
                              "prompt_tokens": None, "completion_tokens": None, "finish_reason": None,
                              "content": "", "tool_calls": []}
    calls: Dict[int, Dict[str, Any]] = {}
    parts: List[str] = []
    start = time.monotonic()
    try:
        for elapsed, event in client.stream("/v1/chat/completions", payload, timeout):
            if event.get("done"):
                break
            if "error" in event:
                result["error"] = "stream error: " + core.short(event["error"], 80)
                break
            choice = core._first_choice(event)
            delta = choice.get("delta") or {}
            if delta.get("content") or delta.get("tool_calls"):
                if result["ttft_s"] is None:
                    result["ttft_s"] = elapsed
            if delta.get("content"):
                parts.append(delta["content"])
            for fragment in delta.get("tool_calls") or []:
                slot = calls.setdefault(int(fragment.get("index", 0)),
                                        {"id": None, "type": "function", "function": {"name": "", "arguments": ""}})
                if fragment.get("id"):
                    slot["id"] = fragment["id"]
                function = fragment.get("function") or {}
                if function.get("name"):
                    slot["function"]["name"] = function["name"]
                if function.get("arguments"):
                    slot["function"]["arguments"] += function["arguments"]
            if choice.get("finish_reason"):
                result["finish_reason"] = choice["finish_reason"]
            usage = event.get("usage")
            if usage:
                result["prompt_tokens"] = usage.get("prompt_tokens")
                result["completion_tokens"] = usage.get("completion_tokens")
    except urllib.error.HTTPError as exc:
        result["status"] = exc.code
        result["error"] = "HTTP {}".format(exc.code)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        result["error"] = "{}: {}".format(type(exc).__name__, core.short(exc, 80))
    result["e2e_s"] = time.monotonic() - start
    result["content"] = "".join(parts)
    result["tool_calls"] = [dict(calls[i], id=calls[i]["id"] or "call_{}".format(i)) for i in sorted(calls)]
    result["ok"] = result["error"] is None and result["ttft_s"] is not None
    if not result["ok"] and result["error"] is None:
        result["error"] = "empty response"
    return result


class Recorder:
    """Thread-safe collection of per-request and per-turn records."""

    def __init__(self, out_dir: str) -> None:
        self.lock = threading.Lock()
        self.requests: List[Dict[str, Any]] = []
        self.turns: List[Dict[str, Any]] = []
        self.request_log = open(os.path.join(out_dir, "requests.jsonl"), "w", encoding="utf-8")
        self.turn_log = open(os.path.join(out_dir, "turns.jsonl"), "w", encoding="utf-8")

    def request(self, record: Dict[str, Any]) -> None:
        with self.lock:
            self.requests.append(record)
            self.request_log.write(json.dumps(record) + "\n")
            self.request_log.flush()

    def turn(self, record: Dict[str, Any]) -> None:
        with self.lock:
            self.turns.append(record)
            self.turn_log.write(json.dumps(record) + "\n")
            self.turn_log.flush()

    def close(self) -> None:
        self.request_log.close()
        self.turn_log.close()


def run_student(index: int, args: Any, key: Tuple[str, str], prompt_def: Dict[str, Any],
                recorder: Recorder, t0: float, stop_at: float, hard_stop: float) -> None:
    rng = random.Random(args.seed * 1000 + index)
    user_id, api_key = key
    client = core.Client(args.base_url, api_key or None, args.cacert, args.timeout)
    time.sleep(rng.uniform(0, args.ramp_up))
    workspace = Workspace(rng, (args.tool_time_min, args.tool_time_max), args.test_fail_rate)
    tools = prompt_def["tools"]
    prompt = (prompt_def["system"] or SYSTEM_PROMPT).replace("{root}", ROOT).replace("{tree}", workspace.tree())
    messages: List[dict] = [{"role": "system", "content": prompt}]
    task = TASKS[index % len(TASKS)]
    requests_in_turn = [task] + rng.sample(FOLLOW_UPS, k=min(len(FOLLOW_UPS), args.max_turns - 1))

    def call_model(kind: str, turn: int, step: int, msgs: List[dict], tool_defs: List[dict]) -> Dict[str, Any]:
        """One model call, retried after HTTP 429/503 like a client with backoff."""
        retries = 0
        while True:
            res = agent_request(client, args.model, msgs, tool_defs, args.max_output, args.timeout)
            if res["status"] in (429, 503) and retries < args.max_retries:
                retries += 1
                log("retry", turn, step, res)
                time.sleep(min(30.0, 5.0 * retries) + rng.uniform(0, 2))
                continue
            log(kind, turn, step, res)
            return res

    def run_subagent(turn: int, call_args: str) -> Tuple[str, float, int]:
        """A nested agent with its own prompt and tools, as OpenCode's subagent tool starts."""
        definition = prompt_def.get("subagent") or {}
        try:
            task_prompt = str(json.loads(call_args or "{}").get("prompt") or "Explore the project.")
        except ValueError:
            task_prompt = "Explore the project."
        msgs = [{"role": "system", "content": definition.get("system") or SYSTEM_PROMPT.replace("{root}", ROOT)
                 .replace("{tree}", workspace.tree())},
                {"role": "user", "content": "You are a subagent spawned by another session.\n" + task_prompt}]
        sub_tools = definition.get("tools") or [t for t in TOOLS if t["function"]["name"] in ("read", "glob", "grep")]
        spent, steps = 0.0, 0
        while steps < args.max_steps and time.monotonic() < hard_stop:
            steps += 1
            res = call_model("subagent", turn, steps, msgs, sub_tools)
            if not res["ok"]:
                return "Error: the subagent failed ({})".format(res["error"]), spent, steps
            msgs.append(dict({"role": "assistant", "content": res["content"]},
                             **({"tool_calls": res["tool_calls"]} if res["tool_calls"] else {})))
            if not res["tool_calls"]:
                return res["content"] or "(no findings)", spent, steps
            for sub_call in res["tool_calls"]:
                output, seconds, _ = workspace.execute(sub_call["function"]["name"], sub_call["function"]["arguments"])
                time.sleep(seconds)
                spent += seconds
                msgs.append({"role": "tool", "tool_call_id": sub_call["id"], "content": output[-MAX_TOOL_OUTPUT:]})
        return "(subagent stopped at the step limit)", spent, steps

    def log(kind: str, turn: int, step: int, res: Dict[str, Any]) -> None:
        recorder.request({
            "user": index, "user_id": user_id, "kind": kind, "turn": turn, "step": step,
            "start_s": round(time.monotonic() - res["e2e_s"] - t0, 3), "ok": res["ok"], "error": res["error"],
            "ttft_s": res["ttft_s"], "e2e_s": res["e2e_s"], "prompt_tokens": res["prompt_tokens"],
            "completion_tokens": res["completion_tokens"], "finish_reason": res["finish_reason"],
            "tool_calls": len(res["tool_calls"]),
            "tools": [c["function"]["name"] for c in res["tool_calls"]]})

    if args.title_request:
        title = [{"role": "user", "content": "Generate a title of at most six words for this request: " + task}]
        call_model("title", 0, 0, title, [])

    for turn, text in enumerate(requests_in_turn, 1):
        if time.monotonic() >= stop_at:
            break
        messages.append({"role": "user", "content": text})
        turn_start = time.monotonic()
        model_s = tool_s = 0.0
        steps = tool_errors = compactions = subagent_steps = 0
        completed = False
        while steps < args.max_steps and time.monotonic() < hard_stop:
            steps += 1
            res = call_model("agent", turn, steps, messages, tools)
            model_s += res["e2e_s"]
            if not res["ok"]:
                break
            assistant: Dict[str, Any] = {"role": "assistant", "content": res["content"]}
            if res["tool_calls"]:
                assistant["tool_calls"] = res["tool_calls"]
            messages.append(assistant)
            if not res["tool_calls"]:
                completed = True
                break
            for call in res["tool_calls"]:
                if call["function"]["name"] in ("subagent", "task"):
                    output, seconds, sub_steps = run_subagent(turn, call["function"]["arguments"])
                    subagent_steps += sub_steps
                    tool_s += seconds
                    messages.append({"role": "tool", "tool_call_id": call["id"], "content": output[-MAX_TOOL_OUTPUT:]})
                    continue
                output, seconds, ok = workspace.execute(call["function"]["name"], call["function"]["arguments"])
                tool_errors += 0 if ok else 1
                time.sleep(seconds)
                tool_s += seconds
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": output[-MAX_TOOL_OUTPUT:]})
            used = (res["prompt_tokens"] or 0) + (res["completion_tokens"] or 0)
            # Leave room for the tool outputs appended since the last call.
            if used > args.context_limit - args.max_output - 4096:
                # Like OpenCode's automatic compaction: keep the task, drop the transcript.
                compactions += 1
                messages = [messages[0], {"role": "user", "content": "Summary of the work so far: the task was: "
                                          + task + " Continue with: " + text}]
        recorder.turn({
            "user": index, "user_id": user_id, "turn": turn, "completed": completed, "steps": steps,
            "subagent_steps": subagent_steps, "tool_errors": tool_errors, "compactions": compactions,
            "turn_s": round(time.monotonic() - turn_start, 3), "model_s": round(model_s, 3),
            "tool_s": round(tool_s, 3)})
        if not completed:
            break
        # Reading the answer, looking at the code, running things locally.
        pause = rng.uniform(args.think_time_min, args.think_time_max)
        time.sleep(max(0.0, min(pause, stop_at - time.monotonic())))


def summarize(recorder: Recorder, wall: float) -> Dict[str, Any]:
    agent = [r for r in recorder.requests if r["kind"] in ("agent", "subagent")]
    ok = [r for r in agent if r["ok"]]
    turns = recorder.turns
    done = [t for t in turns if t["completed"]]
    errors: Dict[str, int] = {}
    for r in recorder.requests:
        if not r["ok"]:
            errors[r["error"]] = errors.get(r["error"], 0) + 1
    decode = [(r["completion_tokens"] - 1) / (r["e2e_s"] - r["ttft_s"]) for r in ok
              if r["completion_tokens"] and r["completion_tokens"] > 8 and r["e2e_s"] > r["ttft_s"]]
    out_tokens = sum(r["completion_tokens"] or 0 for r in ok)
    prompts = [r["prompt_tokens"] for r in ok if r["prompt_tokens"]]

    def pct(values: List[float], p: float, digits: int = 2) -> Optional[float]:
        value = core.percentile(values, p)
        return None if value is None else round(value, digits)

    return {
        "wall_time_s": round(wall, 1),
        "users_active": len({r["user"] for r in recorder.requests}),
        "requests": len(agent),
        "requests_ok": len(ok),
        "retries_after_429_503": sum(1 for r in recorder.requests if r["kind"] == "retry"),
        "errors": errors,
        "tool_calls": sum(r["tool_calls"] for r in ok),
        "turns_started": len(turns),
        "turns_completed": len(done),
        "subagent_requests": sum(1 for r in recorder.requests if r["kind"] == "subagent"),
        "compactions": sum(t["compactions"] for t in turns),
        "tool_errors": sum(t["tool_errors"] for t in turns),
        "ttft_p50_s": pct([r["ttft_s"] for r in ok], 50),
        "ttft_p95_s": pct([r["ttft_s"] for r in ok], 95),
        "ttft_p99_s": pct([r["ttft_s"] for r in ok], 99),
        "request_p50_s": pct([r["e2e_s"] for r in ok], 50),
        "request_p95_s": pct([r["e2e_s"] for r in ok], 95),
        "decode_tok_s_p50": pct(decode, 50, 1),
        "decode_tok_s_p10": pct(decode, 10, 1),
        "turn_model_time_p50_s": pct([t["model_s"] for t in done], 50),
        "turn_model_time_p95_s": pct([t["model_s"] for t in done], 95),
        "turn_total_time_p50_s": pct([t["turn_s"] for t in done], 50),
        "turn_total_time_p95_s": pct([t["turn_s"] for t in done], 95),
        "steps_per_turn_p50": pct([t["steps"] for t in done], 50, 1),
        "prompt_tokens_p50": pct(prompts, 50, 0),
        "prompt_tokens_p95": pct(prompts, 95, 0),
        "prompt_tokens_max": max(prompts) if prompts else None,
        "output_tokens_total": out_tokens,
        "output_tok_s": round(out_tokens / wall, 1) if wall else None,
        "requests_per_min": round(60 * len(agent) / wall, 1) if wall else None,
    }


def print_report(summary: Dict[str, Any], server: Dict[str, Any]) -> None:
    f = core._fmt
    print("Results")
    print("  users active / wall time   : {} / {} s".format(summary["users_active"], summary["wall_time_s"]))
    print("  agent requests (ok)        : {} ({}), {} per minute".format(
        summary["requests"], summary["requests_ok"], f(summary["requests_per_min"])))
    print("  retries after 429/503      : {}".format(summary["retries_after_429_503"]))
    print("  errors                     : {}".format(summary["errors"] or "none"))
    print("  turns completed / started  : {} / {}  (compactions: {})".format(
        summary["turns_completed"], summary["turns_started"], summary["compactions"]))
    print("  tool calls (tool errors)   : {} ({}), subagent calls: {}".format(
        summary["tool_calls"], summary["tool_errors"], summary["subagent_requests"]))
    print("  TTFT p50 / p95 / p99       : {} / {} / {} s".format(
        f(summary["ttft_p50_s"]), f(summary["ttft_p95_s"]), f(summary["ttft_p99_s"])))
    print("  request time p50 / p95     : {} / {} s".format(f(summary["request_p50_s"]), f(summary["request_p95_s"])))
    print("  output speed per user p50  : {} tok/s (slowest 10%: {} tok/s)".format(
        f(summary["decode_tok_s_p50"]), f(summary["decode_tok_s_p10"])))
    print("  model time per turn p50/p95: {} / {} s ({} steps per turn)".format(
        f(summary["turn_model_time_p50_s"]), f(summary["turn_model_time_p95_s"]), f(summary["steps_per_turn_p50"])))
    print("  prompt tokens p50/p95/max  : {} / {} / {}".format(
        f(summary["prompt_tokens_p50"]), f(summary["prompt_tokens_p95"]), f(summary["prompt_tokens_max"])))
    print("  output tokens total (tok/s): {} ({})".format(summary["output_tokens_total"], f(summary["output_tok_s"])))
    if server:
        print("Server (sampled from /metrics)")
        for key, value in server.items():
            print("  {:<27}: {}".format(key.replace("_", " "), f(value)))


def read_metrics(url: Optional[str]) -> Optional[Dict[str, Any]]:
    if not url:
        return None
    try:
        return core.summarize_metrics(core.fetch_metrics(url, timeout=5))
    except (urllib.error.URLError, OSError, RuntimeError, ValueError):
        return None


def run(args: Any) -> int:
    keys = load_keys(args.api_keys_file)
    prompt_def = load_prompt(args.prompt_file)
    metrics_url = None if args.metrics_url in (None, "", "none") else args.metrics_url
    print("Codendum classroom simulation  {}".format(core.utc_now()))
    print("  target     : {}  model={}".format(args.base_url.rstrip("/"), args.model))
    print("  users      : {} over {} API key(s), ramp-up {} s, duration {} s".format(
        args.users, len(keys), args.ramp_up, args.duration))
    print("  behaviour  : think time {}-{} s, build/test time {}-{} s, up to {} steps per request, {} requests per user".format(
        args.think_time_min, args.think_time_max, args.tool_time_min, args.tool_time_max, args.max_steps, args.max_turns))
    print("  prompt     : {}".format(args.prompt_file or "built-in OpenCode-like system prompt and tools"))
    if not args.yes:
        if not sys.stdin.isatty():
            core.eprint("error: refusing to start without --yes when stdin is not a terminal")
            return core.EXIT_USAGE
        if input("This loads the server for the whole duration. Continue? [y/N] ").strip().lower() not in ("y", "yes"):
            print("aborted")
            return core.EXIT_USAGE
    load = core.server_load(metrics_url)
    if load and not args.allow_busy:
        core.eprint("error: the server is busy ({} requests running or waiting); use --allow-busy to override".format(
            core._fmt(load)))
        return core.EXIT_FAIL

    started = core.utc_now()
    out_dir = args.out_dir or os.path.join("bench-results", "classroom-" + started.replace(":", "").replace("-", ""))
    os.makedirs(out_dir, exist_ok=True)
    recorder = Recorder(out_dir)
    before = read_metrics(metrics_url)
    sampler = core.MetricsSampler(metrics_url)
    sampler.start()
    t0 = time.monotonic()
    stop_at = t0 + args.duration
    hard_stop = stop_at + args.grace
    threads = [threading.Thread(target=run_student, daemon=True,
                                args=(i, args, keys[i % len(keys)], prompt_def, recorder, t0, stop_at, hard_stop))
               for i in range(args.users)]
    for thread in threads:
        thread.start()
    last = 0.0
    while any(t.is_alive() for t in threads) and time.monotonic() < hard_stop + args.timeout:
        time.sleep(1)
        now = time.monotonic() - t0
        if now - last >= 60:
            last = now
            with recorder.lock:
                done = sum(1 for r in recorder.requests if r["kind"] in ("agent", "subagent"))
            print("  t={:>5.0f}s  agent requests so far: {}".format(now, done), flush=True)
    wall = time.monotonic() - t0
    sampler.stop()
    recorder.close()
    after = read_metrics(metrics_url)
    summary = summarize(recorder, wall)
    server: Dict[str, Any] = {}
    if before and after:
        def delta(key: str) -> Optional[float]:
            a, b = after.get(key), before.get(key)
            return None if a is None or b is None else a - b
        queries, hits = delta("prefix_cache_queries_total"), delta("prefix_cache_hits_total")
        server = {
            "running_peak": sampler.peak_running,
            "waiting_peak": sampler.peak_waiting,
            "kv_cache_peak_percent": sampler.peak_kv,
            "preemptions": delta("preemptions_total"),
            "prefix_cache_hit_rate_percent": round(100 * hits / queries, 1) if queries else None,
        }
    meta = {"started": started, "finished": core.utc_now(), "tool_version": core.__version__,
            "target": args.base_url, "model": args.model, "profile": args.profile, "users": args.users,
            "api_keys": len(keys), "duration_s": args.duration, "ramp_up_s": args.ramp_up,
            "think_time_s": [args.think_time_min, args.think_time_max],
            "tool_time_s": [args.tool_time_min, args.tool_time_max], "max_steps": args.max_steps,
            "max_turns": args.max_turns, "max_output": args.max_output, "context_limit": args.context_limit,
            "prompt_file": bool(args.prompt_file), "seed": args.seed}
    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as handle:
        json.dump({"meta": meta, "client": summary, "server": server}, handle, indent=2)
    print_report(summary, server)
    print("Results written to {}".format(out_dir))
    return core.EXIT_OK if summary["requests_ok"] else core.EXIT_FAIL
