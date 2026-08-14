#!/usr/bin/env python3
"""Harness-owned qualification gate for Nemotron Diffusion VLM BF16 speed work."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import statistics
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any

SCHEMA = "nemotron-bf16-linear-spec-qualification-v1"
BENCHMARK_ID = "nemotron-vlm-bf16-linear-spec-v1"
MODEL_ID = "nvidia/Nemotron-Labs-Diffusion-VLM-8B"
DRAFT_ADAPTER_ID = "nvidia/Nemotron-Labs-Diffusion-8B"
DRAFT_ADAPTER_SHA256 = "5893f4aec2f43bd0ea065776a269f5f7b70c52db52be3270686349a78e824908"
MODE = "linear_speculative"
DTYPE = "bfloat16"
CHIP = "Apple M3 Ultra"
IMAGE_RELATIVE_PATH = "mlx-vlm/examples/images/cats.jpg"
IMAGE_SHA256 = "dea9e7ef97386345f7cff32f9055da4982da5471c48d575146c796ab4563b04e"
PROMPT = (
    "Describe every visible cat in this image in exhaustive detail, including anatomy, pose, expression, coat, "
    "lighting, setting, spatial relationships, and composition. Continue with a careful visual inventory and "
    "grounded comparisons until you have produced at least 512 tokens."
)
PROMPT_SHA256 = "70c83a08c3d4d5ec205a8d8b741c66db71614f647f3281f10ae4d4a18432b945"
OUTPUT_TOKENS = 512
BLOCK_LENGTH = 32

MONITORED_PATHS = (
    "mlx-vlm/mlx_vlm/models/nemotron_labs_diffusion",
    "mlx-vlm/mlx_vlm/models/nemotron_labs_diffusion_vlm",
    "mlx-vlm/mlx_vlm/generate/diffusion.py",
    "mlx-vlm/benchmarks/nemotron_diffusion_vlm",
    "diagnostics/nemotron-diffusion-vlm",
)

# Existing generic compatibility is not retroactively outlawed. These patterns
# reject newly added acceleration paths that actually construct or invoke a
# low-bit representation. Runtime qualification independently requires zero
# low-bit tensors, modules, caches, and adapters.
FORBIDDEN_ADDITION_PATTERNS = (
    ("MLX weight quantization", re.compile(r"\bmx\.quantize\s*\(")),
    ("model quantization", re.compile(r"\bquantize_model\s*\(")),
    ("quantized module construction", re.compile(r"\b(?:nn\.)?QuantizedLinear\s*\(")),
    ("quantized cache construction", re.compile(r"\b(?:Batch)?QuantizedKVCache\s*\(")),
    ("cache conversion", re.compile(r"\.to_quantized\s*\(")),
    ("KV quantization helper", re.compile(r"\bmaybe_quantize_kv_cache\s*\(")),
    ("one-bit matmul", re.compile(r"\bone_bit_quantized_matmul\s*\(")),
    ("TurboQuant", re.compile(r"\bturboquant\b", re.IGNORECASE)),
    ("quantized attention", re.compile(r"\bquantized_scaled_dot_product_attention\s*\(")),
    ("quantization CLI flag", re.compile(r"--(?:kv-bits|quantize|quantization)\b")),
    ("low-bit KV setting", re.compile(r"(?:\bkv_bits\s*=|[\"']kv_bits[\"']\s*:)\s*[1-9]")),
    ("low-bit format", re.compile(r"\b(?:nvfp4|mxfp4|int4|w4a\d+|fp8)\b", re.IGNORECASE)),
)


class GateFailure(Exception):
    pass


def fail(message: str) -> None:
    raise GateFailure(message)


def require(condition: bool, message: str) -> None:
    if not condition:
        fail(message)


def require_mapping(value: Any, name: str) -> dict[str, Any]:
    require(isinstance(value, dict), f"{name} must be an object")
    return value


def require_number(value: Any, name: str, *, minimum: float | None = None) -> float:
    require(isinstance(value, (int, float)) and not isinstance(value, bool), f"{name} must be numeric")
    number = float(value)
    require(math.isfinite(number), f"{name} must be finite")
    if minimum is not None:
        require(number >= minimum, f"{name} must be >= {minimum}")
    return number


def require_integer(value: Any, name: str, *, minimum: int | None = None) -> int:
    require(isinstance(value, int) and not isinstance(value, bool), f"{name} must be an integer")
    if minimum is not None:
        require(value >= minimum, f"{name} must be >= {minimum}")
    return value


def require_sha256(value: Any, name: str) -> str:
    require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None, f"{name} must be a lowercase SHA-256")
    return value


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_output(workspace: Path, args: list[str]) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=workspace,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        fail(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def scan_text(source: str, origin: str) -> list[str]:
    violations = []
    for line_number, line in enumerate(source.splitlines(), start=1):
        for label, pattern in FORBIDDEN_ADDITION_PATTERNS:
            if pattern.search(line):
                violations.append(f"{origin}:{line_number}: {label}: {line.strip()}")
    return violations


def added_source_lines(workspace: Path) -> str:
    diff = git_output(
        workspace,
        ["diff", "--no-ext-diff", "--unified=0", "HEAD", "--", *MONITORED_PATHS],
    )
    return "\n".join(
        line[1:]
        for line in diff.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )


def source_gate(workspace: Path) -> None:
    workspace = workspace.resolve()
    require((workspace / ".git").exists(), f"workspace is not a Git root: {workspace}")
    require((workspace / MONITORED_PATHS[0]).is_dir(), "Nemotron MLX runtime directory is missing")

    violations = scan_text(added_source_lines(workspace), "tracked additions")
    untracked = git_output(
        workspace,
        ["ls-files", "--others", "--exclude-standard", "--", *MONITORED_PATHS],
    )
    for relative in untracked.splitlines():
        path = workspace / relative
        if path.suffix not in {".py", ".metal", ".json", ".toml", ".yaml", ".yml"} or not path.is_file():
            continue
        violations.extend(scan_text(path.read_text(encoding="utf-8", errors="replace"), relative))

    require(not violations, "new low-bit execution path detected:\n" + "\n".join(violations))
    print("PASS: no newly added low-bit execution path in the Nemotron campaign scope")


def load_artifact(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"cannot read qualification artifact {path}: {exc}")
    return require_mapping(payload, "artifact")


def safetensors_header(path: Path) -> tuple[dict[str, Any], int]:
    try:
        with path.open("rb") as handle:
            prefix = handle.read(8)
            require(len(prefix) == 8, f"invalid safetensors header: {path}")
            header_size = struct.unpack("<Q", prefix)[0]
            require(0 < header_size <= 100 * 1024 * 1024, f"implausible safetensors header size: {path}")
            raw_header = handle.read(header_size)
            require(len(raw_header) == header_size, f"truncated safetensors header: {path}")
        header = json.loads(raw_header)
    except (OSError, json.JSONDecodeError, struct.error) as exc:
        fail(f"cannot inspect safetensors file {path}: {exc}")
    return require_mapping(header, f"safetensors header {path}"), header_size


def inspect_safetensors(path: Path) -> tuple[int, set[str]]:
    header, header_size = safetensors_header(path)
    tensor_count = 0
    dtypes: set[str] = set()
    data_size = path.stat().st_size - 8 - header_size
    for name, entry in header.items():
        if name == "__metadata__":
            continue
        tensor = require_mapping(entry, f"{path}:{name}")
        dtype = tensor.get("dtype")
        offsets = tensor.get("data_offsets")
        require(isinstance(dtype, str), f"{path}:{name}.dtype is required")
        require(
            isinstance(offsets, list)
            and len(offsets) == 2
            and all(isinstance(value, int) for value in offsets)
            and 0 <= offsets[0] <= offsets[1] <= data_size,
            f"{path}:{name}.data_offsets are invalid",
        )
        tensor_count += 1
        dtypes.add(dtype)
    require(tensor_count > 0, f"safetensors file contains no tensors: {path}")
    return tensor_count, dtypes


def find_config_low_bit_value(value: Any, path: str = "config") -> str | None:
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if "quant" in str(key).lower() and child not in (None, False, {}, []):
                return child_path
            found = find_config_low_bit_value(child, child_path)
            if found:
                return found
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found = find_config_low_bit_value(child, f"{path}[{index}]")
            if found:
                return found
    elif isinstance(value, str) and re.search(r"\b(?:int4|int8|fp8|nvfp4|mxfp4|one[-_ ]?bit)\b", value, re.IGNORECASE):
        return path
    return None


def checkpoint_gate(artifact_path: Path, expected_adapter_sha256: str = DRAFT_ADAPTER_SHA256) -> None:
    root = load_artifact(artifact_path)
    require(root.get("schema") == SCHEMA, f"schema must be {SCHEMA}")
    model = require_mapping(root.get("model"), "model")
    require(model.get("id") == MODEL_ID, f"model.id must be {MODEL_ID}")
    revision = model.get("revision")
    require(isinstance(revision, str) and re.fullmatch(r"[0-9a-f]{40}", revision) is not None, "model.revision must be a full lowercase Git commit")
    local_path_value = model.get("local_path")
    require(isinstance(local_path_value, str) and bool(local_path_value), "model.local_path is required")
    local_path = Path(local_path_value).expanduser().resolve()
    require(local_path.is_dir(), f"model.local_path does not exist: {local_path}")
    require(local_path.name == revision, "model.local_path must be the Hugging Face snapshot directory for model.revision")

    config_path = local_path / "config.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"cannot read checkpoint config {config_path}: {exc}")
    require(config.get("model_type") == "nemotron_labs_diffusion_vlm", "checkpoint config is not the Nemotron Diffusion VLM")
    low_bit_config = find_config_low_bit_value(config)
    require(low_bit_config is None, f"checkpoint config enables a low-bit path at {low_bit_config}")

    forbidden_files = [
        path
        for path in local_path.rglob("*")
        if path.is_file()
        and (
            path.suffix.lower() in {".gguf", ".bin", ".pt", ".pth", ".ckpt"}
            or re.search(r"\b(?:4bit|8bit|int4|int8|fp8|nvfp4|mxfp4|quantized)\b", path.name, re.IGNORECASE)
        )
    ]
    require(not forbidden_files, f"checkpoint contains disallowed weight files: {[str(path) for path in forbidden_files]}")

    weight_files = sorted(local_path.glob("*.safetensors"))
    require(weight_files, "checkpoint has no top-level safetensors weights")
    total_tensors = 0
    observed_dtypes: set[str] = set()
    for path in weight_files:
        count, dtypes = inspect_safetensors(path)
        total_tensors += count
        observed_dtypes.update(dtypes)
    require(observed_dtypes == {"BF16"}, f"checkpoint tensor dtypes must be only BF16, observed {sorted(observed_dtypes)}")

    draft_adapter = require_mapping(model.get("draft_adapter"), "model.draft_adapter")
    require(draft_adapter.get("id") == DRAFT_ADAPTER_ID, f"model.draft_adapter.id must be {DRAFT_ADAPTER_ID}")
    adapter_revision = draft_adapter.get("revision")
    require(
        isinstance(adapter_revision, str) and re.fullmatch(r"[0-9a-f]{40}", adapter_revision) is not None,
        "model.draft_adapter.revision must be a full lowercase Git commit",
    )
    adapter_path_value = draft_adapter.get("local_path")
    require(isinstance(adapter_path_value, str) and bool(adapter_path_value), "model.draft_adapter.local_path is required")
    adapter_path = Path(adapter_path_value).expanduser().resolve()
    require(adapter_path.name == "adapter_model.safetensors" and adapter_path.is_file(), "draft adapter path must name adapter_model.safetensors")
    require(adapter_revision in adapter_path.parts, "draft adapter path must come from its declared Hugging Face snapshot revision")
    require(file_sha256(adapter_path) == expected_adapter_sha256, "draft adapter SHA-256 does not match NVIDIA's official adapter")

    adapter_config_path = adapter_path.with_name("adapter_config.json")
    try:
        adapter_config = json.loads(adapter_config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"cannot read draft adapter config {adapter_config_path}: {exc}")
    require(adapter_config.get("base_model_name_or_path") == DRAFT_ADAPTER_ID, "draft adapter config names the wrong base model")
    require(adapter_config.get("r") == 128 and adapter_config.get("lora_alpha") == 512, "draft adapter rank/alpha do not match NVIDIA's linear-spec adapter")
    require(adapter_config.get("target_modules") == ["o_proj"], "draft adapter must target only o_proj")

    adapter_tensors, adapter_dtypes = inspect_safetensors(adapter_path)
    require(adapter_dtypes <= {"BF16", "F32"}, f"draft LoRA source tensor dtypes are invalid: {sorted(adapter_dtypes)}")

    print(
        f"PASS: checkpoint {revision} has {total_tensors} BF16 model tensors and "
        f"the official {adapter_tensors}-tensor draft LoRA; qualification still requires BF16 adapter runtime tensors"
    )


def validate_runtime_audit(data: dict[str, Any]) -> None:
    for field in ("quantized_module_count", "quantized_tensor_count", "low_bit_cache_count"):
        require_integer(data.get(field), f"runtime_audit.{field}", minimum=0)
        require(data[field] == 0, f"runtime_audit.{field} must be zero")
    require_integer(data.get("linear_module_count"), "runtime_audit.linear_module_count", minimum=1)
    require(data.get("default_compute_dtype") == DTYPE, f"runtime_audit.default_compute_dtype must be {DTYPE}")

    for field in ("weight_dtype_counts", "kv_cache_dtype_counts", "draft_lora_dtype_counts"):
        counts = require_mapping(data.get(field), f"runtime_audit.{field}")
        require_integer(counts.get(DTYPE), f"runtime_audit.{field}.{DTYPE}", minimum=1)
        forbidden = []
        for name, count in counts.items():
            count = require_integer(count, f"runtime_audit.{field}.{name}", minimum=0)
            if name not in {"bfloat16", "float32"} and count > 0:
                forbidden.append(name)
        require(not forbidden, f"runtime_audit.{field} contains disallowed dtypes: {forbidden}")


def validate_result(path: Path, minimum_tps: float, minimum_runs: int) -> None:
    root = load_artifact(path)
    require(root.get("schema") == SCHEMA, f"schema must be {SCHEMA}")
    require(root.get("benchmark_id") == BENCHMARK_ID, f"benchmark_id must be {BENCHMARK_ID}")

    model = require_mapping(root.get("model"), "model")
    require(model.get("id") == MODEL_ID, f"model.id must be {MODEL_ID}")
    require(isinstance(model.get("local_path"), str) and bool(model["local_path"]), "model.local_path is required")
    require(
        isinstance(model.get("revision"), str) and re.fullmatch(r"[0-9a-f]{40}", model["revision"]) is not None,
        "model.revision must be a full lowercase Git commit",
    )
    for field in ("weight_dtype", "activation_dtype", "kv_cache_dtype", "draft_lora_dtype"):
        require(model.get(field) == DTYPE, f"model.{field} must be {DTYPE}")
    require(model.get("quantized") is False, "model.quantized must be false")
    require(model.get("adapter_quantized") is False, "model.adapter_quantized must be false")
    draft_adapter = require_mapping(model.get("draft_adapter"), "model.draft_adapter")
    require(draft_adapter.get("id") == DRAFT_ADAPTER_ID, f"model.draft_adapter.id must be {DRAFT_ADAPTER_ID}")
    require(
        isinstance(draft_adapter.get("revision"), str)
        and re.fullmatch(r"[0-9a-f]{40}", draft_adapter["revision"]) is not None,
        "model.draft_adapter.revision must be a full lowercase Git commit",
    )
    require(isinstance(draft_adapter.get("local_path"), str) and bool(draft_adapter["local_path"]), "model.draft_adapter.local_path is required")

    hardware = require_mapping(root.get("hardware"), "hardware")
    require(hardware.get("chip") == CHIP, f"hardware.chip must be {CHIP}")
    require_number(hardware.get("unified_memory_gib"), "hardware.unified_memory_gib", minimum=500.0)

    workload = require_mapping(root.get("workload"), "workload")
    require(workload.get("mode") == MODE, f"workload.mode must be {MODE}")
    require(workload.get("image_path") == IMAGE_RELATIVE_PATH, f"workload.image_path must be {IMAGE_RELATIVE_PATH}")
    require(workload.get("image_sha256") == IMAGE_SHA256, "workload.image_sha256 does not match the fixed image")
    require(workload.get("image_width") == 640, "workload.image_width must be 640")
    require(workload.get("image_height") == 480, "workload.image_height must be 480")
    require(workload.get("prompt") == PROMPT, "workload.prompt does not match the fixed prompt")
    require(workload.get("prompt_sha256") == PROMPT_SHA256, "workload.prompt_sha256 does not match the fixed prompt")
    require_sha256(workload.get("prepared_input_ids_sha256"), "workload.prepared_input_ids_sha256")
    require_sha256(workload.get("prepared_pixel_values_sha256"), "workload.prepared_pixel_values_sha256")
    require_integer(workload.get("vision_token_count"), "workload.vision_token_count", minimum=1)
    require_integer(workload.get("max_new_tokens"), "workload.max_new_tokens", minimum=OUTPUT_TOKENS)
    require(workload["max_new_tokens"] == OUTPUT_TOKENS, f"workload.max_new_tokens must be exactly {OUTPUT_TOKENS}")
    require(workload.get("block_length") == BLOCK_LENGTH, f"workload.block_length must be {BLOCK_LENGTH}")
    require(workload.get("batch_size") == 1, "workload.batch_size must be 1")
    require_number(workload.get("temperature"), "workload.temperature", minimum=0.0)
    require(workload["temperature"] == 0.0, "workload.temperature must be 0.0")
    require(workload.get("counted_token_kind") == "accepted_emitted_output", "only accepted emitted output tokens may be counted")
    require(workload.get("decode_timer_excludes_prefill") is True, "decode timer must exclude prefill")
    require(workload.get("decode_timer_excludes_vision_encode") is True, "decode timer must exclude vision encode")
    require(workload.get("fresh_kv_cache_each_run") is True, "each run must use a fresh KV cache")
    require(workload.get("synchronize_timer_boundaries") is True, "timer boundaries must synchronize MLX")
    require_integer(workload.get("warmup_runs"), "workload.warmup_runs", minimum=2)

    validate_runtime_audit(require_mapping(root.get("runtime_audit"), "runtime_audit"))

    correctness = require_mapping(root.get("correctness"), "correctness")
    require(correctness.get("reference_mode") == "ar", "correctness.reference_mode must be ar")
    require(correctness.get("greedy_token_match") is True, "linear self-spec output must match greedy AR")
    require(correctness.get("mismatched_token_count") == 0, "correctness.mismatched_token_count must be zero")
    require(correctness.get("vision_ablation_changes_output") is True, "vision ablation must change the output")
    require(correctness.get("image_conditioned_output_nonempty") is True, "image-conditioned output must be nonempty")

    runs = root.get("runs")
    require(isinstance(runs, list), "runs must be an array")
    require(len(runs) >= minimum_runs, f"runs must contain at least {minimum_runs} measured runs")
    measured_tps = []
    for index, item in enumerate(runs, start=1):
        run = require_mapping(item, f"runs[{index}]")
        tokens = require_integer(run.get("accepted_output_tokens"), f"runs[{index}].accepted_output_tokens", minimum=OUTPUT_TOKENS)
        require(tokens == OUTPUT_TOKENS, f"runs[{index}] must emit exactly {OUTPUT_TOKENS} accepted tokens")
        require(run.get("finish_reason") == "length", f"runs[{index}].finish_reason must be length")
        require(run.get("synchronized") is True, f"runs[{index}] must be synchronized")
        seconds = require_number(run.get("decode_seconds"), f"runs[{index}].decode_seconds", minimum=1e-9)
        emitted_tps = require_number(run.get("emitted_tps"), f"runs[{index}].emitted_tps", minimum=0.0)
        calculated = tokens / seconds
        require(math.isclose(emitted_tps, calculated, rel_tol=0.002, abs_tol=0.05), f"runs[{index}].emitted_tps does not equal accepted tokens / decode seconds")
        require_number(run.get("vision_encode_seconds"), f"runs[{index}].vision_encode_seconds", minimum=0.0)
        require_number(run.get("prefill_seconds"), f"runs[{index}].prefill_seconds", minimum=0.0)
        measured_tps.append(calculated)

    median_tps = statistics.median(measured_tps)
    summary = require_mapping(root.get("summary"), "summary")
    reported_median = require_number(summary.get("median_emitted_tps"), "summary.median_emitted_tps", minimum=0.0)
    require(math.isclose(reported_median, median_tps, rel_tol=0.002, abs_tol=0.05), "summary median does not match measured runs")
    require(median_tps >= minimum_tps, f"median emitted throughput {median_tps:.3f} tok/s is below {minimum_tps:.3f} tok/s")

    provenance = require_mapping(root.get("provenance"), "provenance")
    require_sha256(provenance.get("source_tree_sha256"), "provenance.source_tree_sha256")
    require_sha256(provenance.get("benchmark_runner_sha256"), "provenance.benchmark_runner_sha256")
    require(isinstance(provenance.get("mlx_version"), str) and bool(provenance["mlx_version"]), "provenance.mlx_version is required")

    print(f"PASS: {path} qualifies at median {median_tps:.3f} accepted emitted tok/s ({len(runs)} runs, BF16, no low-bit runtime state)")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    source = subparsers.add_parser("source", help="reject newly added low-bit execution paths")
    source.add_argument("--workspace", type=Path, default=Path.cwd())
    result = subparsers.add_parser("result", help="validate a benchmark qualification artifact")
    result.add_argument("artifact", type=Path)
    result.add_argument("--min-tps", type=float, required=True)
    result.add_argument("--min-runs", type=int, default=3)
    checkpoint = subparsers.add_parser("checkpoint", help="inspect on-disk checkpoint and draft adapter dtypes")
    checkpoint.add_argument("artifact", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "source":
            source_gate(args.workspace)
        elif args.command == "checkpoint":
            checkpoint_gate(args.artifact)
        else:
            require(args.min_tps >= 0.0, "--min-tps must be non-negative")
            require(args.min_runs >= 1, "--min-runs must be positive")
            validate_result(args.artifact, args.min_tps, args.min_runs)
    except GateFailure as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
