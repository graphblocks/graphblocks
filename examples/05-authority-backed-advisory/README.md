# Authority-Backed Advisory

This domain-local legal fixture demonstrates a general source/evidence/review
pattern: resolve an authoritative source, build supported claims, run independent
checks, and gate the final advisory result. GraphBlocks itself remains
domain-neutral.

```bash
python examples/05-authority-backed-advisory/run.py
```

The script executes the graph with dated authority/document fakes, a scripted
LLM, a source gate, and a recording reviewer. It makes no request to the
illustrative authority APIs.
