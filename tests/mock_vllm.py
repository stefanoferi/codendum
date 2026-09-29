#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Stefano Noferi
"""Tiny stand-in for the vLLM OpenAI-compatible server, used by offline tests.

It implements just enough of /health, /metrics, /v1/models and
/v1/chat/completions (JSON and SSE streaming, tool calls) to exercise the
Codendum scripts and the nginx example configuration without a GPU.
It is not a model and must never be used for anything but tests.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE = {"requests": 0, "running": 0, "prompt_tokens": 0, "generation_tokens": 0}
LOCK = threading.Lock()


def metrics_text(max_model_len: int, waiting: int = 0) -> str:
    with LOCK:
        running, done = STATE["running"], STATE["requests"]
        prompt, gen = STATE["prompt_tokens"], STATE["generation_tokens"]
    return "\n".join([
        "# HELP vllm:num_requests_running Number of requests in model execution batches.",
        "# TYPE vllm:num_requests_running gauge",
        'vllm:num_requests_running{engine="0",model_name="coder"} %d' % running,
        "# TYPE vllm:num_requests_waiting gauge",
        'vllm:num_requests_waiting{engine="0",model_name="coder"} %d' % waiting,
        "# TYPE vllm:kv_cache_usage_perc gauge",
        'vllm:kv_cache_usage_perc{engine="0",model_name="coder"} %.4f' % min(0.05 * running, 1.0),
        "# TYPE vllm:num_preemptions_total counter",
        'vllm:num_preemptions_total{engine="0",model_name="coder"} %d' % (done // 10),
        'vllm:prompt_tokens_total{engine="0",model_name="coder"} %d' % prompt,
        'vllm:generation_tokens_total{engine="0",model_name="coder"} %d' % gen,
        'vllm:prefix_cache_queries_total{engine="0",model_name="coder"} %d' % max(prompt, 1),
        'vllm:prefix_cache_hits_total{engine="0",model_name="coder"} %d' % (prompt // 4),
        'vllm:request_success_total{engine="0",finished_reason="length",model_name="coder"} %d' % done,
        'vllm:request_success_total{engine="0",finished_reason="stop",model_name="coder"} 0',
        'vllm:time_to_first_token_seconds_sum{engine="0",model_name="coder"} %.3f' % (0.05 * done),
        'vllm:time_to_first_token_seconds_count{engine="0",model_name="coder"} %d' % done,
        'vllm:cache_config_info{block_size="16",cache_dtype="fp8",engine="0",num_gpu_blocks="%d"} 1.0'
        % (max_model_len * 20 // 16),
        "",
    ])


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "mock-vllm"
    options: argparse.Namespace

    def log_message(self, fmt: str, *args) -> None:  # keep test output quiet
        if self.options.verbose:
            sys.stderr.write("mock: " + fmt % args + "\n")

    # ---- helpers ---------------------------------------------------------
    def _send(self, status: int, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, obj) -> None:
        self._send(status, json.dumps(obj).encode())

    def _authorized(self) -> bool:
        key = self.options.require_key
        if not key:
            return True
        if self.headers.get("Authorization") == "Bearer " + key:
            return True
        self._json(401, {"error": "Unauthorized"})
        return False

    def _chunk(self, data: bytes) -> None:
        self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
        self.wfile.flush()

    # ---- routes -------------------------------------------------------------
    def do_GET(self) -> None:
        if self.path == "/health":
            self._send(200, b"", "text/plain")
        elif self.path.startswith("/metrics"):  # vLLM mounts /metrics as a prefix route
            body = metrics_text(self.options.max_model_len, self.options.fake_waiting)
            self._send(200, body.encode(), "text/plain; version=0.0.4")
        elif self.path in ("/version", "/ping", "/server_info", "/load"):
            self._json(200, {"version": "mock"})
        elif self.path == "/v1/models":
            if self._authorized():
                self._json(200, {"object": "list", "data": [{
                    "id": "coder", "object": "model", "owned_by": "vllm",
                    "root": "Qwen/Qwen3-Coder-30B-A3B-Instruct-FP8",
                    "max_model_len": self.options.max_model_len}]})
        else:
            self._json(404, {"detail": "Not Found"})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if self.path in ("/tokenize", "/v1/embeddings", "/v1/completions"):
            self._json(200, {"mock": True})
            return
        if self.path != "/v1/chat/completions":
            self._json(404, {"detail": "Not Found"})
            return
        if not self._authorized():
            return
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            self._json(400, {"error": "invalid JSON"})
            return
        with LOCK:
            STATE["running"] += 1
        try:
            self._complete(body)
        finally:
            with LOCK:
                STATE["running"] -= 1
                STATE["requests"] += 1

    def _complete(self, body: dict) -> None:
        messages = body.get("messages") or []
        prompt_tokens = sum(len(str(m.get("content") or "").split()) for m in messages) + 8
        max_tokens = int(body.get("max_tokens") or 16)
        tools = body.get("tools") or []
        choice = body.get("tool_choice", "auto")
        last_role = messages[-1].get("role") if messages else "user"
        message: dict = {"role": "assistant", "content": None}
        finish = "stop"
        if tools and last_role != "tool" and choice != "none" and (
                self.options.tool_mode == "call" or choice == "required" or isinstance(choice, dict)):
            # Call the first declared tool with arguments that suit the common names.
            name = ((tools[0].get("function") or {}).get("name")) or "read_file"
            arguments = {"path": "src/main/java/App.java", "filePath": "README.md"}
            message["tool_calls"] = [{
                "id": "chatcmpl-tool-" + uuid.uuid4().hex[:12], "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)}}]
            finish = "tool_calls"
        elif tools and last_role != "tool" and self.options.tool_mode == "raw-markup":
            message["content"] = ("<tool_call>\n<function=read_file>\n<parameter=path>\n"
                                  "src/main/java/App.java\n</parameter>\n</function>\n</tool_call>")
        elif tools and last_role != "tool":
            message["content"] = "I think it prints hello, but I did not open the file."
        elif last_role == "tool":
            message["content"] = 'The main method prints "hello, codendum".'
        else:
            message["content"] = "pong"
        completion_tokens = max_tokens if body.get("ignore_eos") else len((message["content"] or "x").split())
        with LOCK:
            STATE["prompt_tokens"] += prompt_tokens
            STATE["generation_tokens"] += completion_tokens
        usage = {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                 "total_tokens": prompt_tokens + completion_tokens}
        rid = "chatcmpl-" + uuid.uuid4().hex[:16]
        if not body.get("stream"):
            self._json(200, {"id": rid, "object": "chat.completion", "created": int(time.time()),
                             "model": body.get("model"), "usage": usage,
                             "choices": [{"index": 0, "message": message, "finish_reason": finish}]})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def event(payload: dict) -> None:
            self._chunk(b"data: " + json.dumps(payload).encode() + b"\n\n")

        base = {"id": rid, "object": "chat.completion.chunk", "created": int(time.time()), "model": body.get("model")}
        time.sleep(self.options.first_token_delay)
        event(dict(base, choices=[{"index": 0, "delta": {"role": "assistant", "content": ""}}]))
        pieces = (message["content"] or "").split(" ") if not body.get("ignore_eos") else ["tok"] * completion_tokens
        for i, piece in enumerate(p for p in pieces if p):
            event(dict(base, choices=[{"index": 0, "delta": {"content": piece if i == 0 else " " + piece}}]))
            time.sleep(self.options.stream_delay)
        for index, call in enumerate(message.get("tool_calls") or []):
            # Like vLLM: the call header first, then the arguments in fragments.
            args = call["function"]["arguments"]
            head = {"index": index, "id": call["id"], "type": "function",
                    "function": {"name": call["function"]["name"], "arguments": ""}}
            event(dict(base, choices=[{"index": 0, "delta": {"tool_calls": [head]}}]))
            for part in (args[: len(args) // 2], args[len(args) // 2:]):
                event(dict(base, choices=[{"index": 0, "delta": {"tool_calls": [
                    {"index": index, "function": {"arguments": part}}]}}]))
                time.sleep(self.options.stream_delay)
        event(dict(base, choices=[{"index": 0, "delta": {}, "finish_reason": "length" if body.get("ignore_eos") else finish}]))
        if (body.get("stream_options") or {}).get("include_usage"):
            event(dict(base, choices=[], usage=usage))
        self._chunk(b"data: [DONE]\n\n")
        self._chunk(b"")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0, help="0 picks a free port")
    parser.add_argument("--port-file", help="write the bound port to this file")
    parser.add_argument("--tool-mode", choices=["call", "none", "raw-markup"], default="call")
    parser.add_argument("--stream-delay", type=float, default=0.0, help="seconds between streamed chunks")
    parser.add_argument("--first-token-delay", type=float, default=0.0, help="seconds before the first chunk")
    parser.add_argument("--max-model-len", type=int, default=65536)
    parser.add_argument("--fake-waiting", type=int, default=0, help="report this many waiting requests")
    parser.add_argument("--require-key", help="expect this bearer token (defense-in-depth tests)")
    parser.add_argument("--verbose", action="store_true")
    options = parser.parse_args()
    Handler.options = options
    server = ThreadingHTTPServer((options.host, options.port), Handler)
    server.daemon_threads = True
    if options.port_file:
        with open(options.port_file + ".tmp", "w", encoding="ascii") as handle:
            handle.write(str(server.server_address[1]))
        os.replace(options.port_file + ".tmp", options.port_file)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
