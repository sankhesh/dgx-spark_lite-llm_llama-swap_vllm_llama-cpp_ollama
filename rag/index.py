#!/usr/bin/env python3
"""
Index a source repository (default: VTK) into a local vector store for RAG.

Walks the repo, chunks text/source files, embeds each chunk via the LiteLLM
`/v1/embeddings` endpoint (nomic-embed-text served by the always-on llama-embed
container), and writes:

  <out>/chunks.sqlite   - chunk metadata + text
  <out>/embeddings.npy  - float32 [N, dim] L2-normalized matrix, row i <-> chunk rowid i+1

Query it with ask.py.

Example:
  ./.venv/bin/python index.py --repo /home/sankhesh/Projects/vtk
"""
import argparse, os, sqlite3, subprocess, sys, time
import numpy as np
import requests

# nomic-embed-text requires task-specific prefixes.
DOC_PREFIX = "search_document: "

DEFAULT_EXTS = [".h", ".hxx", ".txx", ".md"]  # API headers + docs; add .cxx/.py to widen

# Keep each chunk safely under the embed model's 2048-token context. ~4000 chars
# of source is well under that even with long lines; longer windows are truncated.
MAX_CHARS = 2400


def list_files(repo, exts):
    """Prefer git-tracked files; fall back to os.walk."""
    try:
        out = subprocess.check_output(["git", "-C", repo, "ls-files"], text=True)
        files = out.splitlines()
    except Exception:
        files = []
        for root, _, names in os.walk(repo):
            if "/.git/" in root + "/":
                continue
            for n in names:
                files.append(os.path.relpath(os.path.join(root, n), repo))
    return [f for f in files if os.path.splitext(f)[1].lower() in exts]


def chunk_file(path, chunk_lines, overlap):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
    except Exception:
        return []
    if not lines:
        return []
    chunks, step = [], max(1, chunk_lines - overlap)
    for start in range(0, len(lines), step):
        window = lines[start:start + chunk_lines]
        text = "".join(window).strip()[:MAX_CHARS]
        if text:
            chunks.append((start + 1, start + len(window), text))
        if start + chunk_lines >= len(lines):
            break
    return chunks


def _embed_call(api_base, model, texts, timeout=180):
    r = requests.post(
        f"{api_base}/embeddings",
        json={"model": model, "input": [DOC_PREFIX + t for t in texts]},
        headers={"Authorization": "Bearer sk-dgx-local"},
        timeout=timeout,
    )
    r.raise_for_status()
    data = sorted(r.json()["data"], key=lambda d: d["index"])
    return [d["embedding"] for d in data]


def embed_texts(api_base, model, texts):
    """Embed a list, resilient to individual over-long/bad chunks. Returns a
    list aligned to `texts`; any item that can't be embedded becomes None
    (filled with a zero vector later, so it just never matches a query)."""
    try:
        return _embed_call(api_base, model, texts)
    except Exception:
        if len(texts) == 1:
            try:                       # last resort: hard-truncate the offender
                return _embed_call(api_base, model, [texts[0][:1200]])
            except Exception:
                return [None]
        mid = len(texts) // 2
        return (embed_texts(api_base, model, texts[:mid]) +
                embed_texts(api_base, model, texts[mid:]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="/home/sankhesh/Projects/vtk")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "index"))
    # Embeddings go straight to the always-on llama-embed container (avoids
    # LiteLLM spend-logging churn over tens of thousands of calls).
    ap.add_argument("--api-base", default="http://localhost:19100/v1")
    ap.add_argument("--embed-model", default="nomic-embed-text-v1.5")
    ap.add_argument("--exts", default=",".join(DEFAULT_EXTS),
                    help="comma-separated file extensions to index")
    ap.add_argument("--chunk-lines", type=int, default=60)
    ap.add_argument("--overlap", type=int, default=10)
    ap.add_argument("--batch", type=int, default=4,
                    help="chunks per embed request; keep small — the embed "
                         "server's total token budget is ~8k across its slots")
    ap.add_argument("--max-files", type=int, default=0, help="0 = all")
    args = ap.parse_args()

    exts = [e if e.startswith(".") else "." + e for e in args.exts.split(",")]
    os.makedirs(args.out, exist_ok=True)
    files = list_files(args.repo, exts)
    if args.max_files:
        files = files[:args.max_files]
    print(f"[index] {len(files)} files matching {exts} under {args.repo}")

    # Build all chunks first.
    metas, texts = [], []
    for rel in files:
        for (s, e, text) in chunk_file(os.path.join(args.repo, rel), args.chunk_lines, args.overlap):
            metas.append((rel, s, e))
            texts.append(text)
    print(f"[index] {len(texts)} chunks; embedding in batches of {args.batch} ...")

    db = sqlite3.connect(os.path.join(args.out, "chunks.sqlite"))
    db.execute("DROP TABLE IF EXISTS chunks")
    db.execute("CREATE TABLE chunks (id INTEGER PRIMARY KEY, path TEXT, start_line INT, end_line INT, text TEXT)")

    vecs, t0, skipped = [], time.time(), 0
    for i in range(0, len(texts), args.batch):
        bt = texts[i:i + args.batch]
        embs = embed_texts(args.api_base, args.embed_model, bt)
        vecs.extend(embs)
        for j, emb in enumerate(embs):
            if emb is None:
                skipped += 1
            rel, s, e = metas[i + j]
            db.execute("INSERT INTO chunks (id, path, start_line, end_line, text) VALUES (?,?,?,?,?)",
                       (i + j + 1, rel, s, e, bt[j]))
        done = i + len(bt)
        rate = done / max(1e-6, time.time() - t0)
        print(f"\r[index] {done}/{len(texts)} chunks ({rate:.0f}/s)", end="", flush=True)
    db.commit(); db.close()

    dim = next((len(v) for v in vecs if v is not None), 768)
    mat = np.zeros((len(vecs), dim), dtype=np.float32)
    for i, v in enumerate(vecs):
        if v is not None:
            mat[i] = v
    mat /= (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9)  # normalize for cosine
    np.save(os.path.join(args.out, "embeddings.npy"), mat)
    print(f"\n[index] done: {mat.shape[0]} vectors dim {dim} "
          f"({skipped} unembeddable, zero-filled) -> {args.out}")


if __name__ == "__main__":
    main()
