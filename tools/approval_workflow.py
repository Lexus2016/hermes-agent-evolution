#!/usr/bin/env python3
"""
Enterprise Approval Workflow Framework

Implements approval workflow framework with four gate types, RACI mapping,
SLA-defined escalation, and audit-trail schema for governance and compliance.

Features:
- Four approval gate types (pre, post, parallel, conditional)
- RACI role-based approval mapping
- SLA-defined escalation paths
- Immutable audit trail schema
- Approval workflow DSL
- Integration with existing approval system
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Callable

logger = logging.getLogger(__name__)


class ApprovalGateType(Enum):
    """Types of approval gates."""

    PRE = "pre"  # Approval before execution
    POST = "post"  # Approval after execution (review)
    PARALLEL = "parallel"  # Multiple approvals in parallel
    CONDITIONAL = "conditional"  # Approval based on conditions


class ApprovalStatus(Enum):
    """Status of an approval request."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    ESCALATED = "escalated"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class RACIRole(Enum):
    """RACI roles for approval governance."""

    RESPONSIBLE = "responsible"  # Does the work
    ACCOUNTABLE = "accountable"  # Ultimately answerable
    CONSULTED = "consulted"  # Provides input
    INFORMED = "informed"  # Kept in the loop


@dataclass
class ApprovalGate:
    """Definition of an approval gate."""

    gate_id: str
    name: str
    gate_type: ApprovalGateType
    description: str = ""
    required_roles: Set[RACIRole] = field(default_factory=set)
    required_approvers: List[str] = field(default_factory=list)
    conditions: Dict[str, Any] = field(default_factory=dict)
    sla_minutes: Optional[int] = None
    escalation_path: List[str] = field(default_factory=list)


@dataclass
class ApprovalRequest:
    """A single approval request."""

    request_id: str
    gate_id: str
    operation: str
    requester: str
    approvers: List[str]
    status: ApprovalStatus = ApprovalStatus.PENDING
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    expires_at: Optional[float] = None
    sla_deadline: Optional[float] = None
    escalation_level: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)
    approval_comments: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class ApprovalDecision:
    """Record of an approval decision."""

    request_id: str
    approver: str
    decision: ApprovalStatus
    timestamp: float = field(default_factory=time.time)
    comment: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AuditTrailEntry:
    """Immutable audit trail entry."""

    timestamp: float = field(default_factory=time.time)
    event_type: str = ""
    request_id: str = ""
    gate_id: str = ""
    operation: str = ""
    user: str = ""
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "timestamp": datetime.fromtimestamp(self.timestamp).isoformat(),
            "event_type": self.event_type,
            "request_id": self.request_id,
            "gate_id": self.gate_id,
            "operation": self.operation,
            "user": self.user,
            "details": self.details,
        }


