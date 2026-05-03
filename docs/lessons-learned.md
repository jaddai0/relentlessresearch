# RelentlessResearch Lessons Learned

This file captures framework lessons discovered while running hard problems. Keep it with the template pack and copy relevant notes into each problem blueprint.

## Prompt Memory And Restarts

- The runner loads config once at process start. Changes to `context_files`, `problem.known_facts`, model settings, validation commands, or success commands do not affect an already-running background process. Restart the loop after changing config.
- Updating the target files that are already listed in `context_files` is enough for the next prompt, because file content is read each iteration.
- The model can overwrite the research notebook with a narrower or misleading summary. Durable human guidance should live in a stable context doc such as `docs/relentless_problem_blueprint.md`, and the system prompt should tell the model not to drop durable facts.
- Long autonomous runs need scheduled compaction. Archive raw iteration artifacts and feed the worker a compact checkpoint plus only a few recent iterations; otherwise the model starts anchoring on stale failed diagnostics and prompt noise.
- A useful default is `compaction.enabled=true`, `every_iterations=10`, `max_recent_iterations=3`, and capped observation/diff/context sizes. Increase context only for a specific reason.

## Supervisor Role

- RelentlessResearch works best with a supervising architect. The supervisor should proactively intervene when the worker repeats a false trail, over-interprets weak evidence, ignores a known constraint, or spends multiple iterations fixing diagnostic plumbing.
- Steering does not have to mean taking over implementation. Often the highest leverage move is updating the blueprint with a crisp ruled-out hypothesis, an interpretation warning, or a concrete next experiment.
- If steering changes config or context-file lists, restart the loop. If steering edits an already-listed context file, the next iteration will see it.
- Treat `max_iterations` as a checkpoint, not an invitation to blindly extend a stalled run. When a run stops at the cap, inspect the last several iterations, repair any validation breakage, compact/update the blueprint, and restart only with narrower steering.
- Compaction/archive code must tolerate minimal Python builds. If gzip is unavailable, fall back to plain `.tar` archives rather than crashing the active research loop after a successful iteration.

## Avoiding False Trails

