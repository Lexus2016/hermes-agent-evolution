# AI-SAFE Threat Matrix — 19 × YAISAFE.* with audit criteria

Severity derived from source `Likelihood × Impact`:
High×High = 🔴 CRITICAL · (any)×High = 🟠 HIGH · Med×Med = 🟡 MEDIUM · Med×Low = 🟢 LOW.

Each entry: **Control** (what must exist) · **Check** (how to verify) · **🚩 Red flags** · **OWASP**.

---

## INPUT — Interface (Input/Output)

### YAISAFE.INPUT.1 — Prompt Injection — 🔴 CRITICAL
- **Control:** input sanitization/validation; separation of system instructions vs data; injection-pattern detector.
- **Check:** is there a filtering layer before the LLM? Is user/external text concatenated straight into the system prompt? Is indirect injection (instruction inside email/doc/RAG) handled?
- **🚩** `prompt = SYSTEM_PROMPT + user_input`; external text into prompt unwrapped; no filter; no injection tests.
- **OWASP:** LLM01, MCP01, MCP05, T6, RAG: Indirect Prompt Injection.

### YAISAFE.INPUT.2 — Denial of Service — 🟡 MEDIUM
- **Control:** Rate Limiting, WAF, resource-consumption monitoring.
- **Check:** request-rate limit per user/IP/key? input-length & context-size caps? anomaly monitoring?
- **🚩** unbounded loops; no input-length cap; no rate limit at gateway/app.
- **OWASP:** LLM10, MCP09, T4, RAG: Resource Exhaustion.

### YAISAFE.INPUT.3 — Improper Output Handling — 🔴 CRITICAL
- **Control:** strict output validation/sanitization; typed schemas (Pydantic, JSON Schema); output encoding before web render.
- **Check:** does LLM output reach `eval`/`exec`/`subprocess`/SQL/`innerHTML` unchecked? schema-validated?
- **🚩** `eval(model_output)`; model string → SQL; output → HTML without escaping.
- **OWASP:** LLM05.

## EXEC — Execution & Tools

### YAISAFE.EXEC.1 — Tool Misuse — 🔴 CRITICAL
- **Control:** least privilege per tool; clear tool-purpose descriptions; Human Approval Gates for critical actions.
- **Check:** narrow scope per tool? irreversible/external actions (send/delete/pay) require human confirmation?
- **🚩** `send_email`/`delete`/`pay` called autonomously; tool broader than the task needs.
- **OWASP:** LLM06, T2.

### YAISAFE.EXEC.2 — Privilege Escalation — 🟠 HIGH
- **Control:** sandboxing for code execution (gVisor, Firecracker, containers); static analysis of generated code.
- **Check:** where does generated code run? network isolation? tool runs under least-privilege service account/IAM?
- **🚩** `subprocess.run(cmd, shell=True)`; code on host without sandbox; unrestricted net/FS access.
- **OWASP:** MCP03, T3.

### YAISAFE.EXEC.3 — Tool Poisoning — 🟠 HIGH
- **Control:** audit & integrity control of tool descriptions; separate data from instructions architecturally.
- **Check:** where do tool/MCP descriptions come from? integrity checked? can a description be read as an executable command?
- **🚩** tools/MCP from unverified sources; description injected into prompt unseparated; no change control on metadata.
- **OWASP:** MCP02, MCP04.

### YAISAFE.EXEC.4 — Auth Bypass & Impersonation — 🟠 HIGH
- **Control:** strong authN/authZ per tool call (OAuth2, mTLS); short-lived tokens; audit of all calls.
- **Check:** every tool call authorized? tokens short-lived? all calls logged with identity?
- **🚩** static/long-lived tokens; one shared token; no call log; identity unchecked per call.
- **OWASP:** MCP10, T9, T13.

## LOGIC — Reasoning & Planning

### YAISAFE.LOGIC.1 — Jailbreaking — 🟠 HIGH
- **Control:** improved-alignment models; Prompt Hardening; monitoring for bypass techniques.
- **Check:** system prompt hardened? bypass monitoring? Red Teaming performed?
- **🚩** weak/empty system prompt; no monitoring; un-aligned model.
- **OWASP:** LLM01, T7.

### YAISAFE.LOGIC.2 — Reasoning Collapse — 🟢 LOW
- **Control:** timeouts, Circuit Breakers, HITL for complex tasks, prompt simplification.
- **Check:** task-execution timeout? loop guard (max iterations)?
- **🚩** agent loop with no iteration/time cap; no exit on contradictory input.
- **OWASP:** LLM09, T5.

