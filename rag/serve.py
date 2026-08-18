#!/usr/bin/env python3
"""
rag-proxy: an OpenAI-compatible /v1/chat/completions endpoint that answers with
retrieval-augmented context from the indexed repo (see index.py).

For every request it embeds the last user message, retrieves the top-k chunks
from the local index, injects them as context, and forwards to the real code
model — streaming the answer straight back. Register it in LiteLLM as a model
(e.g. `vtk-rag`) so any OpenAI-compatible client (codecompanion, aider, …) can
use RAG just by selecting that model name; retrieval happens server-side.

Talks directly to llama-embed (embeddings) and llama-swap (chat) on dgx_net —
NOT back through LiteLLM — so there is no proxy loop. Stdlib + numpy + requests.
"""
import json, os, sqlite3, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import numpy as np
import requests

EMBED_URL   = os.environ.get("EMBED_URL", "http://llama-embed:8080/v1")
CHAT_URL    = os.environ.get("CHAT_URL", "http://llama-swap:8080/v1")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "nomic-embed-text-v1.5")
CHAT_MODEL  = os.environ.get("CHAT_MODEL", "Qwen2.5-Coder-32B-Instruct")
INDEX_DIR   = os.environ.get("INDEX_DIR", "/index")
TOP_K       = int(os.environ.get("TOP_K", "8"))
SERVED_NAME = os.environ.get("SERVED_NAME", "vtk-rag")
PORT        = int(os.environ.get("PORT", "8080"))

QUERY_PREFIX = "search_query: "
SYSTEM = (
    "You are a senior VTK (Visualization Toolkit) engineer. Use the provided "
    "source context to answer. Cite files as `path:start-end`. If the context "
    "is insufficient, say so rather than inventing APIs. Prefer concrete, "
    "compilable examples."
)

# --- load the index once into memory ---
print(f"[rag-proxy] loading index from {INDEX_DIR} ...", flush=True)
MAT = np.load(os.path.join(INDEX_DIR, "embeddings.npy"))
DB_PATH = os.path.join(INDEX_DIR, "chunks.sqlite")
print(f"[rag-proxy] {MAT.shape[0]} vectors, dim {MAT.shape[1]}", flush=True)


def last_user_text(messages):
    for m in reversed(messages):
        if m.get("role") == "user":
            c = m.get("content", "")
            if isinstance(c, list):  # OpenAI array-content form
                return " ".join(p.get("text", "") for p in c if isinstance(p, dict))
            return c or ""
    return ""


def retrieve(query):
    r = requests.post(f"{EMBED_URL}/embeddings",
                      json={"model": EMBED_MODEL, "input": QUERY_PREFIX + query},
                      timeout=60)
    r.raise_for_status()
    q = np.asarray(r.json()["data"][0]["embedding"], dtype=np.float32)
    q /= (np.linalg.norm(q) + 1e-9)
    top = np.argsort(-(MAT @ q))[:TOP_K]
    db = sqlite3.connect(DB_PATH)
    blocks = []
    for rank, idx in enumerate(top, 1):
        row = db.execute("SELECT path, start_line, end_line, text FROM chunks WHERE id=?",
                         (int(idx) + 1,)).fetchone()
        if row:
            p, s, e, t = row
            blocks.append(f"### [{rank}] {p}:{s}-{e}\n```\n{t}\n```")
    db.close()
    return "\n\n".join(blocks)


def build_messages(incoming):
    ctx = retrieve(last_user_text(incoming))
    context_msg = {"role": "system",
                   "content": SYSTEM + "\n\nRelevant VTK source context "
                   "(cite as path:start-end):\n\n" + ctx}
    # PRESERVE the client's own system prompt(s) and tool instructions — an
    # agent framework's tool-use rules live there — and inject the retrieved VTK
    # context as an ADDITIONAL system message right after them.
    systems = [m for m in incoming if m.get("role") == "system"]
    others = [m for m in incoming if m.get("role") != "system"]
    return systems + [context_msg] + others


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            self._json(200, {"object": "list", "data": [
                {"id": SERVED_NAME, "object": "model", "owned_by": "rag-proxy"}]})
        elif self.path == "/health":
            self._json(200, {"status": "ok"})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self.path.rstrip("/").endswith("/chat/completions"):
            return self._json(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(length) or b"{}")
        stream = bool(req.get("stream", False))
        payload = {
            "model": CHAT_MODEL,
            "messages": build_messages(req.get("messages", [])),
            "stream": stream,
        }
        # Forward generation params AND tool-calling fields, so a tools-capable
        # CHAT_MODEL (e.g. Qwen3-Coder-30B-tools) can do agentic editing with
        # the injected VTK context. tool_calls come back through the SSE relay
        # / JSON passthrough unchanged.
        for k in ("max_tokens", "temperature", "top_p", "tools", "tool_choice"):
            if k in req:
                payload[k] = req[k]

        up = requests.post(f"{CHAT_URL}/chat/completions", json=payload,
                           stream=stream, timeout=900)
        if not stream:
            self._json(up.status_code, up.json())
            return
        self.send_response(up.status_code)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            for line in up.iter_lines():
                # Relay upstream SSE lines verbatim (already chat.completion.chunk format).
                self.wfile.write(line + b"\n" if line else b"\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


if __name__ == "__main__":
    print(f"[rag-proxy] serving model '{SERVED_NAME}' on :{PORT} "
          f"(embed={EMBED_URL}, chat={CHAT_URL}/{CHAT_MODEL})", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
