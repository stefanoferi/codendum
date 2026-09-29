#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Stefano Noferi
"""Client-side helpers for Codendum.

Subcommands:
  smoke     functional checks of a vLLM OpenAI-compatible endpoint
  metrics   summary of vLLM Prometheus metrics plus host memory and GPU state
  bench     controlled load test at several concurrency levels
  gen-keys  generate per-user API keys for the nginx proxy
  classroom simulated class: users running OpenCode-like agent sessions

The shell scripts in scripts/ are thin wrappers around these subcommands.
Standard library only; Python 3.8 or newer.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import os
import platform
import random
import re
import secrets
import shutil
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Iterator, List, Optional, Tuple

__version__ = "0.1.0"

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_INCONCLUSIVE = 2
EXIT_USAGE = 64

API_KEY_ENV = "CODENDUM_API_KEY"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def utc_now() -> str:
    """Current time as ISO 8601 UTC with second precision."""
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def eprint(*args: Any) -> None:
    print(*args, file=sys.stderr)


# --------------------------------------------------------------------------
# HTTP client
# --------------------------------------------------------------------------


class Client:
    """Minimal JSON/SSE client for an OpenAI-compatible server."""

    def __init__(
        self,
        base_url: str,
        api_key: Optional[str] = None,
        cacert: Optional[str] = None,
        timeout: float = 60.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        parsed = urllib.parse.urlsplit(self.base_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("base URL must look like http(s)://host[:port]")
        handlers: List[Any] = []
        if parsed.hostname in LOOPBACK_HOSTS:
            # Never send loopback traffic through an HTTP proxy from the environment.
            handlers.append(urllib.request.ProxyHandler({}))
        if parsed.scheme == "https":
            context = ssl.create_default_context(cafile=cacert) if cacert else ssl.create_default_context()
            handlers.append(urllib.request.HTTPSHandler(context=context))
        self.opener = urllib.request.build_opener(*handlers)

    def _request(self, method: str, path: str, payload: Optional[dict], accept: str) -> urllib.request.Request:
        headers = {"Accept": accept, "User-Agent": "codendum/" + __version__}
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        return urllib.request.Request(self.base_url + path, data=data, method=method, headers=headers)

    def call(
        self, method: str, path: str, payload: Optional[dict] = None, timeout: Optional[float] = None
    ) -> Tuple[int, bytes, float]:
        """Return (status, body, elapsed_seconds). HTTP errors are returned, not raised."""
        req = self._request(method, path, payload, "application/json")
        start = time.monotonic()
        try:
            with self.opener.open(req, timeout=timeout or self.timeout) as resp:
                return resp.status, resp.read(), time.monotonic() - start
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), time.monotonic() - start

    def stream(self, path: str, payload: dict, timeout: Optional[float] = None) -> Iterator[Tuple[float, dict]]:
        """Yield (elapsed_seconds, event) for each server-sent event.

        The final "[DONE]" marker is yielded as {"done": True}. HTTP errors raise
        urllib.error.HTTPError. The timeout applies to each socket read.
        """
        req = self._request("POST", path, payload, "text/event-stream")
        start = time.monotonic()
        with self.opener.open(req, timeout=timeout or self.timeout) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    yield time.monotonic() - start, {"done": True}
                    return
                yield time.monotonic() - start, json.loads(data)


def api_key_from_env() -> Optional[str]:
    value = os.environ.get(API_KEY_ENV, "").strip()
    return value or None


def parse_json(body: bytes) -> Any:
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None


def short(text: Any, limit: int = 120) -> str:
    value = str(text).replace("\n", " ")
    return value if len(value) <= limit else value[: limit - 3] + "..."


# --------------------------------------------------------------------------
# smoke
# --------------------------------------------------------------------------

READ_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Read a UTF-8 text file from the user's project and return its contents.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to the project root."}
            },
            "required": ["path"],
        },
    },
}

TOOL_MESSAGES = [
    {
        "role": "system",
        "content": (
            "You are a coding assistant working in the user's repository. "
            "You cannot see any file unless you call a tool."
        ),
    },
    {
        "role": "user",
        "content": (
            "What does the main method in src/main/java/App.java print? "
            "Use the read_file tool to open the file before answering."
        ),
    },
]

TOOL_RESULT = 'public class App { public static void main(String[] a) { System.out.println("hello, codendum"); } }'

# Qwen3-Coder emits tool calls as XML-like markup. If it shows up in the
# message content, the server-side tool parser did not extract it.
RAW_TOOL_MARKUP = re.compile(r"<tool_call>|<function=|<parameter=")

# Endpoints that must never be reachable through the public proxy.
PRIVATE_PATHS = ["/health", "/metrics", "/version", "/ping", "/tokenize", "/server_info", "/load"]
# Non-normalized paths that a careless proxy would forward verbatim to /metrics.
TRAVERSAL_PATHS = ["/metrics/../v1/models", "/v1/models/../../metrics"]


class Report:
    def __init__(self) -> None:
        self.rows: List[Tuple[str, str, str]] = []

    def add(self, status: str, name: str, detail: str) -> None:
        self.rows.append((status, name, detail))
        print("[{:<4}] {}: {}".format(status, name, detail), flush=True)

    def exit_code(self) -> int:
        states = {row[0] for row in self.rows}
        if "FAIL" in states:
            return EXIT_FAIL
        if "INCONCLUSIVE" in states:
            return EXIT_INCONCLUSIVE
        return EXIT_OK


def _first_choice(obj: Any) -> dict:
    if isinstance(obj, dict):
        choices = obj.get("choices") or []
        if choices and isinstance(choices[0], dict):
            return choices[0]
    return {}


def _check_tool_call(message: dict) -> Tuple[bool, str, Optional[dict]]:
    """Validate the first tool call of an assistant message."""
    calls = message.get("tool_calls") or []
    if not calls:
        return False, "no tool_calls in response", None
    call = calls[0]
    function = call.get("function") or {}
    if function.get("name") != "read_file":
        return False, "unexpected tool name {!r}".format(function.get("name")), call
    try:
        arguments = json.loads(function.get("arguments") or "")
    except ValueError:
        return False, "arguments are not valid JSON: {}".format(short(function.get("arguments"))), call
    if not isinstance(arguments, dict) or not isinstance(arguments.get("path"), str):
        return False, "arguments lack a string 'path': {}".format(short(arguments)), call
    return True, "read_file(path={!r})".format(arguments["path"]), call


def cmd_smoke(args: argparse.Namespace) -> int:
    client = Client(args.base_url, api_key_from_env(), args.cacert, args.timeout)
    report = Report()
    model = args.model
    print("Codendum smoke test  {}  target={}  model={}".format(utc_now(), client.base_url, model))

    # 1. Health / private endpoint exposure
    if args.proxy:
        exposed = []
        for path in PRIVATE_PATHS:
            try:
                status, _, _ = client.call("GET", path)
            except (urllib.error.URLError, OSError) as exc:
                report.add("FAIL", "private endpoints", "cannot reach proxy: {}".format(exc))
                return report.exit_code()
            if status not in (401, 403, 404):
                exposed.append("{} -> {}".format(path, status))
        for path in TRAVERSAL_PATHS:
            status, body, _ = client.call("GET", path)
            if status == 200 and b"vllm:" in body:
                exposed.append("{} -> metrics".format(path))
        if exposed:
            report.add("FAIL", "private endpoints", "reachable through the proxy: " + ", ".join(exposed))
        else:
            report.add("PASS", "private endpoints", "not exposed ({}, dot-segment variants)".format(
                ", ".join(PRIVATE_PATHS)))
        if client.api_key:
            anonymous = Client(args.base_url, None, args.cacert, args.timeout)
            status, _, _ = anonymous.call("GET", "/v1/models")
            if status == 401:
                report.add("PASS", "authentication", "request without API key rejected with 401")
            else:
                report.add("FAIL", "authentication", "request without API key returned {}".format(status))
    else:
        try:
            status, _, elapsed = client.call("GET", "/health")
        except (urllib.error.URLError, OSError) as exc:
            report.add("FAIL", "health", "cannot connect: {}".format(exc))
            return report.exit_code()
        if status == 200:
            report.add("PASS", "health", "GET /health -> 200 ({:.0f} ms)".format(elapsed * 1000))
        else:
            report.add("FAIL", "health", "GET /health -> {}".format(status))

    # 2. Model list
    try:
        status, body, _ = client.call("GET", "/v1/models")
    except (urllib.error.URLError, OSError) as exc:
        report.add("FAIL", "models", "cannot connect: {}".format(exc))
        return report.exit_code()
    listing = parse_json(body)
    ids = [m.get("id") for m in (listing or {}).get("data", []) if isinstance(m, dict)] if isinstance(listing, dict) else []
    if status != 200:
        report.add("FAIL", "models", "GET /v1/models -> {} {}".format(status, short(body.decode("utf-8", "replace"))))
        return report.exit_code()
    if model not in ids:
        report.add("FAIL", "models", "model {!r} not served; available: {}".format(model, ids))
        return report.exit_code()
    entry = next(m for m in listing["data"] if m.get("id") == model)
    report.add("PASS", "models", "{!r} served (max_model_len={})".format(model, entry.get("max_model_len", "n/a")))

    # 3. Plain chat completion
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with the single word: pong"}],
        "max_tokens": 16,
        "temperature": 0,
    }
    status, body, elapsed = client.call("POST", "/v1/chat/completions", payload)
    choice = _first_choice(parse_json(body))
    content = ((choice.get("message") or {}).get("content") or "").strip()
    if status == 200 and content:
        report.add("PASS", "chat", "{!r} in {:.2f} s".format(short(content, 40), elapsed))
    else:
        report.add("FAIL", "chat", "status {} content {!r}".format(status, short(body.decode("utf-8", "replace"))))

    # 4. Streaming (verifies that SSE passes through without buffering issues)
    payload = dict(payload, stream=True, stream_options={"include_usage": True})
    chunks, ttft, done = 0, None, False
    try:
        for elapsed, event in client.stream("/v1/chat/completions", payload):
            if event.get("done"):
                done = True
                break
            delta = (_first_choice(event).get("delta") or {})
            if delta.get("content"):
                chunks += 1
                ttft = ttft if ttft is not None else elapsed
    except urllib.error.HTTPError as exc:
        report.add("FAIL", "streaming", "HTTP {}".format(exc.code))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        report.add("FAIL", "streaming", "{}: {}".format(type(exc).__name__, exc))
    else:
        if done and chunks:
            report.add("PASS", "streaming", "{} content chunks, first after {:.2f} s, [DONE] received".format(chunks, ttft))
        else:
            report.add("FAIL", "streaming", "chunks={} done={}".format(chunks, done))

    if args.skip_tools:
        report.add("SKIP", "tools", "skipped by --skip-tools")
        return report.exit_code()

    # 5. Tool call with tool_choice=auto (exercises the server-side tool parser,
    #    which is what OpenCode relies on).
    payload = {
        "model": model,
        "messages": TOOL_MESSAGES,
        "tools": [READ_FILE_TOOL],
        "tool_choice": "auto",
        "max_tokens": 256,
        "temperature": 0,
    }
    status, body, elapsed = client.call("POST", "/v1/chat/completions", payload)
    choice = _first_choice(parse_json(body))
    message = choice.get("message") or {}
    auto_call: Optional[dict] = None
    call_message: dict = message
    if status != 200:
        report.add("FAIL", "tool call (auto)", "status {} {}".format(status, short(body.decode("utf-8", "replace"))))
    else:
        ok, detail, candidate = _check_tool_call(message)
        if ok:
            auto_call = candidate
            report.add("PASS", "tool call (auto)", "{} finish_reason={} ({:.2f} s)".format(
                detail, choice.get("finish_reason"), elapsed))
        elif RAW_TOOL_MARKUP.search(message.get("content") or ""):
            report.add("FAIL", "tool call (auto)",
                       "raw tool-call markup in content; check --enable-auto-tool-choice and --tool-call-parser")
        elif candidate is not None:
            report.add("FAIL", "tool call (auto)", detail)
        else:
            report.add("INCONCLUSIVE", "tool call (auto)",
                       "model answered without calling the tool: {!r}".format(short(message.get("content"), 80)))

    # 6. Tool call with tool_choice=required (structured output path; confirms
    #    that a well-formed call can be produced even if 'auto' was inconclusive).
    payload["tool_choice"] = "required"
    status, body, elapsed = client.call("POST", "/v1/chat/completions", payload)
    required_message = _first_choice(parse_json(body)).get("message") or {}
    if status != 200:
        report.add("FAIL", "tool call (required)", "status {} {}".format(status, short(body.decode("utf-8", "replace"))))
    else:
        ok, detail, required_call = _check_tool_call(required_message)
        report.add("PASS" if ok else "FAIL", "tool call (required)", detail)
        if auto_call is None and ok:
            auto_call, call_message = required_call, required_message

    # 7. Round trip: send the tool result back and expect a text answer.
    if auto_call is None:
        report.add("INCONCLUSIVE", "tool round trip", "no valid tool call to answer")
        return report.exit_code()
    messages = list(TOOL_MESSAGES) + [
        {"role": "assistant", "content": call_message.get("content") or "", "tool_calls": [auto_call]},
        {"role": "tool", "tool_call_id": auto_call.get("id", "call_0"), "content": TOOL_RESULT},
    ]
    payload = {"model": model, "messages": messages, "tools": [READ_FILE_TOOL], "max_tokens": 128, "temperature": 0}
    status, body, elapsed = client.call("POST", "/v1/chat/completions", payload)
    answer = ((_first_choice(parse_json(body)).get("message") or {}).get("content") or "").strip()
    if status == 200 and answer:
        note = "" if "hello" in answer.lower() else " (answer does not quote the file; review manually)"
        report.add("PASS", "tool round trip", "{!r}{}".format(short(answer, 60), note))
    else:
        report.add("FAIL", "tool round trip", "status {} {}".format(status, short(body.decode("utf-8", "replace"))))
    return report.exit_code()


# --------------------------------------------------------------------------
# metrics
# --------------------------------------------------------------------------

_SAMPLE_RE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})?\s+(\S+)(?:\s+-?\d+)?\s*$")
_LABEL_RE = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')

Sample = Tuple[str, Dict[str, str], float]


def parse_prometheus(text: str) -> List[Sample]:
    samples: List[Sample] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = _SAMPLE_RE.match(line)
        if not match:
            continue
        name, raw_labels, raw_value = match.groups()
        try:
            value = float(raw_value)
        except ValueError:
            continue
        labels = dict(_LABEL_RE.findall(raw_labels or ""))
        samples.append((name, labels, value))
    return samples


def _pick(samples: List[Sample], names: Tuple[str, ...], how: str = "sum") -> Optional[float]:
    for name in names:
        values = [v for n, _, v in samples if n == name and not math.isnan(v)]
        if values:
            return max(values) if how == "max" else sum(values)
    return None


def summarize_metrics(samples: List[Sample]) -> Dict[str, Any]:
    kv = _pick(samples, ("vllm:kv_cache_usage_perc", "vllm:gpu_cache_usage_perc"), "max")
    summary: Dict[str, Any] = {
        "kv_cache_usage_percent": None if kv is None else round(kv * 100, 2),
        "requests_running": _pick(samples, ("vllm:num_requests_running",)),
        "requests_waiting": _pick(samples, ("vllm:num_requests_waiting",)),
        "preemptions_total": _pick(samples, ("vllm:num_preemptions_total", "vllm:num_preemptions")),
        "prompt_tokens_total": _pick(samples, ("vllm:prompt_tokens_total",)),
        "generation_tokens_total": _pick(samples, ("vllm:generation_tokens_total",)),
        "prefix_cache_queries_total": _pick(samples, ("vllm:prefix_cache_queries_total", "vllm:gpu_prefix_cache_queries_total")),
        "prefix_cache_hits_total": _pick(samples, ("vllm:prefix_cache_hits_total", "vllm:gpu_prefix_cache_hits_total")),
        "kv_cache_capacity_tokens": None,
        "requests_finished": {},
        "ttft_mean_seconds": None,
        "queue_time_mean_seconds": None,
        "e2e_latency_mean_seconds": None,
    }
    for name, labels, _ in samples:
        if name == "vllm:cache_config_info":
            try:
                summary["kv_cache_capacity_tokens"] = int(labels["num_gpu_blocks"]) * int(labels["block_size"])
            except (KeyError, ValueError):
                pass
            break
    for name, labels, value in samples:
        if name == "vllm:request_success_total":
            reason = labels.get("finished_reason", "unknown")
            summary["requests_finished"][reason] = summary["requests_finished"].get(reason, 0) + value
    for key, base in (("ttft_mean_seconds", "vllm:time_to_first_token_seconds"),
                      ("queue_time_mean_seconds", "vllm:request_queue_time_seconds"),
                      ("e2e_latency_mean_seconds", "vllm:e2e_request_latency_seconds")):
        total, count = _pick(samples, (base + "_sum",)), _pick(samples, (base + "_count",))
        if total is not None and count:
            summary[key] = round(total / count, 4)
    queries, hits = summary["prefix_cache_queries_total"], summary["prefix_cache_hits_total"]
    summary["prefix_cache_hit_rate_percent"] = round(100 * hits / queries, 2) if queries and hits is not None else None
    return summary


def host_memory() -> Optional[Dict[str, float]]:
    try:
        with open("/proc/meminfo", encoding="ascii") as handle:
            info = {}
            for line in handle:
                key, _, rest = line.partition(":")
                info[key] = int(rest.split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    gib = float(1 << 30)
    return {
        "mem_total_gib": round(info.get("MemTotal", 0) / gib, 2),
        "mem_available_gib": round(info.get("MemAvailable", 0) / gib, 2),
        "swap_used_gib": round((info.get("SwapTotal", 0) - info.get("SwapFree", 0)) / gib, 2),
    }


def gpu_status() -> Optional[List[Dict[str, str]]]:
    if not shutil.which("nvidia-smi"):
        return None
    fields = ["name", "utilization.gpu", "memory.used", "memory.total", "temperature.gpu", "power.draw"]
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=" + ",".join(fields), "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    gpus = []
    for line in out.strip().splitlines():
        values = [v.strip() for v in line.split(",")]
        gpus.append(dict(zip(fields, values)))
    return gpus


def fetch_metrics(url: str, timeout: float = 10.0) -> List[Sample]:
    parsed = urllib.parse.urlsplit(url)
    base = "{}://{}".format(parsed.scheme, parsed.netloc)
    client = Client(base, api_key_from_env(), None, timeout)
    status, body, _ = client.call("GET", parsed.path or "/metrics")
    if status != 200:
        raise RuntimeError("GET {} -> {}".format(url, status))
    return parse_prometheus(body.decode("utf-8", "replace"))


def _fmt(value: Any, unit: str = "") -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return "{}{}".format(value, unit)


def print_metrics_report(summary: Dict[str, Any], memory: Optional[dict], gpus: Optional[list]) -> None:
    print("vLLM")
    print("  KV cache usage       : {}".format(_fmt(summary["kv_cache_usage_percent"], " %")))
    print("  KV cache capacity    : {}".format(_fmt(summary["kv_cache_capacity_tokens"], " tokens")))
    print("  Requests running     : {}".format(_fmt(summary["requests_running"])))
    print("  Requests waiting     : {}".format(_fmt(summary["requests_waiting"])))
    print("  Preemptions (total)  : {}".format(_fmt(summary["preemptions_total"])))
    print("  Prefix cache hit rate: {}".format(_fmt(summary["prefix_cache_hit_rate_percent"], " %")))
    print("  Prompt tokens (total): {}".format(_fmt(summary["prompt_tokens_total"])))
    print("  Output tokens (total): {}".format(_fmt(summary["generation_tokens_total"])))
    print("  Mean TTFT            : {}".format(_fmt(summary["ttft_mean_seconds"], " s")))
    print("  Mean queue time      : {}".format(_fmt(summary["queue_time_mean_seconds"], " s")))
    print("  Mean request latency : {}".format(_fmt(summary["e2e_latency_mean_seconds"], " s")))
    finished = ", ".join("{}={}".format(k, _fmt(v)) for k, v in sorted(summary["requests_finished"].items()))
    print("  Finished requests    : {}".format(finished or "n/a"))
    print("Host memory (unified CPU/GPU on GB10)")
    if memory:
        print("  Total / available    : {} GiB / {} GiB".format(memory["mem_total_gib"], memory["mem_available_gib"]))
        print("  Swap used            : {} GiB".format(memory["swap_used_gib"]))
    else:
        print("  unavailable (/proc/meminfo not readable)")
    print("GPU (nvidia-smi)")
    if gpus:
        for gpu in gpus:
            print("  {name}: util {utilization.gpu} %, memory {memory.used}/{memory.total} MiB, "
                  "{temperature.gpu} C, {power.draw} W".format(**gpu))
        print("  note: on unified-memory systems nvidia-smi may report memory as [N/A]; use host memory.")
    else:
        print("  unavailable (nvidia-smi not found or failed)")


def cmd_metrics(args: argparse.Namespace) -> int:
    if args.watch:
        previous: Optional[Dict[str, Any]] = None
        previous_time = 0.0
        try:
            while True:
                now = time.monotonic()
                try:
                    summary = summarize_metrics(fetch_metrics(args.url))
                except (urllib.error.URLError, OSError, RuntimeError) as exc:
                    print("{} error: {}".format(utc_now(), exc), flush=True)
                    time.sleep(args.watch)
                    continue
                memory = host_memory() or {}
                if args.json:
                    print(json.dumps({"timestamp": utc_now(), "vllm": summary, "host_memory": memory}), flush=True)
                    previous, previous_time = summary, now
                    time.sleep(args.watch)
                    continue
                rates = ""
                if previous is not None:
                    span = max(now - previous_time, 1e-6)
                    gen = (summary["generation_tokens_total"] or 0) - (previous["generation_tokens_total"] or 0)
                    pre = (summary["preemptions_total"] or 0) - (previous["preemptions_total"] or 0)
                    rates = " out_tok/s={:.1f} preempt+={}".format(gen / span, _fmt(pre))
                print("{} running={} waiting={} kv={} mem_avail={}{}".format(
                    utc_now(), _fmt(summary["requests_running"]), _fmt(summary["requests_waiting"]),
                    _fmt(summary["kv_cache_usage_percent"], "%"),
                    _fmt(memory.get("mem_available_gib"), "GiB"), rates), flush=True)
                previous, previous_time = summary, now
                time.sleep(args.watch)
        except KeyboardInterrupt:
            return EXIT_OK
    try:
        summary = summarize_metrics(fetch_metrics(args.url))
    except (urllib.error.URLError, OSError, RuntimeError) as exc:
        eprint("error: cannot read metrics from {}: {}".format(args.url, exc))
        return EXIT_FAIL
    memory, gpus = host_memory(), gpu_status()
    if args.json:
        print(json.dumps({"timestamp": utc_now(), "vllm": summary, "host_memory": memory, "gpus": gpus}, indent=2))
    else:
        print("Codendum metrics  {}  source={}".format(utc_now(), args.url))
        print_metrics_report(summary, memory, gpus)
    return EXIT_OK


# --------------------------------------------------------------------------
# bench
# --------------------------------------------------------------------------

# Common English words; with a leading space most of them are a single token
# for Qwen tokenizers, so N words is roughly N prompt tokens. The server-side
# token counts (usage.prompt_tokens) are what the report uses.
_WORDS = (
    "the of and to in is for that on with as it by this be are from at or an was have not "
    "which but all can has one more their will new about other some time only also when two "
    "into year first after use any these may such most over made well since used through "
    "system data file class method value test build error report server client request "
    "module package interface service result change update number list table code line"
).split()

SHAPES = ("short", "long")


def make_prompt(target_tokens: int, rng: random.Random, tag: str) -> str:
    words = " ".join(rng.choice(_WORDS) for _ in range(max(target_tokens - 24, 8)))
    return "[{}] Summarize the following notes in one paragraph.\n\n{}".format(tag, words)


def percentile(values: List[float], pct: float) -> Optional[float]:
    """Percentile with linear interpolation between closest ranks."""
    if not values:
        return None
    ordered = sorted(values)
    rank = (len(ordered) - 1) * pct / 100.0
    low, high = math.floor(rank), math.ceil(rank)
    if low == high:
        return ordered[int(rank)]
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


class MetricsSampler(threading.Thread):
    """Samples /metrics once per second and keeps peak values."""

    def __init__(self, url: Optional[str]) -> None:
        super().__init__(daemon=True)
        self.url = url
        self.stop_event = threading.Event()
        self.peak_kv: Optional[float] = None
        self.peak_running: Optional[float] = None
        self.peak_waiting: Optional[float] = None

    def run(self) -> None:
        while self.url and not self.stop_event.is_set():
            try:
                summary = summarize_metrics(fetch_metrics(self.url, timeout=5))
            except (urllib.error.URLError, OSError, RuntimeError, ValueError):
                summary = None
            if summary:
                for attr, key in (("peak_kv", "kv_cache_usage_percent"),
                                  ("peak_running", "requests_running"),
                                  ("peak_waiting", "requests_waiting")):
                    value = summary.get(key)
                    if value is not None:
                        current = getattr(self, attr)
                        setattr(self, attr, value if current is None else max(current, value))
            self.stop_event.wait(1.0)

    def stop(self) -> None:
        self.stop_event.set()
        if self.is_alive():
            self.join(timeout=10)


def run_one(client: Client, model: str, prompt: str, max_tokens: int, timeout: float) -> Dict[str, Any]:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0,
        "stream": True,
        "stream_options": {"include_usage": True},
        # vLLM extension: keep generating until max_tokens for repeatable output sizes.
        "ignore_eos": True,
    }
    result: Dict[str, Any] = {"ok": False, "error": None, "ttft_s": None, "e2e_s": None,
                              "prompt_tokens": None, "completion_tokens": None}
    start = time.monotonic()
    chunks = 0
    try:
        for elapsed, event in client.stream("/v1/chat/completions", payload, timeout):
            if event.get("done"):
                break
            if "error" in event:
                result["error"] = "stream error: " + short(event["error"], 80)
                break
            delta = _first_choice(event).get("delta") or {}
            if delta.get("content") or delta.get("reasoning_content") or delta.get("tool_calls"):
                chunks += 1
                if result["ttft_s"] is None:
                    result["ttft_s"] = elapsed
            usage = event.get("usage")
            if usage:
                result["prompt_tokens"] = usage.get("prompt_tokens")
                result["completion_tokens"] = usage.get("completion_tokens")
    except urllib.error.HTTPError as exc:
        result["error"] = "HTTP {}".format(exc.code)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        result["error"] = "{}: {}".format(type(exc).__name__, short(exc, 80))
    result["e2e_s"] = time.monotonic() - start
    if result["completion_tokens"] is None and chunks:
        result["completion_tokens"] = chunks
    result["ok"] = result["error"] is None and result["ttft_s"] is not None
    if not result["ok"] and result["error"] is None:
        result["error"] = "no output tokens received"
    return result


def server_load(url: Optional[str]) -> Optional[float]:
    if not url:
        return None
    try:
        summary = summarize_metrics(fetch_metrics(url, timeout=5))
    except (urllib.error.URLError, OSError, RuntimeError, ValueError):
        return None
    return (summary["requests_running"] or 0) + (summary["requests_waiting"] or 0)


def preemptions(url: Optional[str]) -> Optional[float]:
    if not url:
        return None
    try:
        return summarize_metrics(fetch_metrics(url, timeout=5))["preemptions_total"]
    except (urllib.error.URLError, OSError, RuntimeError, ValueError):
        return None


def wait_for_idle(url: Optional[str], limit_s: float = 180.0) -> None:
    deadline = time.monotonic() + limit_s
    while url and time.monotonic() < deadline:
        load = server_load(url)
        if not load:
            return
        time.sleep(2)


def summarize_level(shape: str, concurrency: int, results: List[Dict[str, Any]], wall: float,
                    preempt: Optional[float], sampler: MetricsSampler) -> Dict[str, Any]:
    ok = [r for r in results if r["ok"]]
    ttft = [r["ttft_s"] for r in ok]
    e2e = [r["e2e_s"] for r in ok]
    out_tokens = sum(r["completion_tokens"] or 0 for r in ok)
    in_tokens = sum(r["prompt_tokens"] or 0 for r in ok)
    tpot = [
        (r["e2e_s"] - r["ttft_s"]) / (r["completion_tokens"] - 1)
        for r in ok if r["completion_tokens"] and r["completion_tokens"] > 1
    ]
    errors: Dict[str, int] = {}
    for r in results:
        if not r["ok"]:
            errors[r["error"]] = errors.get(r["error"], 0) + 1

    def rnd(value: Optional[float], digits: int = 3) -> Optional[float]:
        return None if value is None else round(value, digits)

    return {
        "shape": shape,
        "concurrency": concurrency,
        "requests": len(results),
        "succeeded": len(ok),
        "failed": len(results) - len(ok),
        "errors": errors,
        "input_tokens_mean": rnd(in_tokens / len(ok), 1) if ok else None,
        "output_tokens_mean": rnd(out_tokens / len(ok), 1) if ok else None,
        "wall_time_s": rnd(wall, 3),
        "request_throughput_rps": rnd(len(ok) / wall) if wall > 0 else None,
        "output_throughput_tok_s": rnd(out_tokens / wall, 1) if wall > 0 else None,
        "total_throughput_tok_s": rnd((in_tokens + out_tokens) / wall, 1) if wall > 0 else None,
        "ttft_p50_s": rnd(percentile(ttft, 50)),
        "ttft_p95_s": rnd(percentile(ttft, 95)),
        "ttft_p99_s": rnd(percentile(ttft, 99)),
        "e2e_p50_s": rnd(percentile(e2e, 50)),
        "e2e_p95_s": rnd(percentile(e2e, 95)),
        "tpot_mean_ms": rnd(1000 * sum(tpot) / len(tpot), 1) if tpot else None,
        "preemptions": preempt,
        "kv_cache_peak_percent": sampler.peak_kv,
        "running_peak": sampler.peak_running,
        "waiting_peak": sampler.peak_waiting,
    }


CSV_FIELDS = [
    "shape", "concurrency", "requests", "succeeded", "failed", "input_tokens_mean", "output_tokens_mean",
    "wall_time_s", "request_throughput_rps", "output_throughput_tok_s", "total_throughput_tok_s",
    "ttft_p50_s", "ttft_p95_s", "ttft_p99_s", "e2e_p50_s", "e2e_p95_s", "tpot_mean_ms",
    "preemptions", "kv_cache_peak_percent", "running_peak", "waiting_peak",
]


def parse_int_list(text: str) -> List[int]:
    try:
        values = [int(part) for part in text.split(",") if part.strip()]
    except ValueError:
        raise argparse.ArgumentTypeError("expected a comma-separated list of integers: {!r}".format(text))
    if not values or any(v < 1 for v in values):
        raise argparse.ArgumentTypeError("values must be positive integers")
    return values


def positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("expected an integer: {!r}".format(text))
    if value < 1:
        raise argparse.ArgumentTypeError("expected an integer >= 1: {!r}".format(text))
    return value


def parse_shapes(text: str) -> List[str]:
    shapes = [part.strip() for part in text.split(",") if part.strip()]
    if not shapes or any(s not in SHAPES for s in shapes):
        raise argparse.ArgumentTypeError("shapes must be a comma-separated subset of: " + ",".join(SHAPES))
    return shapes


def cmd_bench(args: argparse.Namespace) -> int:
    client = Client(args.base_url, api_key_from_env(), args.cacert, args.timeout)
    metrics_url = None if args.metrics_url in ("", "none") else args.metrics_url
    input_tokens = {"short": args.short_input, "long": args.long_input}
    plan = []
    for shape in args.shapes:
        for level in args.concurrency:
            plan.append((shape, level, max(level * args.rounds, args.min_requests)))
    total_requests = sum(n for _, _, n in plan)
    approx_tokens = sum(n * (input_tokens[s] + args.output_tokens) for s, _, n in plan)

    print("Codendum benchmark plan")
    print("  target        : {}  model={}".format(client.base_url, args.model))
    print("  concurrency   : {}".format(", ".join(str(c) for c in args.concurrency)))
    print("  prompt shapes : {}".format(", ".join("{} (~{} input tokens)".format(s, input_tokens[s]) for s in args.shapes)))
    print("  output tokens : {} per request (ignore_eos)".format(args.output_tokens))
    print("  requests      : {} in total, ~{:.1f} M tokens processed".format(total_requests, approx_tokens / 1e6))
    print("  metrics       : {}".format(metrics_url or "disabled (no preemption / KV data)"))

    if not args.yes:
        if not sys.stdin.isatty():
            eprint("error: refusing to start without --yes when stdin is not a terminal")
            return EXIT_USAGE
        answer = input("This generates sustained load on the server. Continue? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("aborted")
            return EXIT_USAGE

    load = server_load(metrics_url)
    if load and not args.allow_busy:
        eprint("error: the server is busy ({} requests running or waiting). "
               "Benchmarks must not run during a session; use --allow-busy to override.".format(_fmt(load)))
        return EXIT_FAIL

    started = utc_now()
    out_dir = args.out_dir or os.path.join("bench-results", started.replace(":", "").replace("-", ""))
    os.makedirs(out_dir, exist_ok=True)
    rng = random.Random(args.seed)
    run_tag = secrets.token_hex(4)

    if not args.no_warmup:
        warm = run_one(client, args.model, make_prompt(64, rng, run_tag + "-warmup"), 8, args.timeout)
        if not warm["ok"]:
            eprint("error: warm-up request failed: {}".format(warm["error"]))
            return EXIT_FAIL

    summaries: List[Dict[str, Any]] = []
    with open(os.path.join(out_dir, "requests.jsonl"), "w", encoding="utf-8") as log:
        for shape, level, count in plan:
            wait_for_idle(metrics_url)
            prompts = [make_prompt(input_tokens[shape], rng, "{}-{}-{}-{}".format(run_tag, shape, level, i))
                       for i in range(count)]
            before = preemptions(metrics_url)
            sampler = MetricsSampler(metrics_url)
            sampler.start()
            start = time.monotonic()
            with ThreadPoolExecutor(max_workers=level) as pool:
                results = list(pool.map(
                    lambda p: run_one(client, args.model, p, args.output_tokens, args.timeout), prompts))
            wall = time.monotonic() - start
            sampler.stop()
            after = preemptions(metrics_url)
            delta = after - before if before is not None and after is not None else None
            for index, result in enumerate(results):
                log.write(json.dumps(dict(result, shape=shape, concurrency=level, index=index)) + "\n")
            summary = summarize_level(shape, level, results, wall, delta, sampler)
            summaries.append(summary)
            print("  {:<5} c={:<3} ok={}/{} ttft_p50={} ttft_p95={} out_tok/s={} preempt={}".format(
                shape, level, summary["succeeded"], summary["requests"], _fmt(summary["ttft_p50_s"], "s"),
                _fmt(summary["ttft_p95_s"], "s"), _fmt(summary["output_throughput_tok_s"]),
                _fmt(summary["preemptions"])), flush=True)

    meta = {
        "started": started,
        "finished": utc_now(),
        "tool_version": __version__,
        "target": client.base_url,
        "model": args.model,
        "profile": args.profile or None,
        "concurrency_levels": args.concurrency,
        "shapes": {s: input_tokens[s] for s in args.shapes},
        "output_tokens": args.output_tokens,
        "rounds": args.rounds,
        "seed": args.seed,
        "client_host": platform.node(),
        "percentile_method": "linear interpolation between closest ranks",
    }
    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as handle:
        json.dump({"meta": meta, "levels": summaries}, handle, indent=2)
    with open(os.path.join(out_dir, "summary.csv"), "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(summaries)
    print("Results written to {}".format(out_dir))
    return EXIT_FAIL if any(s["failed"] for s in summaries) else EXIT_OK


# --------------------------------------------------------------------------
# gen-keys
# --------------------------------------------------------------------------

USER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")
KEY_PREFIX = "cdm_"


def _write_exclusive(path: str, content: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)


def cmd_gen_keys(args: argparse.Namespace) -> int:
    if args.users_file:
        try:
            with open(args.users_file, encoding="utf-8") as handle:
                lines = [line.strip() for line in handle]
        except OSError as exc:
            eprint("error: cannot read users file: {}".format(exc))
            return EXIT_USAGE
        users = [line for line in lines if line and not line.startswith("#")]
        if not users:
            eprint("error: no user ids in {}".format(args.users_file))
            return EXIT_USAGE
    elif args.count:
        width = max(2, len(str(args.count)))
        users = ["{}{:0{}d}".format(args.prefix, i, width) for i in range(1, args.count + 1)]
    else:
        eprint("error: give --users-file FILE or --count N")
        return EXIT_USAGE
    invalid = [u for u in users if not USER_ID_RE.match(u)]
    if invalid:
        eprint("error: invalid user ids (allowed: lowercase letters, digits, '.', '_', '-'; max 32): {}".format(invalid[:5]))
        return EXIT_USAGE
    if len(set(users)) != len(users):
        eprint("error: duplicate user ids")
        return EXIT_USAGE
    os.makedirs(args.out_dir, mode=0o700, exist_ok=True)
    map_path = os.path.join(args.out_dir, "api-keys.map")
    csv_path = os.path.join(args.out_dir, "api-keys.csv")
    for path in (map_path, csv_path):
        if os.path.exists(path):
            eprint("error: {} already exists; refusing to overwrite. Use a new --out-dir for rotation.".format(path))
            return EXIT_FAIL
    keys = {user: KEY_PREFIX + secrets.token_hex(32) for user in users}
    header = "# Generated by Codendum on {}. Secret: do not commit or share.\n".format(utc_now())
    map_lines = ['"Bearer {}" "{}";\n'.format(key, user) for user, key in keys.items()]
    csv_lines = ["user_id,api_key\n"] + ["{},{}\n".format(user, key) for user, key in keys.items()]
    _write_exclusive(map_path, header + "".join(map_lines))
    _write_exclusive(csv_path, "".join(csv_lines))
    print("Generated {} keys".format(len(keys)))
    print("  nginx map       : {} (mode 0600)".format(map_path))
    print("  distribution CSV: {} (mode 0600)".format(csv_path))
    return EXIT_OK


def cmd_classroom(args: argparse.Namespace) -> int:
    import classroom  # sibling module; imported lazily to keep the other commands light

    return classroom.run(args)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codendum.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version="%(prog)s " + __version__)
    sub = parser.add_subparsers(dest="command")

    def add_target(p: argparse.ArgumentParser) -> None:
        p.add_argument("--base-url", default="http://127.0.0.1:8000",
                       help="server root URL without /v1 (default: %(default)s)")
        p.add_argument("--model", default="coder", help="served model name (default: %(default)s)")
        p.add_argument("--cacert", help="CA bundle for a private TLS certificate")

    smoke = sub.add_parser("smoke", help="functional checks")
    add_target(smoke)
    smoke.add_argument("--timeout", type=float, default=300.0, help="per-request timeout in seconds")
    smoke.add_argument("--proxy", action="store_true",
                       help="target is the public proxy: expect private endpoints to be blocked")
    smoke.add_argument("--skip-tools", action="store_true", help="skip tool-calling checks")
    smoke.set_defaults(func=cmd_smoke)

    metrics = sub.add_parser("metrics", help="metrics summary")
    metrics.add_argument("--url", default="http://127.0.0.1:8000/metrics", help="vLLM metrics URL")
    metrics.add_argument("--json", action="store_true",
                         help="machine-readable output (one JSON object per line with --watch)")
    metrics.add_argument("--watch", type=float, metavar="SECONDS", help="print one line every SECONDS")
    metrics.set_defaults(func=cmd_metrics)

    bench = sub.add_parser("bench", help="load test")
    add_target(bench)
    bench.add_argument("--metrics-url", default="http://127.0.0.1:8000/metrics",
                       help="vLLM metrics URL, or 'none' (default: %(default)s)")
    bench.add_argument("--concurrency", type=parse_int_list, default=[1, 8, 16, 24, 40],
                       help="comma-separated concurrency levels (default: 1,8,16,24,40)")
    bench.add_argument("--shapes", type=parse_shapes, default=list(SHAPES),
                       help="prompt shapes to run: short,long (default: both)")
    bench.add_argument("--short-input", type=positive_int, default=512, help="approx. input tokens, short prompts")
    bench.add_argument("--long-input", type=positive_int, default=16384, help="approx. input tokens, long prompts")
    bench.add_argument("--output-tokens", type=positive_int, default=256, help="output tokens per request")
    bench.add_argument("--rounds", type=positive_int, default=2, help="requests per worker at each level")
    bench.add_argument("--min-requests", type=positive_int, default=8, help="minimum requests per level")
    bench.add_argument("--timeout", type=float, default=900.0, help="per-request idle timeout in seconds")
    bench.add_argument("--seed", type=int, default=20260929, help="random seed for prompt text")
    bench.add_argument("--out-dir", help="output directory (default: bench-results/<UTC timestamp>)")
    bench.add_argument("--profile", help="profile label stored in the results")
    bench.add_argument("--no-warmup", action="store_true", help="skip the warm-up request")
    bench.add_argument("--allow-busy", action="store_true", help="run even if the server is serving requests")
    bench.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    bench.set_defaults(func=cmd_bench)

    klass = sub.add_parser("classroom", help="simulated class of agent users")
    add_target(klass)
    klass.add_argument("--api-keys-file", help="CSV from gen-api-keys (user_id,api_key); one key per user, "
                                               "reused if there are fewer keys than users")
    klass.add_argument("--metrics-url", default="none", help="vLLM metrics URL, or 'none' (default)")
    klass.add_argument("--users", type=positive_int, default=40, help="simulated users (default: %(default)s)")
    klass.add_argument("--duration", type=positive_int, default=900,
                       help="seconds during which users start new requests (default: %(default)s)")
    klass.add_argument("--ramp-up", type=float, default=120.0, help="users start within this many seconds")
    klass.add_argument("--grace", type=float, default=300.0,
                       help="extra seconds for requests in progress at the end (default: %(default)s)")
    klass.add_argument("--think-time-min", type=float, default=20.0, help="pause between requests, minimum s")
    klass.add_argument("--think-time-max", type=float, default=90.0, help="pause between requests, maximum s")
    klass.add_argument("--tool-time-min", type=float, default=3.0, help="simulated build/test time, minimum s")
    klass.add_argument("--tool-time-max", type=float, default=12.0, help="simulated build/test time, maximum s")
    klass.add_argument("--test-fail-rate", type=float, default=0.3,
                       help="probability that the first test run of a session fails (default: %(default)s)")
    klass.add_argument("--max-turns", type=positive_int, default=4, help="requests per user (default: %(default)s)")
    klass.add_argument("--max-steps", type=positive_int, default=40,
                       help="model calls per request, tool steps included (default: %(default)s)")
    klass.add_argument("--max-output", type=positive_int, default=8192, help="max_tokens per call (OpenCode limit.output)")
    klass.add_argument("--context-limit", type=positive_int, default=65536, help="client context limit (OpenCode limit.context)")
    klass.add_argument("--max-retries", type=int, default=5, help="retries after HTTP 429/503 per call")
    klass.add_argument("--no-title-request", dest="title_request", action="store_false",
                       help="skip the short title request OpenCode sends at the start of a session")
    klass.add_argument("--prompt-file", help='JSON {"system": ..., "tools": [...]} captured from a real client')
    klass.add_argument("--timeout", type=float, default=900.0, help="per-request idle timeout in seconds")
    klass.add_argument("--seed", type=int, default=20260929, help="random seed")
    klass.add_argument("--out-dir", help="output directory (default: bench-results/classroom-<UTC timestamp>)")
    klass.add_argument("--profile", help="profile label stored in the results")
    klass.add_argument("--allow-busy", action="store_true", help="run even if the server is serving requests")
    klass.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    klass.set_defaults(func=cmd_classroom)

    keys = sub.add_parser("gen-keys", help="generate per-user API keys")
    keys.add_argument("--out-dir", required=True, help="directory for api-keys.map and api-keys.csv")
    keys.add_argument("--count", type=positive_int, help="number of users with numbered ids")
    keys.add_argument("--prefix", default="user", help="user id prefix (default: %(default)s)")
    keys.add_argument("--users-file", help="file with one user id per line (overrides --count/--prefix)")
    keys.set_defaults(func=cmd_gen_keys)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return EXIT_USAGE
    try:
        return args.func(args)
    except ValueError as exc:
        eprint("error: {}".format(exc))
        return EXIT_USAGE
    except KeyboardInterrupt:
        eprint("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
