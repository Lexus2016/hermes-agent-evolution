# Enterprise Approval Workflow Framework

## Overview

Implementation of approval workflow framework with four gate types, RACI mapping, SLA-defined escalation, and audit-trail schema for governance and compliance.

## Features

### Four Approval Gate Types

1. **Pre-Approval** - Approval required before execution
2. **Post-Approval** - Approval after execution (review/audit)
3. **Parallel Approval** - Multiple approvals in parallel
4. **Conditional Approval** - Approval based on conditions/parameters

### RACI Role-Based Governance

- **Responsible (R)** - Who does the work
- **Accountable (A)** - Who is ultimately answerable
- **Consulted (C)** - Who provides input
- **Informed (I)** - Who is kept in the loop

### SLA-Defined Escalation

- Configurable SLA deadlines per approval gate
- Automatic escalation on SLA breach
- Multi-level escalation paths
- Escalation notifications

### Immutable Audit Trail

- JSON-line based audit logging
- All events captured (created, approved, rejected, escalated)
- Operation-level filtering
- Immutable storage

## Usage

### Basic Setup

```python
from tools.approval_workflow import ApprovalWorkflowEngine, ApprovalGate, ApprovalGateType

# Initialize engine
engine = ApprovalWorkflowEngine()

# Define an approval gate
deploy_gate = ApprovalGate(
    gate_id="prod_deploy",
    name="Production Deployment",
    gate_type=ApprovalGateType.PRE,
    description="Approval required for production deployments",
    required_approvers=["ops_lead", "security"],
    sla_minutes=60,
    escalation_path=["manager", "director"],
)
engine.register_gate(deploy_gate)
```

### RACI-Based Governance

```python
# Define RACI mapping for operations
engine.define_raci_mapping(
    operation="database_migration",
    responsible=["db_admin"],
    accountable=["tech_lead"],
    consulted=["security_team"],
    informed=["product_manager"],
)

# Request approval (approvers determined from RACI map)
request = engine.create_approval_request(
    gate_id="migration_approval",
    operation="database_migration",
    requester="developer",
)
```

### Creating Approval Requests

```python
# Create a new approval request
request = engine.create_approval_request(
    gate_id="prod_deploy",
    operation="deploy_app_v2",
    requester="developer",
    metadata={
        "version": "2.0.0",
        "environment": "production",
        "change_log": "...",
    },
)

print(f"Request ID: {request.request_id}")
print(f"Status: {request.status}")
print(f"Approvers: {request.approvers}")
```

### Approving/Rejecting Requests

```python
# Approve
approved_request = engine.approve_request(
    request_id=request.request_id,
    approver="ops_lead",
    comment="Approved for production deployment",
)

# Reject
rejected_request = engine.reject_request(
    request_id=request.request_id,
    approver="security",
    comment="Security scan failed - fix vulnerabilities first",
)
```

### Checking Pending Requests

```python
# Get all pending requests for a user
pending = engine.get_pending_requests_for_user("ops_lead")
for request in pending:
    print(f"{request.operation} - {request.created_at}")
    print(f"Requester: {request.requester}")
    print(f"Metadata: {request.metadata}")
```

### SLA Monitoring

```python
# Check for SLA breaches
breached_requests = engine.check_sla_breaches()

for request in breached_requests:
    print(f"Request {request.request_id} exceeded SLA")
    print(f"Escalation level: {request.escalation_level}")
```

### Audit Trail

```python
# Read audit trail
trail = engine.get_audit_trail(operation="deploy_app_v2")

for entry in trail:
    print(f"{entry['timestamp']}: {entry['event_type']}")
    print(f"  User: {entry['user']}")
    print(f"  Details: {entry['details']}")
```

## Callbacks

### Request Notification

```python
def notify_approver(request):
    """Notify approver of pending approval."""
    send_message(
        to=request.approvers,
        subject=f"Approval Required: {request.operation}",
        body=f"Please review: {request.metadata}",
    )

engine.set_request_callback(notify_approver)
```

### Escalation Notification

```python
def notify_escalation(request, escalated_to):
    """Notify escalation target."""
    send_alert(
        to=escalated_to,
        subject=f"Escalated Approval: {request.operation}",
        body=f"SLA exceeded, level {request.escalation_level} escalation",
    )

engine.set_escalation_callback(notify_escalation)
```

