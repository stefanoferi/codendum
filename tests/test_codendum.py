# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Stefano Noferi
"""Unit tests for scripts/lib/codendum.py (standard library only)."""

import importlib.util
import json
import os
import random
import unittest

_PATH = os.path.join(os.path.dirname(__file__), "..", "scripts", "lib", "codendum.py")
_SPEC = importlib.util.spec_from_file_location("codendum", _PATH)
codendum = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(codendum)

METRICS = """
# HELP vllm:kv_cache_usage_perc KV-cache usage. 1 means 100 percent usage.
# TYPE vllm:kv_cache_usage_perc gauge
vllm:kv_cache_usage_perc{engine="0",model_name="coder"} 0.25
vllm:num_requests_running{engine="0",model_name="coder"} 7.0
vllm:num_requests_waiting{engine="0",model_name="coder"} 3.0
vllm:num_preemptions_total{engine="0",model_name="coder"} 2.0
vllm:prefix_cache_queries_total{engine="0",model_name="coder"} 1000.0
vllm:prefix_cache_hits_total{engine="0",model_name="coder"} 250.0
vllm:request_success_total{engine="0",finished_reason="stop",model_name="coder"} 10.0
vllm:request_success_total{engine="0",finished_reason="length",model_name="coder"} 5.0
vllm:time_to_first_token_seconds_sum{engine="0",model_name="coder"} 3.0
vllm:time_to_first_token_seconds_count{engine="0",model_name="coder"} 15.0
vllm:cache_config_info{block_size="16",cache_dtype="fp8",engine="0",num_gpu_blocks="1000",note="a \\"quoted\\" value"} 1.0
"""


class PrometheusTests(unittest.TestCase):
    def test_parse_labels_and_values(self):
        samples = codendum.parse_prometheus(METRICS)
        names = [s[0] for s in samples]
        self.assertIn("vllm:cache_config_info", names)
        info = [s for s in samples if s[0] == "vllm:cache_config_info"][0]
        self.assertEqual(info[1]["num_gpu_blocks"], "1000")
        self.assertEqual(info[2], 1.0)

    def test_summary(self):
        summary = codendum.summarize_metrics(codendum.parse_prometheus(METRICS))
        self.assertEqual(summary["kv_cache_usage_percent"], 25.0)
        self.assertEqual(summary["requests_running"], 7.0)
        self.assertEqual(summary["requests_waiting"], 3.0)
        self.assertEqual(summary["preemptions_total"], 2.0)
        self.assertEqual(summary["prefix_cache_hit_rate_percent"], 25.0)
        self.assertEqual(summary["kv_cache_capacity_tokens"], 16000)
        self.assertEqual(summary["requests_finished"], {"stop": 10.0, "length": 5.0})
        self.assertEqual(summary["ttft_mean_seconds"], 0.2)

    def test_legacy_metric_names(self):
        text = 'vllm:gpu_cache_usage_perc{model_name="coder"} 0.5\nvllm:num_preemptions{model_name="coder"} 4\n'
        summary = codendum.summarize_metrics(codendum.parse_prometheus(text))
        self.assertEqual(summary["kv_cache_usage_percent"], 50.0)
        self.assertEqual(summary["preemptions_total"], 4.0)

    def test_missing_metrics_are_none(self):
        summary = codendum.summarize_metrics([])
        self.assertIsNone(summary["kv_cache_usage_percent"])
        self.assertIsNone(summary["prefix_cache_hit_rate_percent"])


class PercentileTests(unittest.TestCase):
    def test_interpolation(self):
        values = [1.0, 2.0, 3.0, 4.0]
        self.assertEqual(codendum.percentile(values, 50), 2.5)
        self.assertEqual(codendum.percentile(values, 0), 1.0)
        self.assertEqual(codendum.percentile(values, 100), 4.0)
        self.assertAlmostEqual(codendum.percentile(values, 95), 3.85)

    def test_empty_and_single(self):
        self.assertIsNone(codendum.percentile([], 50))
        self.assertEqual(codendum.percentile([7.0], 99), 7.0)


class ToolCallTests(unittest.TestCase):
    def _message(self, name="read_file", arguments='{"path": "a.java"}'):
        return {"tool_calls": [{"id": "x", "type": "function", "function": {"name": name, "arguments": arguments}}]}

    def test_valid_call(self):
        ok, detail, call = codendum._check_tool_call(self._message())
        self.assertTrue(ok, detail)
        self.assertEqual(call["id"], "x")

    def test_no_call(self):
        ok, _, call = codendum._check_tool_call({"content": "hi"})
        self.assertFalse(ok)
        self.assertIsNone(call)

    def test_bad_arguments(self):
        self.assertFalse(codendum._check_tool_call(self._message(arguments="{not json"))[0])
        self.assertFalse(codendum._check_tool_call(self._message(arguments='{"file": "a"}'))[0])
        self.assertFalse(codendum._check_tool_call(self._message(name="write_file"))[0])

    def test_raw_markup_detection(self):
        self.assertTrue(codendum.RAW_TOOL_MARKUP.search("<tool_call>\n<function=read_file>"))
        self.assertFalse(codendum.RAW_TOOL_MARKUP.search("The function reads a file."))


class PromptTests(unittest.TestCase):
    def test_prompt_size_and_uniqueness(self):
        rng = random.Random(1)
        first = codendum.make_prompt(1000, rng, "a")
        second = codendum.make_prompt(1000, rng, "b")
        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("[a] "))
        self.assertGreater(len(first.split()), 900)
        self.assertLess(len(first.split()), 1100)


