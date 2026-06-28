# OWASP catalogs referenced by AI-SAFE

Use these codes to tag every audit finding for traceability.

## OWASP LLM Top 10 (2025)
| Code | Name | Essence |
|---|---|---|
| LLM01 | Prompt Injection | Manipulating the LLM via crafted input → unauthorized access / leakage / decision compromise |
| LLM02 | Sensitive Information Disclosure | Leaking PII/secrets via model output |
| LLM03 | Supply Chain Vulnerabilities | Compromised components/services/datasets |
| LLM04 | Data and Model Poisoning | Malicious data in training / model modification |
| LLM05 | Improper Output Handling | Insufficient output validation → downstream exploits (incl. code execution) |
| LLM06 | Excessive Agency | Unbounded LLM autonomy → unintended actions |
| LLM07 | System Prompt Leakage | Leakage of system prompts holding internal logic/instructions |
| LLM08 | Vector and Embedding Weaknesses | Weaknesses in vectors/embeddings used in RAG (incl. data reconstruction) |
| LLM09 | Misinformation | Spreading false/inaccurate info, esp. under over-reliance |
| LLM10 | Unbounded Consumption | Uncontrolled resource use → DoS and unexpected cost |

## OWASP MCP (Model Context Protocol) Top 10
| Code | Name |
|---|---|
| MCP01 | Prompt Injection |
| MCP02 | Tool Poisoning |
| MCP03 | Privilege Abuse |
| MCP04 | Tool Shadowing & Shadow MCP |
| MCP05 | Indirect Prompt Injection |
| MCP06 | Sensitive Data Exposure & Token Theft |
| MCP07 | Command/SQL Injection & Malicious Code Execution |
| MCP08 | Rug Pull Attacks |
| MCP09 | Denial of Wallet/Service |
| MCP10 | Authentication Bypass |

## OWASP AI Agents (Agentic AI) Top 15
| Code | Name |
|---|---|
| T1 | Memory Poisoning |
| T2 | Tool Misuse |
| T3 | Privilege Compromise |
| T4 | Resource Overload |
| T5 | Cascading Hallucinations |
| T6 | Intent Breaking & Goal Manipulation |
| T7 | Misaligned & Deceptive Behaviors |
| T8 | Repudiation & Untraceability |
| T9 | Identity Spoofing & Impersonation |
| T10 | Overwhelming Human-in-the-Loop (HITL) |
| T11 | Supply Chain Attacks |
| T12 | AI Agents as Attack Tools |
| T13 | Authorization & Control Hijacking |
| T14 | Impact Chain & Blast Radius |
| T15 | Cross-Agent Communication Poisoning |

## RAG-specific threats
Vector Database Compromise · Access Control Failures in RAG · Embedding Inversion Attacks · Context Leakage Between Users · Knowledge Base Poisoning · Retrieval Manipulation · Indirect Prompt Injection via Documents · Data Federation Conflicts · Similarity Search Exploitation · Vector Database Resource Exhaustion.
