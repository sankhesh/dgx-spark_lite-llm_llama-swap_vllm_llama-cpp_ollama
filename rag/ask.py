#!/usr/bin/env python3
"""
Ask a question against the indexed repo (see index.py).

Embeds the query, retrieves the top-k most similar chunks by cosine similarity,
builds a grounded prompt, and streams an answer from the code model via LiteLLM.

Example:
  ./.venv/bin/python ask.py "How do I create a vtkPolyData from points and cells?"
"""
import argparse, json, os, sqlite3, sys
import numpy as np
import requests

QUERY_PREFIX = "search_query: "

SYSTEM = (
    "You are a senior VTK (Visualization Toolkit) engineer. Answer the user's "
    "question using ONLY the provided source context when possible. Cite the "
    "files you used as `path:start-end`. If the context is insufficient, say so "
    "rather than inventing APIs. Prefer concrete, compilable code examples."
)


def embed_query(embed_base, model, text):
    r = requests.post(
        f"{embed_base}/embeddings",
        json={"model": model, "input": QUERY_PREFIX + text},
        headers={"Authorization": "Bearer sk-dgx-local"},
        timeout=60,
    )
    r.raise_for_status()
    v = np.asarray(r.json()["data"][0]["embedding"], dtype=np.float32)
    return v / (np.linalg.norm(v) + 1e-9)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query")
    ap.add_argument("--index", default=os.path.join(os.path.dirname(__file__), "index"))
    ap.add_argument("--embed-base", default="http://localhost:19100/v1",
                    help="embeddings endpoint (direct llama-embed container)")
    ap.add_argument("--embed-model", default="nomic-embed-text-v1.5")
    ap.add_argument("--chat-base", default="http://localhost:14000/v1",
                    help="chat endpoint (LiteLLM gateway)")
    ap.add_argument("--chat-model", default="Qwen2.5-Coder-32B-Instruct")
    ap.add_argument("--k", type=int, default=8)
    ap.add_argument("--max-tokens", type=int, default=1024)
    ap.add_argument("--show-sources", action="store_true")
    args = ap.parse_args()

    mat = np.load(os.path.join(args.index, "embeddings.npy"))
    db = sqlite3.connect(os.path.join(args.index, "chunks.sqlite"))

    q = embed_query(args.embed_base, args.embed_model, args.query)
    sims = mat @ q
    top = np.argsort(-sims)[:args.k]

    blocks, sources = [], []
    for rank, idx in enumerate(top, 1):
        row = db.execute("SELECT path, start_line, end_line, text FROM chunks WHERE id=?",
                         (int(idx) + 1,)).fetchone()
        if not row:
            continue
        path, s, e, text = row
        sources.append(f"{path}:{s}-{e}  (score {sims[idx]:.3f})")
        blocks.append(f"### [{rank}] {path}:{s}-{e}\n```\n{text}\n```")
    db.close()

    if args.show_sources:
        print("Retrieved context:\n  " + "\n  ".join(sources) + "\n", file=sys.stderr)

    user = "Context:\n\n" + "\n\n".join(blocks) + f"\n\n---\nQuestion: {args.query}"
    r = requests.post(
        f"{args.chat_base}/chat/completions",
        json={
            "model": args.chat_model,
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": user}],
            "max_tokens": args.max_tokens,
            "stream": True,
        },
        headers={"Authorization": "Bearer sk-dgx-local"},
        stream=True, timeout=600,
    )
    r.raise_for_status()
    for line in r.iter_lines():
        if not line or not line.startswith(b"data: "):
            continue
        payload = line[6:]
        if payload.strip() == b"[DONE]":
            break
        try:
            delta = json.loads(payload)["choices"][0]["delta"].get("content", "")
            sys.stdout.write(delta); sys.stdout.flush()
        except Exception:
            pass
    print()
    print("\nSources:\n  " + "\n  ".join(sources))


if __name__ == "__main__":
    main()
