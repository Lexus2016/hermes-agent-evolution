# Independent upstream conflict probes

All tags use the same source, not a sequential merge simulation.
AUTONOMOUS means ≤10 non-core conflicts only; other sync gates still apply.

| tag | .github | agent | cron | hermes_cli | tools | other | total | core | behind | verdict |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| v2026.9.24 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | AUTONOMOUS |
| v0.21.6 | 35 | 23 | 3 | 16 | 29 | 72 | 178 | 47 | 9180 | OWNER-SESSION |
| v0.21.7 | 35 | 28 | 4 | 18 | 33 | 80 | 198 | 55 | 9957 | OWNER-SESSION |
| v0.22.0 | 35 | 29 | 4 | 19 | 33 | 81 | 201 | 57 | 10713 | OWNER-SESSION |
| v0.22.1 | 35 | 29 | 4 | 21 | 34 | 84 | 207 | 59 | 12929 | OWNER-SESSION |

v2026.9.24: source `1a69d59c9d83855d85ed6264c79b29f7c5efd2af`, target `f97608f178d1ffeca59860195ab7da295f7c8e5f`.

v0.21.6: source `1a69d59c9d83855d85ed6264c79b29f7c5efd2af`, target `818c13be1dc4fd28987e1e881a9408224afd4535`.

v0.21.7: source `1a69d59c9d83855d85ed6264c79b29f7c5efd2af`, target `71d34bad1045610252eda02ee79549436896e784`.

v0.22.0: source `1a69d59c9d83855d85ed6264c79b29f7c5efd2af`, target `e06df9c2eae6901f4c88f464e0ae6e753c0428b8`.

v0.22.1: source `1a69d59c9d83855d85ed6264c79b29f7c5efd2af`, target `297552a9d646119871391d202509822cd86330df`.
