#!/usr/bin/env bash
# Refresh the VTK RAG index: pull VTK, re-embed, reload rag-proxy.
# Designed to run unattended from cron. Logs to rag/refresh.log.
#
#   Manual run:   rag/refresh.sh
#   Env overrides:
#     VTK_REPO   path to the VTK checkout   (default /home/sankhesh/Projects/vtk)
#     RAG_EXTS   comma-separated extensions (default: headers + docs + C++ + Python)
set -uo pipefail

# Index API headers, docs, C++ implementation, and Python. This is the full
# source tree (minus build/generated files), so a rebuild is larger/slower than
# headers-only — fine for a nightly job. Override RAG_EXTS to narrow it.
: "${RAG_EXTS:=.h,.hxx,.txx,.md,.cxx,.cpp,.cc,.py}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VTK_REPO="${VTK_REPO:-/home/sankhesh/Projects/vtk}"
LOG="$REPO_ROOT/rag/refresh.log"

# cron has a minimal PATH; make sure git + docker are found.
export PATH="/usr/local/bin:/usr/bin:/bin:$PATH"
export GIT_TERMINAL_PROMPT=0   # fail instead of hanging on a credential prompt

exec >>"$LOG" 2>&1
echo "===== $(date -Is) refresh start ====="

echo "[refresh] pulling VTK ($VTK_REPO)"
git -C "$VTK_REPO" pull --ff-only || echo "[refresh] git pull failed/skipped — indexing current checkout"

echo "[refresh] rebuilding index (exts: $RAG_EXTS)"
if ! "$REPO_ROOT/rag/.venv/bin/python" "$REPO_ROOT/rag/index.py" \
        --repo "$VTK_REPO" --batch 8 --exts "$RAG_EXTS"; then
    echo "[refresh] index build FAILED — leaving the existing index in place"
    exit 1
fi

echo "[refresh] restarting rag-proxy to load the new index"
( cd "$REPO_ROOT" && docker compose restart rag-proxy )

echo "===== $(date -Is) refresh done ====="
