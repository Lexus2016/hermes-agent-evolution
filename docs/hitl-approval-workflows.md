# Human-in-the-Loop Approval Workflows

## Overview

The Human-in-the-Loop (HITL) Approval Workflow system provides enterprise-grade governance controls for Hermes Agent operations. It implements approval gates, RACI mapping, SLA-defined escalation paths, and immutable audit trails.

Research Reference:
    https://www.digitalapplied.com/blog/agentic-workflow-approval-gate-framework-governance

## Features

- **Four Gate Types**: Pre, Post, Parallel, Conditional approval gates
- **RACI Mapping**: Role-based approvals (Responsible, Accountable, Consulted, Informed)
- **SLA Escalation**: Automatic escalation on SLA breach
- **Audit Trail**: Immutable logging of all approval decisions
- **Configurable Workflows**: Custom workflows per operation type
- **Compliance Support**: Meets regulatory requirements for sensitive operations

## Gate Types

| Gate Type | Description | Example |
|-----------|-------------|---------|
| **Pre** | Approval required before execution | File write permissions |
| **Post** | Review required after execution | Code change verification |
| **Parallel** | Monitor during execution | Security team oversight |
| **Conditional** | Based on risk assessment | Dynamic approval routing |

## Default Workflows

### High-Risk Operations
- **Applies to**: `write_file`, `patch`, `terminal`, `delete`, `git_push`
- **Gates**: Pre-approval (60 min SLA), Post-review (24h SLA)
- **Escalation**: admin, security-team

### Medium-Risk Operations
- **Applies to**: `web_search`, `web_extract`, `search_files`, `read_file`
- **Gates**: Pre-approval (120 min SLA, auto-approve after SLA)

### Critical Operations
- **Applies to**: `credential_access`, `security_config`, `permission_change`
- **Gates**: Two-person pre-approval, Parallel monitoring, Post-verification
- **Escalation**: admin, cto

## Usage

### Basic Operation Check

```python
from tools.approval_workflow import check_operation_approval

# Check if operation requires approval
can_proceed, request_id = check_operation_approval(
    operation="write_file",
    operation_details={"path": "/etc/config.yaml"},
    requester="user123",
)

if not can_proceed:
    print(f"Approval required. Request ID: {request_id}")
else:
    print("Operation can proceed without approval")
```

### Approving Requests

```python
from tools.approval_workflow import get_approval_system

system = get_approval_system()

# Approve a request
system.approve_request(
    request_id="req-abc123",
    approver="admin_user",
    reason="Operation approved as requested",
)

# Or deny
system.deny_request(
    request_id="req-abc123",
    denier="admin_user",
    reason="Insufficient justification for access",
)
```

### Creating Custom Workflows

```python
from tools.approval_workflow import (
    get_approval_system,
    ApprovalWorkflow,
    ApprovalGate,
    GateType,
    Role,
)

system = get_approval_system()

# Define a custom workflow
custom_workflow = ApprovalWorkflow(
    id="database-ops",
    name="Database Operations Approval",
    description="Approval workflow for database modifications",
    gates=[
        ApprovalGate(
            id="db-pre-approval",
            name="DB Pre-Approval",
            gate_type=GateType.PRE,
            description="DBA approval required before database changes",
            required_roles={Role.ACCOUNTABLE},
            sla_minutes=30,
            auto_approve_after_sla=False,
            escalation_path=["db-team-lead", "cto"],
        ),
    ],
    applicable_operations={"db_query", "db_update", "db_schema_change"},
    risk_threshold="high",
)

system.add_workflow(custom_workflow)
```

### Audit Trail

```python
# Get audit trail for a specific request
audit_entries = system.get_audit_trail(request_id="req-abc123")

for entry in audit_entries:
    print(f"{entry.event_type} by {entry.actor} at {entry.timestamp}")
    print(f"Details: {entry.details}")
```

### System Statistics

```python
from tools.approval_workflow import get_approval_summary

summary = get_approval_summary()
print(summary)
```

Output:
```
=== Approval System Summary ===
Total Workflows: 4
Total Requests: 15
Pending: 3
Approved: 10
Denied: 2
Escalated: 0
Audit Entries: 30

Pending Requests:
  req-001: write_file (requested by user123)
  req-002: patch (requested by dev456)
  req-003: terminal (requested by ops789)
```

