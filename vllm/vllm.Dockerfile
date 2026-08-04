# vllm.Dockerfile
# Optimized vLLM for DGX Spark (GB10)
#
# NOTE: single-stage on purpose. An earlier version installed vllm into a
# `devel` build stage and then switched to a `runtime` stage for the final
# image without ever COPYing the installed packages across — the shipped
# image had python3 but no vllm at all. The runtime image also needs
# libgomp1 (torch depends on it at import time, not just at build time), so
# splitting stages saves little and risks missing runtime shared libs.
FROM nvidia/cuda:13.1.0-devel-ubuntu24.04

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-dev git curl libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Use the eugr/spark-vllm-docker recommendation:
# Pull the nightly wheels built specifically for Grace-Blackwell
RUN pip install --no-cache-dir --break-system-packages vllm --extra-index-url https://wheels.vllm.ai/nightly/

# Ensure MoE backends are optimized for SM12.1
ENV VLLM_FLASHINFER_MOE_BACKEND=latency
ENV GGML_CUDA_ENABLE_UNIFIED_MEMORY=1

ENTRYPOINT ["python3", "-m", "vllm.entrypoints.openai.api_server"]