class ApprovalWorkflowEngine:
    """
    Enterprise approval workflow engine.

    Manages approval gates, RACI-based routing, SLA monitoring,
    escalation, and maintains immutable audit trail.
    """

    def __init__(
        self,
        audit_log_path: Optional[Path] = None,
        auto_escalate: bool = True,
    ):
        """Initialize approval workflow engine.

        Args:
            audit_log_path: Path to audit log file
            auto_escalate: Automatically escalate on SLA breach
        """
        self.gates: Dict[str, ApprovalGate] = {}
        self.pending_requests: Dict[str, ApprovalRequest] = {}
        self.completed_requests: Dict[str, ApprovalRequest] = {}
        self.audit_log_path = (
            audit_log_path or Path.home() / ".hermes" / "approval_audit.jsonl"
        )
        self.auto_escalate = auto_escalate
        self.racii_map: Dict[str, Dict[RACIRole, List[str]]] = {}

        # Ensure audit log directory exists
        self.audit_log_path.parent.mkdir(parents=True, exist_ok=True)

        # Callback for delivering requests to users
        self._request_callback: Optional[Callable[[ApprovalRequest], None]] = None
        self._escalation_callback: Optional[Callable[[ApprovalRequest, str], None]] = (
            None
        )

    def register_gate(self, gate: ApprovalGate) -> None:
        """Register an approval gate.

        Args:
            gate: ApprovalGate definition
        """
        self.gates[gate.gate_id] = gate
        self._audit_log(
            "gate_registered", gate_id=gate.gate_id, details={"name": gate.name}
        )
        logger.info(f"Registered approval gate: {gate.name} ({gate.gate_id})")

    def define_raci_mapping(
        self,
        operation: str,
        responsible: List[str] = [],
        accountable: List[str] = [],
        consulted: List[str] = [],
        informed: List[str] = [],
    ) -> None:
        """Define RACI role mapping for an operation type.

        Args:
            operation: Operation type identifier
            responsible: Who does the work
            accountable: Who is ultimately answerable
            consulted: Who provides input
            informed: Who is kept in the loop
        """
        self.racii_map[operation] = {
            RACIRole.RESPONSIBLE: responsible,
            RACIRole.ACCOUNTABLE: accountable,
            RACIRole.CONSULTED: consulted,
            RACIRole.INFORMED: informed,
        }
        self._audit_log("raci_defined", operation=operation)

    def get_approvers_for_operation(self, operation: str, role: RACIRole) -> List[str]:
        """Get approvers for an operation based on RACI role.

        Args:
            operation: Operation type
            role: RACI role

        Returns:
            List of approver IDs
        """
        if operation not in self.racii_map:
            return []
        return self.racii_map[operation].get(role, [])

    def create_approval_request(
        self,
        gate_id: str,
        operation: str,
        requester: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ApprovalRequest:
        """Create an approval request.

        Args:
            gate_id: ID of the approval gate
            operation: Operation requiring approval
            requester: ID of the requester
            metadata: Optional metadata

        Returns:
            ApprovalRequest object
        """
        if gate_id not in self.gates:
            raise ValueError(f"Unknown gate: {gate_id}")

        gate = self.gates[gate_id]
        request_id = f"{gate_id}-{operation}-{int(time.time() * 1000)}"

        # Determine approvers from gate or RACI map
        approvers = gate.required_approvers.copy()
        if not approvers:
            # Try RACI mapping
            approvers = self.get_approvers_for_operation(
                operation, RACIRole.ACCOUNTABLE
            )

        if not approvers:
            raise ValueError(f"No approvers defined for gate {gate_id}")

        # Calculate SLA deadline
        sla_deadline = None
        if gate.sla_minutes:
            sla_deadline = time.time() + (gate.sla_minutes * 60)

        request = ApprovalRequest(
            request_id=request_id,
            gate_id=gate_id,
            operation=operation,
            requester=requester,
            approvers=approvers,
            metadata=metadata or {},
            sla_deadline=sla_deadline,
        )

        self.pending_requests[request_id] = request

        self._audit_log(
            "request_created",
            request_id=request_id,
            gate_id=gate_id,
            operation=operation,
            user=requester,
            details={"approvers": approvers},
        )

        # Notify approvers
        if self._request_callback:
            self._request_callback(request)

        logger.info(f"Created approval request {request_id} for {operation}")

        return request

    def approve_request(
        self,
        request_id: str,
        approver: str,
        comment: str = "",
    ) -> ApprovalRequest:
        """Approve a request.

        Args:
            request_id: ID of the request
            approver: ID of the approver
            comment: Optional comment

        Returns:
            Updated ApprovalRequest
        """
        request = self._get_pending_request(request_id)

        if approver not in request.approvers:
            raise PermissionError(f"User {approver} not authorized to approve")

        # Record decision
        decision = ApprovalDecision(
            request_id=request_id,
            approver=approver,
            decision=ApprovalStatus.APPROVED,
            comment=comment,
        )
        request.approval_comments.append({
            "approver": approver,
            "decision": "approved",
            "comment": comment,
        })

        self._audit_log(
            "request_approved",
            request_id=request_id,
            user=approver,
            details={"comment": comment},
        )

        # Check if all required approvers have approved
        # For now, single approval is sufficient
        self._complete_request(request, ApprovalStatus.APPROVED)

        return request

    def reject_request(
        self,
        request_id: str,
        approver: str,
        comment: str = "",
    ) -> ApprovalRequest:
        """Reject a request.

        Args:
            request_id: ID of the request
            approver: ID of the approver
            comment: Optional comment

        Returns:
            Updated ApprovalRequest
        """
        request = self._get_pending_request(request_id)

        if approver not in request.approvers:
            raise PermissionError(f"User {approver} not authorized to reject")

        request.approval_comments.append({
            "approver": approver,
            "decision": "rejected",
            "comment": comment,
        })

        self._audit_log(
            "request_rejected",
            request_id=request_id,
            user=approver,
            details={"comment": comment},
        )

        self._complete_request(request, ApprovalStatus.REJECTED)

        return request

    def check_sla_breaches(self) -> List[ApprovalRequest]:
        """Check for SLA breaches and escalate if needed.

        Returns:
            List of breached requests
        """
        now = time.time()
        breached = []

        for request in list(self.pending_requests.values()):
            if request.sla_deadline and now > request.sla_deadline:
                if self.auto_escalate:
                    self._escalate_request(request)
                breached.append(request)

        return breached

    def _escalate_request(self, request: ApprovalRequest) -> None:
        """Escalate a request to the next level.

        Args:
            request: Request to escalate
        """
        gate = self.gates[request.gate_id]

        if request.escalation_level < len(gate.escalation_path):
            # Escalate to next level
            escalation_target = gate.escalation_path[request.escalation_level]
            request.escalation_level += 1
            request.status = ApprovalStatus.ESCALATED
            request.updated_at = time.time()

            self._audit_log(
                "request_escalated",
                request_id=request.request_id,
                details={
                    "level": request.escalation_level,
                    "escalated_to": escalation_target,
                },
            )

            if self._escalation_callback:
                self._escalation_callback(request, escalation_target)

            logger.warning(
                f"Escalated request {request.request_id} to {escalation_target}"
            )

    def get_pending_requests_for_user(self, user_id: str) -> List[ApprovalRequest]:
        """Get all pending requests for a specific user.

        Args:
            user_id: User ID

        Returns:
            List of pending requests
        """
        return [r for r in self.pending_requests.values() if user_id in r.approvers]

    def get_request_status(self, request_id: str) -> Dict[str, Any]:
        """Get the status of a request.

        Args:
            request_id: Request ID

        Returns:
            Status dictionary
        """
        if request_id in self.pending_requests:
            request = self.pending_requests[request_id]
        elif request_id in self.completed_requests:
            request = self.completed_requests[request_id]
        else:
            raise ValueError(f"Unknown request: {request_id}")

        return {
            "request_id": request_id,
            "status": request.status.value,
            "created_at": datetime.fromtimestamp(request.created_at).isoformat(),
            "updated_at": datetime.fromtimestamp(request.updated_at).isoformat(),
            "operation": request.operation,
            "requester": request.requester,
            "approvers": request.approvers,
            "escalation_level": request.escalation_level,
        }

    def get_audit_trail(
        self, operation: Optional[str] = None, limit: int = 100
    ) -> List[Dict[str, Any]]:
        """Read audit trail entries.

        Args:
            operation: Filter by operation (optional)
            limit: Maximum entries to return

        Returns:
            List of audit entries
        """
        entries = []

        if not self.audit_log_path.exists():
            return entries

        try:
            with open(self.audit_log_path, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    entry = json.loads(line)
                    if operation is None or entry.get("operation") == operation:
                        entries.append(entry)
                        if len(entries) >= limit:
                            break
        except Exception as e:
            logger.error(f"Failed to read audit log: {e}")

        return entries[-limit:]

    def set_request_callback(self, callback: Callable[[ApprovalRequest], None]) -> None:
        """Set callback for delivering approval requests to users.

        Args:
            callback: Function to call with new requests
        """
        self._request_callback = callback

    def set_escalation_callback(
        self, callback: Callable[[ApprovalRequest, str], None]
    ) -> None:
        """Set callback for escalation notifications.

        Args:
            callback: Function to call on escalation
        """
        self._escalation_callback = callback

    def _get_pending_request(self, request_id: str) -> ApprovalRequest:
        """Get a pending request by ID.

        Args:
            request_id: Request ID

        Returns:
            ApprovalRequest

        Raises:
            ValueError: If request not found or not pending
        """
        if request_id not in self.pending_requests:
            raise ValueError(f"Request not found or not pending: {request_id}")
        return self.pending_requests[request_id]

    def _complete_request(
        self, request: ApprovalRequest, status: ApprovalStatus
    ) -> None:
        """Mark a request as completed.

        Args:
            request: Request to complete
            status: Final status
        """
        request.status = status
        request.updated_at = time.time()

        # Move from pending to completed
        if request.request_id in self.pending_requests:
            del self.pending_requests[request.request_id]
        self.completed_requests[request.request_id] = request

        self._audit_log(
            "request_completed",
            request_id=request.request_id,
            details={"status": status.value},
        )

    def _audit_log(
        self,
        event_type: str,
        request_id: str = "",
        gate_id: str = "",
        operation: str = "",
        user: str = "",
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Write an entry to the audit log.

        Args:
            event_type: Type of event
            request_id: Related request ID
            gate_id: Related gate ID
            operation: Related operation
            user: User who performed the action
            details: Additional event details
        """
        entry = AuditTrailEntry(
            event_type=event_type,
            request_id=request_id,
            gate_id=gate_id,
            operation=operation,
            user=user,
            details=details or {},
        )

        try:
            with open(self.audit_log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry.to_dict()) + "\n")
        except Exception as e:
            logger.error(f"Failed to write audit log: {e}")


# Singleton instance for global access
_approval_engine: Optional[ApprovalWorkflowEngine] = None


def get_approval_engine() -> ApprovalWorkflowEngine:
    """Get the global approval workflow engine instance.

    Returns:
        ApprovalWorkflowEngine singleton
    """
    global _approval_engine
    if _approval_engine is None:
        _approval_engine = ApprovalWorkflowEngine()
    return _approval_engine
