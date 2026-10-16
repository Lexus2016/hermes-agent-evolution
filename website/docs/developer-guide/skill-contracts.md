# Versioned skill routing contracts

Skills may opt into a top-level `contract` frontmatter mapping. Legacy skills
without a contract keep their existing behavior. The first supported behavioral
criterion is `routing-applicability`: deterministic phrase-based applicability,
plus literal preservation of protected instructions. This is not an LLM judge
and does not establish arbitrary natural-language task outcomes or learned
performance improvements.

The pilot is `evolution-extract`. Its contract records a positive integer
`version`, a bounded `intent`, `behavioral_criteria: [routing-applicability]`,
`applies_to` and `excludes` phrase lists, `protected_constraints` (literal body
text), and `replay_cases` containing unique `id`, `task`, and `applicable` values.
Expected applicability is `true`, `false`, or `null` (unknown).

Matching is case-insensitive with word boundaries. An explicit exclusion hides
a skill from task-grounded retrieval only if no positive scope also matches.
Mixed and unknown applicability remain available to ordinary lexical ranking.
The live skill registry supplies the metadata to the existing retrieval path.

Each skill edit must increase the contract version. Existing protected strings
and historical replay cases cannot be removed or rewritten. Protected strings
must remain in the body, and every historical/current replay case is run against
the same applicability evaluator used by retrieval. A failed replay or missing
constraint rejects `skill_manage` edits before writing. Contracted skills cannot
be deleted (including `remove_file` of `SKILL.md`) or renamed through this path.
The pre-PR runner compares changed skills against an immutable resolved baseline
(`--contract-base`, default `origin/main`) and also rejects deletion, rename, or
contract removal. An unavailable baseline fails closed. Repository maintainers
must review intentional retirement separately; raw filesystem access is not a
security boundary provided by this feature.

Contracts support up to 32 cases and 16 phrases per list. Text fields are bounded
to 512 characters. Unsupported behavioral criteria fail validation. Static
skill-integrity lint also validates the pilot contract and runs its replay.

This implements versioned intent/scope/constraints, a real applicability consumer,
and deterministic edit-time regression checks with unchanged source on failure.
Broader outcome-based replay, model-mediated semantic validation, cross-model
benchmarks, and automatic migration of all skills are not implemented.
