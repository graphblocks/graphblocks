# vLLM Configuration Performance Benchmark

This example compares vLLM server configurations under one fixed workload. It
reports time to first token (TTFT), per-request decode tokens per second, and
aggregate output-token throughput, then evaluates the candidate configuration
with GraphBlocks `MetricObservation`, `evaluate_gate`, and `TrialResult`.

Run the deterministic fixture from the repository root:

```bash
python examples/14-vllm-config-benchmark/run.py
```

The fixture compares `max_num_batched_tokens=2048, max_num_seqs=32` with
`max_num_batched_tokens=8192, max_num_seqs=128`. Both keep tensor parallelism,
chunked prefill, model revision, hardware, prompt lengths, output limit,
concurrency, request rate, seed, temperature, prefix caching, stream interval,
KV-cache allocation, dtype, and warmup count fixed. Its values exercise the
benchmark contract and are not real vLLM performance claims.

## Metric definitions

- TTFT is the time from request start until the first output token.
- Decode TPS is `(output_tokens - 1) / (E2E - TTFT)` in seconds, the inverse of
  vLLM's time-per-output-token calculation.
- Output throughput TPS is all generated tokens divided by benchmark wall time.

The first output token is excluded from decode TPS because its latency is
already represented by TTFT. Report per-request decode TPS and aggregate output
throughput together; they answer different capacity questions. These formulas
match the [official vLLM serving benchmark](https://docs.vllm.ai/en/latest/api/vllm/benchmarks/serve/).

## Run against vLLM

Install vLLM's benchmark dependencies and start the baseline on the target
hardware:

```bash
python -m pip install 'vllm[bench]'
vllm serve MODEL \
  --tensor-parallel-size 1 \
  --dtype bfloat16 \
  --max-model-len 4096 \
  --max-num-batched-tokens 2048 \
  --max-num-seqs 32 \
  --enable-chunked-prefill \
  --no-enable-prefix-caching \
  --gpu-memory-utilization 0.90 \
  --stream-interval 1
```

In another shell, run the [official benchmark CLI](https://docs.vllm.ai/en/latest/cli/bench/serve/):

```bash
vllm bench serve \
  --backend openai \
  --model MODEL \
  --dataset-name random \
  --random-input-len 512 \
  --random-output-len 64 \
  --random-range-ratio 0 \
  --num-prompts 4 \
  --request-rate inf \
  --max-concurrency 4 \
  --num-warmups 2 \
  --seed 7 \
  --ignore-eos \
  --percentile-metrics ttft,tpot,itl,e2el \
  --metric-percentiles 50,95 \
  --save-result \
  --save-detailed \
  --metadata config_id=baseline
```

Stop the server, restart it on the same hardware with the `larger-batch`
arguments from [configs.yaml](configs.yaml), and repeat with metadata
`config_id=larger-batch`. Copy the detailed TTFT, E2E, output-token, and run
duration measurements into the matrix fixture to generate the GraphBlocks
comparison report.

For a fair production comparison, pin the vLLM version, model revision,
tokenizer, dtype/quantization, GPU type/count, power settings, request arrival
pattern, prompt/output lengths, sampling parameters, warmups, and concurrency.
Run multiple repetitions, alternate config order, and retain the raw vLLM result
JSON alongside the GraphBlocks evidence digest. The official
[optimization guide](https://docs.vllm.ai/en/latest/configuration/optimization/)
explains the latency/throughput trade-offs of chunked prefill and batch size.