class ArgumentTests(unittest.TestCase):
    def test_int_list(self):
        self.assertEqual(codendum.parse_int_list("1,8,16"), [1, 8, 16])
        with self.assertRaises(Exception):
            codendum.parse_int_list("1,x")
        with self.assertRaises(Exception):
            codendum.parse_int_list("0")

    def test_shapes(self):
        self.assertEqual(codendum.parse_shapes("long"), ["long"])
        with self.assertRaises(Exception):
            codendum.parse_shapes("huge")

    def test_client_rejects_bad_url(self):
        with self.assertRaises(ValueError):
            codendum.Client("ftp://example")


class OpenCodeConfigTests(unittest.TestCase):
    """Structural checks of the OpenCode examples against the serving profiles."""

    root = os.path.join(os.path.dirname(__file__), "..")

    def _profile(self, name):
        values = {}
        with open(os.path.join(self.root, "config", "profiles", name + ".env"), encoding="utf-8") as handle:
            for line in handle:
                if "=" in line and not line.startswith("#"):
                    key, value = line.strip().split("=", 1)
                    values[key] = value
        return values

    def test_v2_example(self):
        with open(os.path.join(self.root, "config", "opencode.example.json"), encoding="utf-8") as handle:
            config = json.load(handle)
        provider = config["providers"]["codendum"]
        model = provider["models"]["coder"]
        profile = self._profile("classroom-64k")
        self.assertEqual(config["model"], "codendum/coder")
        self.assertEqual(provider["package"], "@opencode/ai/providers/openai-compatible")
        self.assertTrue(provider["settings"]["baseURL"].startswith("https://"))
        self.assertTrue(provider["settings"]["baseURL"].endswith("/v1"))
        self.assertEqual(provider["settings"]["apiKey"], "{env:CODENDUM_API_KEY}")
        self.assertEqual(model["modelID"], "coder")
        self.assertEqual(model["capabilities"], {"tools": True, "input": ["text"], "output": ["text"]})
        self.assertEqual(model["limit"]["context"], int(profile["CODENDUM_CLIENT_CONTEXT_LIMIT"]))
        self.assertEqual(model["limit"]["output"], int(profile["CODENDUM_CLIENT_OUTPUT_LIMIT"]))
        self.assertLessEqual(model["limit"]["context"], int(profile["CODENDUM_MAX_MODEL_LEN"]))

    def test_v1_example_matches_v2(self):
        with open(os.path.join(self.root, "config", "opencode.v1.example.json"), encoding="utf-8") as handle:
            v1 = json.load(handle)
        with open(os.path.join(self.root, "config", "opencode.example.json"), encoding="utf-8") as handle:
            v2 = json.load(handle)
        p1, p2 = v1["provider"]["codendum"], v2["providers"]["codendum"]
        self.assertEqual(p1["options"]["baseURL"], p2["settings"]["baseURL"])
        self.assertEqual(p1["options"]["apiKey"], p2["settings"]["apiKey"])
        self.assertTrue(p1["models"]["coder"]["tool_call"])
        self.assertEqual(p1["models"]["coder"]["limit"], p2["models"]["coder"]["limit"])


class OpenCodeGeneratorTests(unittest.TestCase):
    """The generated provider must match the documented examples."""

    root = os.path.join(os.path.dirname(__file__), "..")

    def _example(self, name):
        with open(os.path.join(self.root, "config", name), encoding="utf-8") as handle:
            return json.load(handle)

    def test_v2_matches_example(self):
        example = self._example("opencode.example.json")
        provider = codendum.opencode_provider("v2", "https://llm.lab.example:8443/v1", "coder", 65536, 8192)
        expected = example["providers"]["codendum"]
        self.assertEqual(provider["package"], expected["package"])
        self.assertEqual(provider["settings"], expected["settings"])
        self.assertEqual(provider["models"]["coder"]["capabilities"], expected["models"]["coder"]["capabilities"])
        self.assertEqual(provider["models"]["coder"]["limit"], expected["models"]["coder"]["limit"])

    def test_v1_matches_example(self):
        example = self._example("opencode.v1.example.json")
        provider = codendum.opencode_provider("v1", "https://llm.lab.example:8443/v1", "coder", 65536, 8192)
        expected = example["provider"]["codendum"]
        self.assertEqual(provider["npm"], expected["npm"])
        self.assertEqual(provider["options"], expected["options"])
        self.assertTrue(provider["models"]["coder"]["tool_call"])

    def test_merge_keeps_other_settings(self):
        existing = {"theme": "dark", "providers": {"other": {"name": "x"}}, "model": "other/m"}
        merged = codendum.merge_opencode_config(
            existing, "v2", codendum.opencode_provider("v2", "https://h/v1", "coder", 1024, 256), "coder", True)
        self.assertEqual(merged["theme"], "dark")
        self.assertIn("other", merged["providers"])
        self.assertEqual(merged["model"], "codendum/coder")
        self.assertEqual(existing["model"], "other/m")

    def test_merge_refuses_mixed_formats(self):
        existing = {"provider": {"codendum": {}}}
        with self.assertRaises(ValueError):
            codendum.merge_opencode_config(existing, "v2", {}, "coder", True)


if __name__ == "__main__":
    unittest.main()