## Approval Request Lifecycle

```
PENDING → (approver action)
    ├─→ APPROVED → Completed
    ├─→ REJECTED → Completed
    └─→ ESCALATED → (still pending, higher level)
         └─→ EXPIRED (if no response after max escalation)
```

## Configuration Examples

### Production Deployments

```python
prod_deploy = ApprovalGate(
    gate_id="prod_deploy",
    name="Production Deployment",
    gate_type=ApprovalGateType.PRE,
    required_approvers=["ops_lead", "security"],
    sla_minutes=60,
    escalation_path=["director", "cto"],
)
```

### Database Schema Changes

```python
schema_change = ApprovalGate(
    gate_id="schema_change",
    name="Schema Change",
    gate_type=ApprovalGateType.PRE,
    required_approvers=["db_admin", "tech_lead"],
    sla_minutes=30,
    escalation_path=["architect"],
)
```

### Cost-Incurring Operations

```python
cost_approval = ApprovalGate(
    gate_id="cost_approval",
    name="Cost Approval",
    gate_type=ApprovalGateType.PRE,
    required_approvers=["finance", "manager"],
    conditions={"max_cost": 1000},
    sla_minutes=120,
    escalation_path=["vp"],
)
```

## Integration with Existing Systems

### With Hermes Approval System

```python
# Bridge between new workflow engine and existing approval.py
from tools.approval_workflow import get_approval_engine

def bridge_approval_to_workflow(command, session_key):
    """Bridge existing approval system to workflow engine."""
    engine = get_approval_engine()
    
    # Determine which gate applies
    if "database" in command and "migrate" in command:
        gate_id = "schema_change"
    elif "deploy" in command and "prod" in command:
        gate_id = "prod_deploy"
    else:
        return None  # No workflow gate
    
    # Create request
    request = engine.create_approval_request(
        gate_id=gate_id,
        operation=command,
        requester=session_key,
    )
    return request
```

## Audit Trail Format

```json
{
  "timestamp": "2026-06-09T12:00:00",
  "event_type": "request_created",
  "request_id": "prod_deploy-deploy_app-1234567890",
  "gate_id": "prod_deploy",
  "operation": "deploy_app",
  "user": "developer",
  "details": {
    "approvers": ["ops_lead", "security"]
  }
}
```

## Event Types

- `gate_registered` - New approval gate registered
- `raci_defined` - RACI mapping defined for operation
- `request_created` - New approval request created
- `request_approved` - Request approved
- `request_rejected` - Request rejected
- `request_escalated` - Request escalated due to SLA breach
- `request_completed` - Request completed (approved/rejected)

## Compliance & Governance

### Regulatory Requirements

The framework supports compliance requirements for:

- **Finance** - SOX, financial controls
- **Healthcare** - HIPAA, access controls
- **Legal** - Documented approval chains

### Audit Trail Immutability

- Append-only JSON-line format
- No delete/update operations
- Timestamped entries
- Operation filtering for compliance reporting

### SLA Compliance

- Configurable SLA per operation
- Automated escalation on breach
- Audit trail of escalation events
- SLA breach reporting

## Best Practices

1. **Define clear RACI mappings** - Establish responsibility before enabling workflows
2. **Set appropriate SLAs** - Balance speed with oversight
3. **Configure escalation paths** - Ensure coverage for time-sensitive operations
4. **Monitor audit trail** - Regular review of approval patterns
5. **Test escalation** - Verify escalation paths work before production use
6. **Document gates** - Maintain clear descriptions of each gate's purpose

## Limitations

- Single approver sufficient for current implementation (can be extended for N-of-M)
- Escalation is automatic (manual escalation can be added)
- No approval delegation (approver transfers) - planned for future

## Future Enhancements

- N-of-M approval (require M of N approvers)
- Approval delegation (temporarily transfer approval rights)
- Conditional approval based on risk score
- Integration with external approval systems (Jira, ServiceNow)
- Approval metrics and reporting dashboard
- Webhook notifications for external systems

## References

Based on enterprise governance frameworks and regulatory compliance requirements.