- Models often misread packed or encoded tensor shapes as effective runtime dimensions. Add explicit equations and layout facts to the blueprint.
- Packed expert tensors may include both an expert axis and a packed inner dimension. For example, a shape like `(256, 2048, 768)` can mean 256 experts, 2048 output dims, and Q6-packed 4096 input dims (`4096 * 6 / 32 = 768`). The correct diagnostic is effective expert-output parity, not raw shape equality against float reference tensors.
- Raw storage equality is not the same as numerical parity. For quantized or packed formats, ask for effective dequantized slice comparisons or component-level forward parity.
- Hand-written dequantizers are easy to get subtly wrong. Do not let a worker patch production quantization from manual bit-unpack output until that unpacker is validated against module behavior or an independent known-good reference.
- Quantized reference metadata can be padded. If a block-scale tensor has more blocks than `ceil(weight_dim / block_size)`, expand the blocks and crop to the actual weight shape before comparing; do not infer a runtime bug from padded scale metadata alone.
- MLX arrays with bfloat16 or packed metadata may fail direct `np.array(...)` conversion. Cast/evaluate first, e.g. `np.array(mx.eval(arr.astype(mx.float32)))`, before NumPy diagnostics.
- If a diagnostic fails because of a script bug, the next step should usually fix the diagnostic, not mutate production code.
- For runtime investigations, prove all diagnostics load the same implementation class as production. A model artifact `model_file`, local shim override, or helper session can make two tests with the same checkpoint exercise different `Model.__call__` signatures.
- Track keyword contracts across upstream helpers. If upstream generation calls `input_embeddings` but the local shim only accepts `inputs_embeds`, a diagnostic can fail or silently avoid the path being tested.
- Load decode settings from the authoritative generation config, not only the model config. Scalar `eos_token_id` in `config.json` can disagree with the EOS list in `generation_config.json`, causing solved outputs to run on until a max-token cap.
- Separate termination bugs from first-token quality bugs. EOS handling can waste tokens and money after a valid answer, but it does not explain a bad prefill logit distribution by itself.
- When a primitive is suspected, build a tiny numerical micro-test before patching production. For example, compare attention-sink math on small random Q/K/V tensors before blaming a full model's sink implementation.
- When a late layer looks suspicious, test causal responsibility by swapping or manually recomputing only that layer. If replacing the suspect layer leaves the same bad top tokens, the fault is upstream or cumulative rather than isolated there.
- Use ablations with original high-precision embeddings or output heads before blaming those endpoints. If original embeddings or `lm_head` leave the same bad logits, the hidden trajectory is the problem.
- Prompt-format matrices are a cheap sanity check. If plain completions and chat templates all produce the same degraded token family, stop treating the chat template as the primary root cause.
- Preserve artifact provenance as a first-class validation target. Resume-based converters should record source artifact identity, command, converter version, shard hashes, and copied/preserved tensor groups; otherwise stale tensors can survive a rebuild and confuse the investigation.
- If the worker spends several iterations on diagnostic plumbing, split the diagnostic into a smaller proof-of-capture step before asking for full parity math. A valid capture with shapes/RMS is useful; a large script with unchecked hooks can quietly waste many attempts.
- Once key names, tensor shapes, or other static discovery facts are known, copy the exact values into the blueprint and explicitly forbid repeated discovery. The next loop step should consume those facts in an effective parity or behavior diagnostic, not spend more attempts listing keys with slightly different substrings.
- If a loader helper is a context manager, put the exact working usage pattern in the blueprint. Many iterations can be wasted by models repeatedly treating a context manager like a direct-return function or by using partially-initialized runtime attributes instead of the established session loader.
- Diagnostics must not print optimistic summaries when all comparisons failed or were skipped. Add attempted/successful/skipped counters and exit nonzero if no useful comparison ran.
- Do not let the worker over-interpret a single ambiguous metric. Add expected error ranges or "not sufficient by itself" notes to the blueprint when a metric is ambiguous.
- Matching top-k outputs for the same input can be stronger evidence than a small relative L2 difference. If top-k matches exactly, steer away from treating that component as the primary culprit unless other evidence contradicts it.
- Trivial test inputs can create false confidence. For attention bugs, a one-token prompt does not exercise causal attention, RoPE position interactions, or multi-token cache behavior.
- Python special-method lookup can make diagnostic monkey-patches silently ineffective. Assigning `obj.__call__ = wrapper` on an instance may not affect `obj(...)`; patch `type(obj).__call__` with a guarded wrapper, or call the target method directly.
- When wrapping compiled/helper functions, verify return ordering from source before unpacking. A reversed `(indices, scores)` tuple can make a good diagnostic fail while looking like a model bug.
- Command-policy failures are informative: add examples of forbidden shell syntax to the blueprint so the model stops proposing rejected commands.
- Validation targets must exercise the actual production path that recently failed. Helper-level contracts can pass while the real loader, custom artifact class, or decode method is still broken; add cheap fake-model tests for expensive paths when full model loads are too costly.
- Do not prove runtime tensor loading from source-string heuristics. For optional learned tensors such as attention sinks, compare configured expected keys against the artifact index, and use a heavyweight loaded-model check only when needed.
- If both `inputs_embeds` and token prefill produce identical cache behavior, stop treating embedding keyword plumbing as the quality root cause and move on to trajectory or weight/semantic probes.
- A duplicate-looking helper call may be harmless if the second helper is idempotent. Verify the helper's no-op conditions before declaring an explosive expansion bug, then simplify the path and add an exact contract test.
- Cache equivalence probes should compare top-token overlap as well as relative L2. Small numeric differences can look alarming while preserving the same bad token family, which means the cache is probably reflecting an upstream collapse rather than causing it.
- Range ablations can be misleading when they mix precision regimes inside a brittle trajectory. If replacing a layer range with higher-precision math makes rank worse, record it, but do not conclude the quantized layer is good without a same-trajectory or full-consistent test.
- When single-layer ablations show non-monotonic effects, scan the whole layer family in one model load. A single-load rank table can reveal sensitive layers and anti-helpful neighbors much faster than one process per layer.
- A component can be "structurally correct" and still quality-sensitive. For example, effective MoE parity with cosine around `0.9993` and relative L2 around `0.036` is not a wiring bug, but it may still be enough quantization error to perturb a fragile full-model trajectory.
- Preserve both causal-ablation results and direct-parity results. The useful interpretation often comes from their tension: a layer can pass parity thresholds, improve answer rank when replaced, and still fail to solve top-token quality.
- An "oracle" can share the same bad assumption as the implementation under
  test. External reference diffs can expose hidden layout, packing, or API
  assumptions that local parity checks accidentally reuse.
- Fused projection tensors may be rank-packed or otherwise structured, not
  merely concatenated. Verify storage layout from an independent reference
  before writing split/reorder logic.
- Passing tiny all-zero multimodal fixtures is not enough. Include nonzero
  inputs and shape cases that exercise every suspected index, tiling, ordering,
  and masking branch.
- Baseline samples can be invalid diagnostics. A quality script that forced a
  2048x2048 image down to `3136` pixels made a map look like a tiny blurry
  icon; a mislabeled or ambiguous audio sample produced the same wrong caption
  with both reference and ported embeddings. Separate model-path failures from
  bad or too-hard evaluation examples.

## Good Autonomy Pattern

- Give the model a short list of ruled-out hypotheses and a short list of next best experiments.
- Prefer diagnostics that isolate the first wrong numerical distribution.
- Keep success gates strict and separate from model confidence.
- When progress becomes repetitive, pause or restart with a stronger blueprint rather than simply raising `max_iterations`.
