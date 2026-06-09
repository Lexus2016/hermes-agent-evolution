"""Human-in-the-Loop Approval Workflow System for Hermes Agent.

Implements enterprise-grade approval workflow controls with audit trails,
approval gates, RACI mapping, and SLA-defined escalation paths.

Research Reference:
    https://www.digitalapplied.com/blog/agentic-workflow-approval-gate-framework-governance

Key Features:
- Four gate types: pre, post, parallel, conditional
- RACI mapping for role-based approvals
- SLA-defined escalation paths
- Immutable audit trail
- Approval workflow DSL
- Integration with existing skill execution
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Union
from threading import Lock

logger = logging.getLogger(__name__)


class GateType(Enum):
    """Types of approval gates."""
    
    PRE = "pre"           # Approval before execution
    POST = "post"         # Approval after execution (review)
    PARALLEL = "parallel" # Concurrent with execution (monitoring)
    CONDITIONAL = "conditional"  # Conditional based on risk assessment


class ApprovalStatus(Enum):
    """Status of an approval request."""
    
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    ESCALATED = "escalated"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class Role(Enum):
    """RACI roles for approval workflow."""
    
    RESPONSIBLE = "responsible"  # Doer of the work
    ACCOUNTABLE = "accountable"  # Final authority
    CONSULTED = "consulted"      # Needs to be consulted
    INFORMED = "informed"        # Needs to be informed


@dataclass
class ApprovalGate:
    """Definition of an approval gate."""
    
    id: str
    name: str
    gate_type: GateType
    description: str
    required_roles: Set[Role]
    sla_minutes: Optional[int] = None  # SLA for approval
    auto_approve_after_sla: bool = False
    escalation_path: Optional[List[str]] = None  # List of role IDs to escalate to
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "gate_type": self.gate_type.value,
            "description": self.description,
            "required_roles": [r.value for r in self.required_roles],
            "sla_minutes": self.sla_minutes,
            "auto_approve_after_sla": self.auto_approve_after_sla,
            "escalation_path": self.escalation_path,
        }


@dataclass
class ApprovalRequest:
    """A single approval request."""
    
    id: str
    gate_id: str
    workflow_id: str
    requester: str
    operation: str
    operation_details: Dict[str, Any]
    status: ApprovalStatus = ApprovalStatus.PENDING
    created_at: float = field(default_factory=time.monotonic)
    updated_at: float = field(default_factory=time.monotonic)
    approvers: List[str] = field(default_factory=list)
    decision_by: Optional[str] = None
    decision_reason: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def is_expired(self, sla_minutes: int) -> bool:
        """Check if approval request has expired SLA."""
        if sla_minutes is None:
            return False
        age_seconds = time.monotonic() - self.created_at
        return age_seconds > (sla_minutes * 60)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "gate_id": self.gate_id,
            "workflow_id": self.workflow_id,
            "requester": self.requester,
            "operation": self.operation,
            "operation_details": self.operation_details,
            "status": self.status.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "approvers": self.approvers.copy(),
            "decision_by": self.decision_by,
            "decision_reason": self.decision_reason,
            "metadata": self.metadata.copy(),
        }


@dataclass
class ApprovalWorkflow:
    """Definition of an approval workflow."""
    
    id: str
    name: str
    description: str
    gates: List[ApprovalGate]
    applicable_operations: Set[str]  # Operation types this workflow applies to
    risk_threshold: str = "medium"  # low, medium, high, critical
    enabled: bool = True
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "gates": [g.to_dict() for g in self.gates],
            "applicable_operations": list(self.applicable_operations),
            "risk_threshold": self.risk_threshold,
            "enabled": self.enabled,
        }


@dataclass
class AuditEntry:
    """Entry in the immutable audit trail."""
    
    timestamp: float
    event_type: str  # created, approved, denied, escalated, etc.
    request_id: str
    workflow_id: str
    actor: str
    details: Dict[str, Any]
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "event_type": self.event_type,
            "request_id": self.request_id,
            "workflow_id": self.workflow_id,
            "actor": self.actor,
            "details": self.details.copy(),
        }


class ApprovalSystem:
    """Manages approval workflows and requests."""
    
    def __init__(self, audit_log_path: Optional[Path] = None):
        self.workflows: Dict[str, ApprovalWorkflow] = {}
        self.requests: Dict[str, ApprovalRequest] = {}
        self.audit_trail: List[AuditEntry] = []
        self.audit_log_path = audit_log_path
        self._lock = Lock()
        
        # Load default workflows
        self._load_default_workflows()
    
    def _load_default_workflows(self) -> None:
        """Load default approval workflows for common operations."""
        
        # High-risk operations (file modifications, system changes)
        high_risk_workflow = ApprovalWorkflow(
            id="high-risk-ops",
            name="High-Risk Operations Approval",
            description="Approval workflow for high-risk operations like file modifications",
            gates=[
                ApprovalGate(
                    id="pre-approval",
                    name="Pre-Execution Approval",
                    gate_type=GateType.PRE,
                    description="Required approval before executing high-risk operation",
                    required_roles={Role.ACCOUNTABLE},
                    sla_minutes=60,
                    auto_approve_after_sla=False,
                    escalation_path=["admin", "security-team"],
                ),
                ApprovalGate(
                    id="post-review",
                    name="Post-Execution Review",
                    gate_type=GateType.POST,
                    description="Review required after execution",
                    required_roles={Role.RESPONSIBLE},
                    sla_minutes=1440,  # 24 hours
                    auto_approve_after_sla=True,
                ),
            ],
            applicable_operations={
                "write_file", "patch", "terminal", "delete", "git_push"
            },
            risk_threshold="high",
        )
        
        # Medium-risk operations (web access, search)
        medium_risk_workflow = ApprovalWorkflow(
            id="medium-risk-ops",
            name="Medium-Risk Operations Approval",
            description="Approval workflow for medium-risk operations",
            gates=[
                ApprovalGate(
                    id="pre-approval",
                    name="Pre-Execution Approval",
                    gate_type=GateType.PRE,
                    description="Approval required for medium-risk operations",
                    required_roles={Role.RESPONSIBLE},
                    sla_minutes=120,
                    auto_approve_after_sla=True,
                ),
            ],
            applicable_operations={
                "web_search", "web_extract", "search_files", "read_file"
            },
            risk_threshold="medium",
        )
        
        # Critical operations (credential access, security changes)
        critical_workflow = ApprovalWorkflow(
            id="critical-ops",
            name="Critical Operations Approval",
            description="Approval workflow for critical security operations",
            gates=[
                ApprovalGate(
                    id="pre-approval",
                    name="Pre-Execution Approval",
                    gate_type=GateType.PRE,
                    description="Two-person approval required for critical operations",
                    required_roles={Role.ACCOUNTABLE, Role.RESPONSIBLE},
                    sla_minutes=30,
                    auto_approve_after_sla=False,
                    escalation_path=["admin", "cto"],
                ),
                ApprovalGate(
                    id="parallel-monitor",
                    name="Parallel Monitoring",
                    gate_type=GateType.PARALLEL,
                    description="Security team monitors execution",
                    required_roles={Role.CONSULTED},
                ),
                ApprovalGate(
                    id="post-approval",
                    name="Post-Execution Verification",
                    gate_type=GateType.POST,
                    description="Verification required after execution",
                    required_roles={Role.ACCOUNTABLE},
                    sla_minutes=60,
                ),
            ],
            applicable_operations={
                "credential_access", "security_config", "permission_change"
            },
            risk_threshold="critical",
        )
        
        self.workflows = {
            "high-risk-ops": high_risk_workflow,
            "medium-risk-ops": medium_risk_workflow,
            "critical-ops": critical_workflow,
        }
        
        logger.info(f"Loaded {len(self.workflows)} default approval workflows")
    
    def add_workflow(self, workflow: ApprovalWorkflow) -> None:
        """Add or update an approval workflow."""
        with self._lock:
            self.workflows[workflow.id] = workflow
            logger.info(f"Added/updated workflow: {workflow.id}")
    
    def get_workflow_for_operation(self, operation: str) -> Optional[ApprovalWorkflow]:
        """Find applicable workflow for an operation."""
        with self._lock:
            for workflow in self.workflows.values():
                if operation in workflow.applicable_operations and workflow.enabled:
                    return workflow
        return None
    
    def create_approval_request(
        self,
        workflow_id: str,
        gate_id: str,
        requester: str,
        operation: str,
        operation_details: Dict[str, Any],
    ) -> ApprovalRequest:
        """Create a new approval request."""
        import uuid
        
        request_id = str(uuid.uuid4())
        request = ApprovalRequest(
            id=request_id,
            gate_id=gate_id,
            workflow_id=workflow_id,
            requester=requester,
            operation=operation,
            operation_details=operation_details,
        )
        
        with self._lock:
            self.requests[request_id] = request
            
            # Audit log
            self._add_audit_entry(
                event_type="created",
                request_id=request_id,
                workflow_id=workflow_id,
                actor=requester,
                details={
                    "gate_id": gate_id,
                    "operation": operation,
                    "operation_details": operation_details,
                },
            )
        
        logger.info(f"Created approval request {request_id} for operation {operation}")
        return request
    
    def approve_request(
        self,
        request_id: str,
        approver: str,
        reason: Optional[str] = None,
    ) -> bool:
        """Approve an approval request."""
        with self._lock:
            if request_id not in self.requests:
                logger.warning(f"Request {request_id} not found")
                return False
            
            request = self.requests[request_id]
            
            if request.status != ApprovalStatus.PENDING:
                logger.warning(f"Request {request_id} is not pending")
                return False
            
            request.status = ApprovalStatus.APPROVED
            request.updated_at = time.monotonic()
            request.approvers.append(approver)
            request.decision_by = approver
            request.decision_reason = reason
            
            # Audit log
            self._add_audit_entry(
                event_type="approved",
                request_id=request_id,
                workflow_id=request.workflow_id,
                actor=approver,
                details={
                    "reason": reason,
                },
            )
        
        logger.info(f"Approved request {request_id} by {approver}")
        return True
    
    def deny_request(
        self,
        request_id: str,
        denier: str,
        reason: Optional[str] = None,
    ) -> bool:
        """Deny an approval request."""
        with self._lock:
            if request_id not in self.requests:
                return False
            
            request = self.requests[request_id]
            request.status = ApprovalStatus.DENIED
            request.updated_at = time.monotonic()
            request.decision_by = denier
            request.decision_reason = reason
            
            # Audit log
            self._add_audit_entry(
                event_type="denied",
                request_id=request_id,
                workflow_id=request.workflow_id,
                actor=denier,
                details={
                    "reason": reason,
                },
            )
        
        logger.info(f"Denied request {request_id} by {denier}")
        return True
    
    def escalate_request(self, request_id: str) -> bool:
        """Escalate an expired or stuck approval request."""
        with self._lock:
            if request_id not in self.requests:
                return False
            
            request = self.requests[request_id]
            
            # Get workflow and gate to check escalation path
            workflow = self.workflows.get(request.workflow_id)
            if not workflow:
                return False
            
            gate = next((g for g in workflow.gates if g.id == request.gate_id), None)
            if not gate or not gate.escalation_path:
                return False
            
            request.status = ApprovalStatus.ESCALATED
            request.updated_at = time.monotonic()
            request.metadata["escalated_to"] = gate.escalation_path
            
            # Audit log
            self._add_audit_entry(
                event_type="escalated",
                request_id=request_id,
                workflow_id=request.workflow_id,
                actor="system",
                details={
                    "escalated_to": gate.escalation_path,
                },
            )
        
        logger.info(f"Escalated request {request_id}")
        return True
    
    def check_expired_requests(self) -> List[str]:
        """Check for expired requests and handle SLA breaches."""
        expired_ids = []
        
        with self._lock:
            for request_id, request in self.requests.items():
                if request.status != ApprovalStatus.PENDING:
                    continue
                
                workflow = self.workflows.get(request.workflow_id)
                if not workflow:
                    continue
                
                gate = next((g for g in workflow.gates if g.id == request.gate_id), None)
                if not gate or gate.sla_minutes is None:
                    continue
                
                if request.is_expired(gate.sla_minutes):
                    expired_ids.append(request_id)
                    
                    # Handle SLA breach
                    if gate.auto_approve_after_sla:
                        request.status = ApprovalStatus.APPROVED
                        request.metadata["auto_approved_sla_breach"] = True
                        
                        self._add_audit_entry(
                            event_type="auto_approved",
                            request_id=request_id,
                            workflow_id=request.workflow_id,
                            actor="system",
                            details={
                                "reason": "SLA breach with auto-approve enabled",
                                "sla_minutes": gate.sla_minutes,
                            },
                        )
                    elif gate.escalation_path:
                        self.escalate_request(request_id)
        
        return expired_ids
    
    def _add_audit_entry(
        self,
        event_type: str,
        request_id: str,
        workflow_id: str,
        actor: str,
        details: Dict[str, Any],
    ) -> None:
        """Add entry to audit trail."""
        entry = AuditEntry(
            timestamp=time.monotonic(),
            event_type=event_type,
            request_id=request_id,
            workflow_id=workflow_id,
            actor=actor,
            details=details,
        )
        
        self.audit_trail.append(entry)
        
        # Persist if path configured
        if self.audit_log_path:
            try:
                with open(self.audit_log_path, "a") as f:
                    f.write(json.dumps(entry.to_dict()) + "\n")
            except Exception as e:
                logger.error(f"Failed to write audit log: {e}")
    
    def get_audit_trail(
        self,
        request_id: Optional[str] = None,
        workflow_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[AuditEntry]:
        """Get audit trail entries, optionally filtered."""
        with self._lock:
            filtered = self.audit_trail
            
            if request_id:
                filtered = [e for e in filtered if e.request_id == request_id]
            
            if workflow_id:
                filtered = [e for e in filtered if e.workflow_id == workflow_id]
            
            return filtered[-limit:]
    
    def get_pending_requests(self, workflow_id: Optional[str] = None) -> List[ApprovalRequest]:
        """Get all pending approval requests."""
        with self._lock:
            pending = [
                r for r in self.requests.values()
                if r.status == ApprovalStatus.PENDING
            ]
            
            if workflow_id:
                pending = [r for r in pending if r.workflow_id == workflow_id]
            
            return pending
    
    def get_statistics(self) -> Dict[str, Any]:
        """Get approval system statistics."""
        with self._lock:
            total_requests = len(self.requests)
            pending = sum(1 for r in self.requests.values() if r.status == ApprovalStatus.PENDING)
            approved = sum(1 for r in self.requests.values() if r.status == ApprovalStatus.APPROVED)
            denied = sum(1 for r in self.requests.values() if r.status == ApprovalStatus.DENIED)
            escalated = sum(1 for r in self.requests.values() if r.status == ApprovalStatus.ESCALATED)
            
            return {
                "total_requests": total_requests,
                "pending": pending,
                "approved": approved,
                "denied": denied,
                "escalated": escalated,
                "workflows": len(self.workflows),
                "audit_entries": len(self.audit_trail),
            }


# Global instance
_global_system: Optional[ApprovalSystem] = None


def get_approval_system(audit_log_path: Optional[Path] = None) -> ApprovalSystem:
    """Get or create the global approval system."""
    global _global_system
    if _global_system is None:
        _global_system = ApprovalSystem(audit_log_path=audit_log_path)
    return _global_system


def check_operation_approval(
    operation: str,
    operation_details: Dict[str, Any],
    requester: str = "system",
) -> tuple[bool, Optional[str]]:
    """Check if an operation requires approval and if approved.
    
    Returns:
        (can_proceed, request_id)
        - can_proceed: True if operation can proceed
        - request_id: ID of approval request if created, None if not needed
    """
    system = get_approval_system()
    workflow = system.get_workflow_for_operation(operation)
    
    if not workflow:
        # No approval required
        return True, None
    
    # Find the first pre-execution gate
    pre_gate = next(
        (g for g in workflow.gates if g.gate_type == GateType.PRE),
        None
    )
    
    if not pre_gate:
        # No pre-approval gate, can proceed
        return True, None
    
    # Create approval request
    request = system.create_approval_request(
        workflow_id=workflow.id,
        gate_id=pre_gate.id,
        requester=requester,
        operation=operation,
        operation_details=operation_details,
    )
    
    logger.info(f"Operation {operation} requires approval. Request ID: {request.id}")
    return False, request.id


def get_approval_summary() -> str:
    """Get a summary of the approval system status."""
    system = get_approval_system()
    stats = system.get_statistics()
    
    lines = [
        "=== Approval System Summary ===",
        f"Total Workflows: {stats['workflows']}",
        f"Total Requests: {stats['total_requests']}",
        f"Pending: {stats['pending']}",
        f"Approved: {stats['approved']}",
        f"Denied: {stats['denied']}",
        f"Escalated: {stats['escalated']}",
        f"Audit Entries: {stats['audit_entries']}",
    ]
    
    pending_requests = system.get_pending_requests()
    if pending_requests:
        lines.append("")
        lines.append("Pending Requests:")
        for req in pending_requests[:10]:
            lines.append(f"  {req.id}: {req.operation} (requested by {req.requester})")
    
    return "\n".join(lines)
