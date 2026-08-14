from __future__ import annotations

import importlib.util
import hashlib
import json
import struct
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "nemotron_bf16_speed_gate.py"
SPEC = importlib.util.spec_from_file_location("nemotron_bf16_speed_gate", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


def valid_payload(tps: float = 400.0, runs: int = 7) -> dict:
    seconds = gate.OUTPUT_TOKENS / tps
    return {
        "schema": gate.SCHEMA,
        "benchmark_id": gate.BENCHMARK_ID,
        "model": {
            "id": gate.MODEL_ID,
            "local_path": "/tmp/not-loaded-by-result-validator",
            "revision": "c6706ba71b824c2c61a94a804da8255c1eb5d30d",
            "weight_dtype": gate.DTYPE,
            "activation_dtype": gate.DTYPE,
            "kv_cache_dtype": gate.DTYPE,
            "draft_lora_dtype": gate.DTYPE,
            "quantized": False,
            "adapter_quantized": False,
            "draft_adapter": {
                "id": gate.DRAFT_ADAPTER_ID,
                "revision": "36e52f3c9ac70f3c5e78a0305520f885a393e3b0",
                "local_path": "/tmp/not-loaded-by-result-validator/linear_spec_lora/adapter_model.safetensors",
            },
        },
        "hardware": {"chip": gate.CHIP, "unified_memory_gib": 512.0},
        "workload": {
            "mode": gate.MODE,
            "image_path": gate.IMAGE_RELATIVE_PATH,
            "image_sha256": gate.IMAGE_SHA256,
            "image_width": 640,
            "image_height": 480,
            "prompt": gate.PROMPT,
            "prompt_sha256": gate.PROMPT_SHA256,
            "prepared_input_ids_sha256": "1" * 64,
            "prepared_pixel_values_sha256": "2" * 64,
            "vision_token_count": 768,
            "max_new_tokens": gate.OUTPUT_TOKENS,
            "block_length": gate.BLOCK_LENGTH,
            "batch_size": 1,
            "temperature": 0.0,
            "counted_token_kind": "accepted_emitted_output",
            "decode_timer_excludes_prefill": True,
            "decode_timer_excludes_vision_encode": True,
            "fresh_kv_cache_each_run": True,
            "synchronize_timer_boundaries": True,
            "warmup_runs": 2,
        },
        "runtime_audit": {
            "quantized_module_count": 0,
            "quantized_tensor_count": 0,
            "low_bit_cache_count": 0,
            "linear_module_count": 274,
            "default_compute_dtype": gate.DTYPE,
            "weight_dtype_counts": {"bfloat16": 612, "float32": 68},
            "kv_cache_dtype_counts": {"bfloat16": 68},
            "draft_lora_dtype_counts": {"bfloat16": 68},
        },
        "correctness": {
            "reference_mode": "ar",
            "greedy_token_match": True,
            "mismatched_token_count": 0,
            "vision_ablation_changes_output": True,
            "image_conditioned_output_nonempty": True,
        },
        "runs": [
            {
                "accepted_output_tokens": gate.OUTPUT_TOKENS,
                "finish_reason": "length",
                "synchronized": True,
                "decode_seconds": seconds,
                "emitted_tps": tps,
                "vision_encode_seconds": 0.04,
                "prefill_seconds": 0.08,
            }
            for _ in range(runs)
        ],
        "summary": {"median_emitted_tps": tps},
        "provenance": {
            "source_tree_sha256": "3" * 64,
            "benchmark_runner_sha256": "4" * 64,
            "mlx_version": "0.32.0",
        },
    }


def write_payload(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "result.json"
    path.write_text(json.dumps(payload))
    return path


def write_safetensors(path: Path, dtype: str = "BF16") -> None:
    header = {"weight": {"dtype": dtype, "shape": [1], "data_offsets": [0, 2]}}
    encoded = json.dumps(header, separators=(",", ":")).encode()
    padded = encoded + b" " * ((8 - len(encoded) % 8) % 8)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(struct.pack("<Q", len(padded)) + padded + b"\x00\x00")


def checkpoint_artifact(tmp_path: Path, *, dtype: str = "BF16", config_extra: dict | None = None) -> Path:
    revision = "c6706ba71b824c2c61a94a804da8255c1eb5d30d"
    snapshot = tmp_path / revision
    snapshot.mkdir()
    config = {"model_type": "nemotron_labs_diffusion_vlm", **(config_extra or {})}
    (snapshot / "config.json").write_text(json.dumps(config))
    write_safetensors(snapshot / "model.safetensors", dtype)
    adapter_revision = "36e52f3c9ac70f3c5e78a0305520f885a393e3b0"
    adapter_path = tmp_path / adapter_revision / "linear_spec_lora" / "adapter_model.safetensors"
    write_safetensors(adapter_path, dtype)
    (adapter_path.parent / "adapter_config.json").write_text(
        json.dumps(
            {
                "base_model_name_or_path": gate.DRAFT_ADAPTER_ID,
                "r": 128,
                "lora_alpha": 512,
                "target_modules": ["o_proj"],
            }
        )
    )
    payload = valid_payload()
    payload["model"]["local_path"] = str(snapshot)
    payload["model"]["draft_adapter"]["revision"] = adapter_revision
    payload["model"]["draft_adapter"]["local_path"] = str(adapter_path)
    return write_payload(tmp_path, payload)


def test_valid_result_passes(tmp_path):
    gate.validate_result(write_payload(tmp_path, valid_payload()), 350.0, 7)


def test_rejects_quantized_runtime(tmp_path):
    payload = valid_payload()
    payload["runtime_audit"]["quantized_module_count"] = 1
    with pytest.raises(gate.GateFailure, match="must be zero"):
        gate.validate_result(write_payload(tmp_path, payload), 350.0, 7)


def test_rejects_wrong_model_even_if_fast(tmp_path):
    payload = valid_payload(900.0)
    payload["model"]["id"] = "cheap/4bit-substitute"
    with pytest.raises(gate.GateFailure, match="model.id"):
        gate.validate_result(write_payload(tmp_path, payload), 350.0, 7)


def test_rejects_draft_token_accounting(tmp_path):
    payload = valid_payload()
    payload["workload"]["counted_token_kind"] = "draft_work"
    with pytest.raises(gate.GateFailure, match="accepted emitted"):
        gate.validate_result(write_payload(tmp_path, payload), 350.0, 7)


def test_rejects_forged_tps_math(tmp_path):
    payload = valid_payload(200.0)
    payload["runs"][0]["emitted_tps"] = 900.0
    with pytest.raises(gate.GateFailure, match="does not equal"):
        gate.validate_result(write_payload(tmp_path, payload), 0.0, 7)


def test_source_scanner_rejects_low_bit_calls():
    violations = gate.scan_text("weights = mx.quantize(weights, bits=4)", "candidate.py")
    assert violations and "MLX weight quantization" in violations[0]


def test_source_scanner_allows_runtime_audit_labels():
    assert gate.scan_text('result["quantized_module_count"] = 0', "benchmark.py") == []


def test_source_scanner_rejects_low_bit_kv_setting():
    violations = gate.scan_text("generate(model, kv_bits=4)", "benchmark.py")
    assert violations and "low-bit KV setting" in violations[0]


def test_checkpoint_gate_accepts_bf16_model_and_adapter(tmp_path):
    artifact = checkpoint_artifact(tmp_path)
    adapter_path = Path(json.loads(artifact.read_text())["model"]["draft_adapter"]["local_path"])
    gate.checkpoint_gate(artifact, expected_adapter_sha256=hashlib.sha256(adapter_path.read_bytes()).hexdigest())


def test_checkpoint_gate_rejects_nonofficial_adapter_hash(tmp_path):
    with pytest.raises(gate.GateFailure, match="does not match NVIDIA"):
        gate.checkpoint_gate(checkpoint_artifact(tmp_path))


def test_checkpoint_gate_rejects_packed_weight_dtype(tmp_path):
    with pytest.raises(gate.GateFailure, match="only BF16"):
        gate.checkpoint_gate(checkpoint_artifact(tmp_path, dtype="U32"), expected_adapter_sha256="0" * 64)


def test_checkpoint_gate_rejects_quantization_config(tmp_path):
    with pytest.raises(gate.GateFailure, match="low-bit path"):
        gate.checkpoint_gate(
            checkpoint_artifact(tmp_path, config_extra={"quantization_config": {"bits": 4}}),
            expected_adapter_sha256="0" * 64,
        )
