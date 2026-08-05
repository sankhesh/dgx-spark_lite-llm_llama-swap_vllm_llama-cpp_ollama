# VTK RAG tool

Retrieval-augmented Q&A over a source repo (default: VTK at
`/home/sankhesh/Projects/vtk`), served entirely by the local DGX Spark stack:

- **Embeddings** → `nomic-embed-text` on the always-on `llama-embed` container.
- **Generation** → `Qwen2.5-Coder-32B-Instruct` via llama-swap.
- Both reached through the LiteLLM gateway (`http://localhost:14000/v1`).

No external services, no training — just retrieval + a code model.

## Setup (one-time)

The venv and models are already provisioned. If recreating:

```bash
python3 -m venv rag/.venv
rag/.venv/bin/pip install numpy requests
```

The stack must be up (`docker compose up -d`), including `llama-embed`.

## Build the index

```bash
rag/.venv/bin/python rag/index.py --repo /home/sankhesh/Projects/vtk
```

Runs with no flags — embeddings go to the always-on `llama-embed` container
(`localhost:19100`). Defaults index API headers + docs (`.h .hxx .txx .md`) at
`--batch 8`. Widen with `--exts`:

```bash
# include C++ sources and Python too (much larger / slower)
rag/.venv/bin/python rag/index.py --exts .h,.hxx,.txx,.md,.cxx,.py
```

Other flags: `--chunk-lines`, `--overlap`, `--batch`, `--max-files` (handy for a
quick trial run), `--out` (index location). Output goes to `rag/index/`
(gitignored).

## Ask questions

```bash
rag/.venv/bin/python rag/ask.py "How do I create a vtkPolyData from points and cells?"
rag/.venv/bin/python rag/ask.py --k 12 --show-sources "What is the difference between vtkImageData and vtkStructuredGrid?"
```

The answer streams to stdout, followed by the source files it retrieved.
`--show-sources` also prints the retrieved chunks (with scores) to stderr first.

## Notes

- Re-run `index.py` after pulling new VTK changes to refresh the vectors.
- Retrieval is brute-force cosine (numpy) — fine well past VTK's size.
- To use a different chat model: `--chat-model Meta-Llama-3.1-70B-Instruct`.
