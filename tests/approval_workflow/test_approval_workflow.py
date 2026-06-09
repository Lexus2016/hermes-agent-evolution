#!/usr/bin/env python3
"""Tests for approval_workflow module."""

import json
import tempfile
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from tools.approval_workflow import (
    ApprovalGate,
    ApprovalGateType,
    ApprovalStatus,
    ApprovalWorkflowEngine,
    RACIRole,
    ApprovalRequest,
    get_approval_engine,
)


@pytest.fixture
def temp_audit_log():
    """Create a temporary audit log file."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        temp_path = Path(f.name)
    yield temp_path
    # Cleanup
    if temp_path.exists():
        temp_path.unlink()


@pytest.fixture
def engine(temp_audit_log):
    """Create an ApprovalWorkflowEngine with temp audit log."""
    return ApprovalWorkflowEngine(audit_log_path=temp_audit_log)


@pytest.fixture
def sample_gate():
    """Create a sample approval gate."""
    return ApprovalGate(
        gate_id="prod_deploy",
        name="Production Deployment",
        gate_type=ApprovalGateType.PRE,
        description="Approval for production deployments",
        required_approvers=["admin", "ops_lead"],
        sla_minutes=60,
        escalation_path=["manager", "director"],
    )


class TestApprovalGate:
    """Test ApprovalGate dataclass."""

    def test_gate_creation(self):
        """Test ApprovalGate can be created."""
        gate = ApprovalGate(
            gate_id="test_gate",
            name="Test Gate",
            gate_type=ApprovalGateType.PRE,
            required_approvers=["user1"],
        )
        assert gate.gate_id == "test_gate"
        assert gate.name == "Test Gate"
        assert gate.gate_type == ApprovalGateType.PRE


class TestApprovalRequest:
    """Test ApprovalRequest dataclass."""

    def test_request_creation(self):
        """Test ApprovalRequest can be created."""
        request = ApprovalRequest(
            request_id="req-1",
            gate_id="gate-1",
            operation="deploy",
            requester="user1",
            approvers=["admin"],
        )
        assert request.request_id == "req-1"
        assert request.status == ApprovalStatus.PENDING
        assert len(request.approval_comments) == 0


class TestApprovalWorkflowEngine:
    """Test ApprovalWorkflowEngine main class."""

    def test_initialization(self, engine):
        """Test engine initialization."""
        assert engine.gates == {}
        assert engine.pending_requests == {}
        assert engine.completed_requests == {}

    def test_register_gate(self, engine, sample_gate):
        """Test registering an approval gate."""
        engine.register_gate(sample_gate)
        assert "prod_deploy" in engine.gates
        assert engine.gates["prod_deploy"].name == "Production Deployment"

    def test_define_raci_mapping(self, engine):
        """Test defining RACI mapping."""
        engine.define_raci_mapping(
            operation="deploy",
            responsible=["developer"],
            accountable=["ops_lead"],
            consulted=["security"],
            informed=["product"],
        )
        assert "deploy" in engine.racii_map
        assert RACIRole.RESPONSIBLE in engine.racii_map["deploy"]
        assert engine.racii_map["deploy"][RACIRole.ACCOUNTABLE] == ["ops_lead"]

    def test_get_approvers_for_operation(self, engine):
        """Test getting approvers from RACI map."""
        engine.define_raci_mapping(
            operation="deploy", accountable=["ops_lead", "manager"]
        )
        approvers = engine.get_approvers_for_operation("deploy", RACIRole.ACCOUNTABLE)
        assert approvers == ["ops_lead", "manager"]

    def test_create_approval_request(self, engine, sample_gate):
        """Test creating an approval request."""
        engine.register_gate(sample_gate)
        request = engine.create_approval_request(
            gate_id="prod_deploy",
            operation="deploy_app",
            requester="developer",
            metadata={"version": "1.0"},
        )
        assert request.request_id
        assert request.gate_id == "prod_deploy"
        assert request.operation == "deploy_app"
        assert request.requester == "developer"
        assert request.status == ApprovalStatus.PENDING
        assert request.approvers == ["admin", "ops_lead"]

    def test_create_approval_request_with_racii(self, engine):
        """Test creating request with RACI-based approvers."""
        engine.define_raci_mapping(operation="deploy", accountable=["ops_lead"])
        gate = ApprovalGate(
            gate_id="deploy",
            name="Deploy",
            gate_type=ApprovalGateType.PRE,
        )
        engine.register_gate(gate)
        request = engine.create_approval_request(
            gate_id="deploy", operation="deploy", requester="dev"
        )
        assert request.approvers == ["ops_lead"]

    def test_create_approval_request_unknown_gate(self, engine):
        """Test error when creating request for unknown gate."""
        with pytest.raises(ValueError, match="Unknown gate"):
            engine.create_approval_request(
                gate_id="unknown", operation="test", requester="user"
            )

    def test_approve_request(self, engine, sample_gate):
        """Test approving a request."""
        engine.register_gate(sample_gate)
        request = engine.create_approval_request(
            gate_id="prod_deploy", operation="deploy", requester="dev"
        )
        approved = engine.approve_request(
            request_id=request.request_id, approver="admin", comment="Looks good"
        )
        assert approved.status == ApprovalStatus.APPROVED
        assert len(approved.approval_comments) == 1

    def test_approve_request_unauthorized(self, engine, sample_gate):
        """Test error when unauthorized user tries to approve."""
        engine.register_gate(sample_gate)
        request = engine.create_approval_request(
            gate_id="prod_deploy", operation="deploy", requester="dev"
        )
        with pytest.raises(PermissionError, match="not authorized"):
            engine.approve_request(
                request_id=request.request_id, approver="unauthorized"
            )

    def test_reject_request(self, engine, sample_gate):
        """Test rejecting a request."""
        engine.register_gate(sample_gate)
        request = engine.create_approval_request(
            gate_id="prod_deploy", operation="deploy", requester="dev"
        )
        rejected = engine.reject_request(
            request_id=request.request_id,
            approver="admin",
            comment="Not ready for production",
        )
        assert rejected.status == ApprovalStatus.REJECTED

    def test_get_request_status(self, engine, sample_gate):
        """Test getting request status."""
        engine.register_gate(sample_gate)
        request = engine.create_approval_request(
            gate_id="prod_deploy", operation="deploy", requester="dev"
        )
        status = engine.get_request_status(request.request_id)
        assert status["request_id"] == request.request_id
        assert status["status"] == "pending"
        assert status["operation"] == "deploy"

    def test_get_request_status_unknown(self, engine):
        """Test error when getting status of unknown request."""
        with pytest.raises(ValueError, match="Unknown request"):
            engine.get_request_status("unknown_request")

    def test_get_pending_requests_for_user(self, engine, sample_gate):
        """Test getting pending requests for a user."""
        engine.register_gate(sample_gate)
        request1 = engine.create_approval_request(
            gate_id="prod_deploy", operation="deploy1", requester="dev1"
        )
        request2 = engine.create_approval_request(
            gate_id="prod_deploy", operation="deploy2", requester="dev2"
        )
        # admin is approver for both
        pending = engine.get_pending_requests_for_user("admin")
        assert len(pending) == 2

    def test_sla_breach_check(self, engine):
        """Test SLA breach detection."""
        gate = ApprovalGate(
            gate_id="sla_test",
            name="SLA Test",
            gate_type=ApprovalGateType.PRE,
            required_approvers=["admin"],
            sla_minutes=-1,  # Already expired
        )
        engine.register_gate(gate)
        request = engine.create_approval_request(
            gate_id="sla_test", operation="test", requester="dev"
        )
        breached = engine.check_sla_breaches()
        assert len(breached) == 1
        assert breached[0].request_id == request.request_id

    def test_sla_escalation(self, engine, sample_gate):
        """Test escalation on SLA breach."""
        sample_gate.sla_minutes = -1  # Already expired
        engine.register_gate(sample_gate)
        request = engine.create_approval_request(
            gate_id="prod_deploy", operation="deploy", requester="dev"
        )
        engine.check_sla_breaches()
        # Request should be escalated
        status = engine.get_request_status(request.request_id)
        assert status["escalation_level"] >= 1

    def test_audit_trail(self, engine, sample_gate):
        """Test audit trail logging."""
        engine.register_gate(sample_gate)
        request = engine.create_approval_request(
            gate_id="prod_deploy", operation="deploy", requester="dev"
        )
        engine.approve_request(request_id=request.request_id, approver="admin")
        trail = engine.get_audit_trail()
        assert len(trail) >= 2  # gate_registered, request_created, request_approved

    def test_audit_trail_filter_by_operation(self, engine, sample_gate):
        """Test filtering audit trail by operation."""
        engine.register_gate(sample_gate)
        request = engine.create_approval_request(
            gate_id="prod_deploy", operation="deploy", requester="dev"
        )
        trail = engine.get_audit_trail(operation="deploy")
        for entry in trail:
            assert entry.get("operation") == "deploy"

    def test_set_request_callback(self, engine, sample_gate):
        """Test setting request callback."""
        callback_mock = Mock()
        engine.set_request_callback(callback_mock)
        engine.register_gate(sample_gate)
        engine.create_approval_request(
            gate_id="prod_deploy", operation="deploy", requester="dev"
        )
        callback_mock.assert_called_once()

    def test_set_escalation_callback(self, engine):
        """Test setting escalation callback."""
        callback_mock = Mock()
        engine.set_escalation_callback(callback_mock)
        gate = ApprovalGate(
            gate_id="escalate_test",
            name="Escalate Test",
            gate_type=ApprovalGateType.PRE,
            required_approvers=["admin"],
            sla_minutes=-1,
            escalation_path=["manager"],
        )
        engine.register_gate(gate)
        engine.create_approval_request(
            gate_id="escalate_test", operation="test", requester="dev"
        )
        engine.check_sla_breaches()
        callback_mock.assert_called_once()


class TestGetApprovalEngine:
    """Test global approval engine singleton."""

    def test_singleton(self):
        """Test that get_approval_engine returns same instance."""
        engine1 = get_approval_engine()
        engine2 = get_approval_engine()
        assert engine1 is engine2


@pytest.mark.integration
class TestApprovalWorkflowIntegration:
    """Integration tests for approval workflow."""

    def test_complete_approval_workflow(self, engine, sample_gate):
        """Test complete workflow from request to approval."""
        # Setup RACI mapping
        engine.define_raci_mapping(
            operation="deploy", accountable=["ops_lead", "manager"]
        )
        engine.register_gate(sample_gate)

        # Create request
        request = engine.create_approval_request(
            gate_id="prod_deploy",
            operation="deploy_app",
            requester="developer",
            metadata={"version": "1.0.0"},
        )

        # Check pending
        assert request.status == ApprovalStatus.PENDING
        assert request.request_id in engine.pending_requests

        # Approve
        approved = engine.approve_request(
            request_id=request.request_id,
            approver="ops_lead",
            comment="Approved for production",
        )

        # Verify completed
        assert approved.status == ApprovalStatus.APPROVED
        assert request.request_id in engine.completed_requests
        assert request.request_id not in engine.pending_requests

        # Check audit trail
        trail = engine.get_audit_trail(request_id=request.request_id)
        assert len(trail) >= 2

    def test_rejection_workflow(self, engine):
        """Test rejection workflow."""
        gate = ApprovalGate(
            gate_id="test_gate",
            name="Test",
            gate_type=ApprovalGateType.PRE,
            required_approvers=["reviewer"],
        )
        engine.register_gate(gate)

        request = engine.create_approval_request(
            gate_id="test_gate", operation="test", requester="dev"
        )

        rejected = engine.reject_request(
            request_id=request.request_id,
            approver="reviewer",
            comment="Needs more testing",
        )

        assert rejected.status == ApprovalStatus.REJECTED
        assert request.request_id in engine.completed_requests

    def test_multi_level_escalation(self, engine):
        """Test multi-level escalation."""
        gate = ApprovalGate(
            gate_id="escalation_gate",
            name="Escalation",
            gate_type=ApprovalGateType.PRE,
            required_approvers=["level1"],
            sla_minutes=-1,
            escalation_path=["level2", "level3"],
        )
        engine.register_gate(gate)
        request = engine.create_approval_request(
            gate_id="escalation_gate", operation="test", requester="dev"
        )

        # First escalation
        engine.check_sla_breaches()
        status = engine.get_request_status(request.request_id)
        assert status["escalation_level"] == 1

        # Second escalation (modify deadline again)
        request = engine.pending_requests.get(request.request_id)
        if request:
            request.sla_deadline = time.time() - 100
        engine.check_sla_breaches()
        status = engine.get_request_status(request.request_id)
        assert status["escalation_level"] == 2