## RACI Roles

| Role | Responsibility | Approval Power |
|------|----------------|----------------|
| **Responsible** | Doer of the work | Can approve own work in some contexts |
| **Accountable** | Final authority | Full approval authority |
| **Consulted** | Subject matter expert | Advisory role, no veto |
| **Informed** | Stakeholder notification | No approval authority |

## SLA Management

### SLA Breach Behavior

When SLA is breached:

1. **Auto-approve**: If enabled, operation proceeds automatically
2. **Escalate**: If escalation path defined, notify next-level approvers
3. **Audit**: Event logged with SLA breach flag

### Configuring SLAs

```python
ApprovalGate(
    id="strict-approval",
    name="Strict Approval",
    gate_type=GateType.PRE,
    required_roles={Role.ACCOUNTABLE},
    sla_minutes=60,              # 1 hour SLA
    auto_approve_after_sla=False,  # Never auto-approve
    escalation_path=["admin", "cto"],  # Escalation chain
)
```

## Compliance Features

### Audit Trail

All approval events are logged:
- Request creation
- Approvals
- Denials
- Escalations
- SLA breaches

Audit entries include:
- Timestamp
- Actor (user/system)
- Event type
- Full details

### Immutable Logging

Configure persistent audit log:

```python
from pathlib import Path
from tools.approval_workflow import get_approval_system

system = get_approval_system(
    audit_log_path=Path("/var/log/hermes/approvals.jsonl")
)
```

## Integration with Hermes

### Automatic Integration

The approval system can be integrated into tool execution:

```python
# In agent/agent_runtime_helpers.py or similar
from tools.approval_workflow import check_operation_approval

def execute_tool_with_approval(operation, details):
    can_proceed, request_id = check_operation_approval(
        operation=operation,
        operation_details=details,
    )
    
    if not can_proceed:
        return f"Approval required. Request ID: {request_id}. Please wait for approval."
    
    # Proceed with tool execution
    return execute_tool(operation, details)
```

## Testing

Run the approval workflow tests:

```bash
python -m pytest tests/approval_workflow/test_approval_workflow.py -v
```

## Best Practices

1. **Define Workflows Early**: Set up workflows before deploying to production
2. **Monitor SLAs**: Regularly review approval times and adjust SLAs
3. **Audit Regularly**: Review audit trail for compliance
4. **Test Escalations**: Verify escalation paths work correctly
5. **Document Policies**: Clearly document approval requirements

## Use Cases

### Finance

- Transaction approval workflows
- Account modification gates
- Audit trail for regulatory compliance

### Healthcare

- Patient data access controls
- Treatment plan approvals
- HIPAA compliance logging

### Legal

- Document approval chains
- Contract review workflows
- Privilege management

## Security Considerations

1. **Role Mapping**: Ensure roles map correctly to organizational structure
2. **Escalation Paths**: Verify escalation chains are appropriate
3. **Audit Protection**: Protect audit logs from tampering
4. **Request Validation**: Validate all request parameters

## Troubleshooting

### Requests Not Requiring Approval

Check if:
- Operation is in workflow's `applicable_operations`
- Workflow is `enabled`
- Pre-gate exists in workflow definition

### SLA Breaches Not Handling

Verify:
- Gate has `sla_minutes` defined
- `auto_approve_after_sla` or `escalation_path` is set
- System is checking expired requests regularly

### Audit Logs Not Writing

Ensure:
- `audit_log_path` is writable
- Directory exists
- Sufficient disk space

## Future Enhancements

Potential improvements:
1. Web-based approval interface
2. Multi-signature approvals
3. Delegated approval authority
4. Conditional routing based on risk scores
5. Integration with external approval systems (ServiceNow, Jira)

## Research Reference

This implementation is inspired by:
- **Agentic Workflow Approval Gate Framework**: https://www.digitalapplied.com/blog/agentic-workflow-approval-gate-framework-governance
- **RACI Matrix**: Industry-standard responsibility assignment
- **SLA Management**: ITIL-based service level agreements

## Implementation

- **Core Module**: `tools/approval_workflow.py`
- **Tests**: `tests/approval_workflow/test_approval_workflow.py`
- **Documentation**: This file
