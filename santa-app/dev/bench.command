#!/bin/bash
# Downloads (once) and benchmarks the Santa models on the synthetic eval set.
# Each model runs in its own process so cold-start timings are real.
cd "$(dirname "$0")" || exit 1
PY="$HOME/Library/Application Support/Santa/venv/bin/python"
mkdir -p out
LOG=out/bench-console.txt
: > "$LOG"
for M in ${SANTA_BENCH_MODELS:-small large-v3-turbo}; do
  echo "== $M $(date)" | tee -a "$LOG"
  "$PY" bench.py --model "$M" --set synthetic 2>&1 | tee -a "$LOG"
done
# Thread-count comparison on the recommended model (speed only)
for T in 4 8; do
  echo "== large-v3-turbo threads=$T $(date)" | tee -a "$LOG"
  "$PY" bench.py --model large-v3-turbo --set synthetic --threads $T --speed-only 2>&1 | tee -a "$LOG"
done
echo "== BENCH DONE $(date)" | tee -a "$LOG"