### YAISAFE.LOGIC.3 — Goal Manipulation — 🟠 HIGH
- **Control:** clear, unambiguous goals in system prompt; audit of Reasoning Traces.
- **Check:** goals/bounds fixed in system prompt? reasoning chains & decisions logged?
- **🚩** goal easily overwritten by input; no reasoning log; no action-vs-goal check.
- **OWASP:** LLM01, T6.

### YAISAFE.LOGIC.4 — Overwhelming HITL — 🟠 HIGH
- **Control:** adaptive HITL thresholds; grouping & prioritization of approval requests; honeypot requests.
- **Check:** is the human-approval process protected from flooding? critical requests prioritized?
- **🚩** operator gets every request unfiltered; no prioritization; uniform mass "approve" requests.
- **OWASP:** T10.

## INFRA — Infrastructure & Orchestration

### YAISAFE.INFRA.1 — Supply Chain Attacks — 🟠 HIGH
- **Control:** trusted repos; SCA & SAST scanning; SBOM; verify model digital signatures.
- **Check:** deps/images scanned (SCA/SAST)? SBOM present? model/base-image provenance & signatures verified?
- **🚩** `pip install` from unknown source; no version pinning/lockfile; no image scan; model loaded unverified.
- **OWASP:** LLM03, T11.

### YAISAFE.INFRA.2 — Resource Overload / Denial of Wallet — 🟡 MEDIUM
- **Control:** resource quotas/limits per agent/user; Circuit Breakers; hard budgets + alerts (Billing).
- **Check:** token/API quotas? hard budgets with alerts? breakers against call storms?
- **🚩** no spend cap; no per-agent quota; no budget-overrun alerts.
- **OWASP:** LLM10, MCP09, T4.

### YAISAFE.INFRA.3 — Cross-Agent Poisoning — 🟠 HIGH
- **Control:** agent isolation (network policy, VPC); input validation in EVERY agent; monitor inter-agent comms.
- **Check:** agents isolated? do agents trust each other's output unchecked? inter-agent traffic monitored? mTLS?
- **🚩** agents share env without segmentation; agent trusts another agent's output; no mTLS between agents.
- **OWASP:** T15, MCP05.

## DATA — Knowledge

### YAISAFE.DATA.1 — Knowledge Base Poisoning — 🟠 HIGH
- **Control:** access control to KB; data versioning; trusted sources; cryptographic integrity checks.
- **Check:** who can write to the KB? sources versioned? document integrity/provenance verified?
- **🚩** anyone can add a RAG doc; no versioning/source audit; no integrity check.
- **OWASP:** LLM04, T1, RAG: Knowledge Base Poisoning.

### YAISAFE.DATA.2 — Sensitive Data Disclosure — 🔴 CRITICAL
- **Control:** de-identify/mask data BEFORE the model; RBAC for RAG; fine-tuning to "forget" data.
- **Check:** PII masked before LLM/storage? role-based access to RAG chunks? output filtered for leaks?
- **🚩** secrets/keys in code or logs; PII into prompt unmasked; no RBAC on RAG; no output leak filter.
- **OWASP:** LLM02, LLM07, MCP06, RAG: Context Leakage.

### YAISAFE.DATA.3 — Retrieval Manipulation — 🟡 MEDIUM
- **Control:** hybrid search (vector + keyword); Reranker for relevance re-scoring.
- **Check:** vector-only search? relevance re-ranking? can a doc be artificially boosted?
- **🚩** pure top-k vector search; no reranker; easily gamed relevance.
- **OWASP:** LLM08, RAG: Retrieval Manipulation.

### YAISAFE.DATA.4 — Embedding Inversion — 🟠 HIGH
- **Control:** Differential Privacy when creating embeddings; granular access control to vector DB; anomalous-query detection.
- **Check:** who accesses the vector DB? embeddings protected? anomalous bulk queries detected?
- **🚩** vector DB open/unauth; embeddings unprotected; no anomaly detection.
- **OWASP:** LLM08, RAG: Embedding Inversion Attacks.

---

## Cross-cutting (from source conclusion)
- Data governance: cleaning, masking, license verification of training sets.
- CI/CD security; anomaly monitoring.
- Regular model testing & updates (drift, Red Teaming).
- Clear provider-vs-user responsibility split.
- Vulnerability tooling: SAST, DAST, SCA.
