"""Tests for approval workflow system."""

import pytest
from unittest.mock import patch, Mock
import time

from tools.approval_workflow import (
    GateType,
    ApprovalStatus,
    Role,
    ApprovalGate,
    ApprovalRequest,
    ApprovalWorkflow,
    ApprovalSystem,
    get_approval_system,
    check_operation_approval,
)


@pytest.fixture
def fresh_system():
    """Provide a fresh ApprovalSystem for each test."""
    return ApprovalSystem()


class TestApprovalGate:
    """Tests for ApprovalGate dataclass."""
    
    def test_gate_creation(self):
        """Test creating an approval gate."""
        gate = ApprovalGate(
            id="gate-001",
            name="Test Gate",
            gate_type=GateType.PRE,
            description="A test gate",
            required_roles={Role.ACCOUNTABLE},
            sla_minutes=60,
        )
        assert gate.gate_type == GateType.PRE
        assert Role.ACCOUNTABLE in gate.required_roles
    
    def test_to_dict(self):
        """Test converting gate to dict."""
        gate = ApprovalGate(
            id="gate-001",
            name="Test",
            gate_type=GateType.POST,
            description="Test",
            required_roles={Role.RESPONSIBLE},
        )
        d = gate.to_dict()
        assert d["gate_type"] == "post"
        assert "responsible" in d["required_roles"]


class TestApprovalRequest:
    """Tests for ApprovalRequest dataclass."""
    
    def test_request_creation(self):
        """Test creating an approval request."""
        request = ApprovalRequest(
            id="req-001",
            gate_id="gate-001",
            workflow_id="wf-001",
            requester="user1",
            operation="write_file",
            operation_details={"path": "/tmp/test"},
        )
        assert request.status == ApprovalStatus.PENDING
        assert request.operation == "write_file"
    
    def test_expiration(self):
        """Test request expiration logic."""
        request = ApprovalRequest(
            id="req-001",
            gate_id="gate-001",
            workflow_id="wf-001",
            requester="user1",
            operation="test",
            operation_details={},
        )
        
        # Should not be expired with future SLA
        assert not request.is_expired(60)  # 60 minutes in the future
        
        # Set created time to simulate expiration
        request.created_at = time.monotonic() - 3660  # 61 minutes ago
        assert request.is_expired(60)  # 60 minute SLA (expired)
        assert not request.is_expired(120)  # 120 minute SLA (not expired)


class TestApprovalWorkflow:
    """Tests for ApprovalWorkflow dataclass."""
    
    def test_workflow_creation(self):
        """Test creating an approval workflow."""
        workflow = ApprovalWorkflow(
            id="wf-001",
            name="Test Workflow",
            description="A test workflow",
            gates=[],
            applicable_operations={"write_file", "patch"},
            risk_threshold="high",
        )
        assert "write_file" in workflow.applicable_operations
        assert workflow.risk_threshold == "high"


class TestApprovalSystem:
    """Tests for ApprovalSystem class."""
    
    def test_initialization(self, fresh_system):
        """Test system initialization with default workflows."""
        system = fresh_system
        assert len(system.workflows) >= 3
        assert "high-risk-ops" in system.workflows
        assert "medium-risk-ops" in system.workflows
        assert "critical-ops" in system.workflows
    
    def test_add_workflow(self, fresh_system):
        """Test adding a custom workflow."""
        system = fresh_system
        
        workflow = ApprovalWorkflow(
            id="custom-wf",
            name="Custom",
            description="Custom workflow",
            gates=[],
            applicable_operations={"custom_op"},
        )
        
        system.add_workflow(workflow)
        assert "custom-wf" in system.workflows
    
    def test_get_workflow_for_operation(self, fresh_system):
        """Test finding workflow for an operation."""
        system = fresh_system
        
        wf = system.get_workflow_for_operation("write_file")
        assert wf is not None
        assert wf.risk_threshold == "high"
        
        # Non-existent operation
        wf = system.get_workflow_for_operation("nonexistent")
        assert wf is None
    
    def test_create_approval_request(self, fresh_system):
        """Test creating an approval request."""
        system = fresh_system
        
        request = system.create_approval_request(
            workflow_id="high-risk-ops",
            gate_id="pre-approval",
            requester="user1",
            operation="write_file",
            operation_details={"path": "/tmp/test"},
        )
        
        assert request.id in system.requests
        assert request.status == ApprovalStatus.PENDING
        assert len(system.audit_trail) > 0
    
    def test_approve_request(self, fresh_system):
        """Test approving a request."""
        system = fresh_system
        
        request = system.create_approval_request(
            workflow_id="high-risk-ops",
            gate_id="pre-approval",
            requester="user1",
            operation="write_file",
            operation_details={},
        )
        
        result = system.approve_request(request.id, "approver1", "Looks good")
        assert result is True
        assert system.requests[request.id].status == ApprovalStatus.APPROVED
        assert "approver1" in system.requests[request.id].approvers
    
    def test_deny_request(self, fresh_system):
        """Test denying a request."""
        system = fresh_system
        
        request = system.create_approval_request(
            workflow_id="high-risk-ops",
            gate_id="pre-approval",
            requester="user1",
            operation="write_file",
            operation_details={},
        )
        
        result = system.deny_request(request.id, "denier1", "Too risky")
        assert result is True
        assert system.requests[request.id].status == ApprovalStatus.DENIED
    
    def test_get_pending_requests(self, fresh_system):
        """Test getting pending requests."""
        system = fresh_system
        
        system.create_approval_request(
            workflow_id="high-risk-ops",
            gate_id="pre-approval",
            requester="user1",
            operation="write_file",
            operation_details={},
        )
        
        system.create_approval_request(
            workflow_id="high-risk-ops",
            gate_id="pre-approval",
            requester="user2",
            operation="patch",
            operation_details={},
        )
        
        pending = system.get_pending_requests()
        assert len(pending) == 2
    
    def test_check_expired_requests(self, fresh_system):
        """Test checking for expired requests."""
        system = fresh_system
        
        request = system.create_approval_request(
            workflow_id="medium-risk-ops",
            gate_id="pre-approval",
            requester="user1",
            operation="web_search",
            operation_details={},
        )
        
        # Manually set created time to simulate expiration
        # Medium-risk-ops has SLA of 120 minutes (7200 seconds)
        system.requests[request.id].created_at = time.monotonic() - 7500  # > 120 minutes ago
        
        expired = system.check_expired_requests()
        assert len(expired) > 0
        # Should be auto-approved based on workflow config
        assert system.requests[request.id].status == ApprovalStatus.APPROVED
    
    def test_statistics(self, fresh_system):
        """Test getting system statistics."""
        system = fresh_system
        
        request = system.create_approval_request(
            workflow_id="high-risk-ops",
            gate_id="pre-approval",
            requester="user1",
            operation="write_file",
            operation_details={},
        )
        
        stats = system.get_statistics()
        assert stats["total_requests"] >= 1
        assert stats["pending"] >= 1
        assert stats["workflows"] >= 3


class TestGlobalFunctions:
    """Tests for global utility functions."""
    
    def test_get_approval_system(self):
        """Test getting global approval system."""
        system = get_approval_system()
        assert isinstance(system, ApprovalSystem)
        
        # Should return same instance
        system2 = get_approval_system()
        assert system is system2
    
    def test_check_operation_approval_no_workflow(self):
        """Test approval check for operation without workflow."""
        can_proceed, request_id = check_operation_approval(
            operation="nonexistent_operation",
            operation_details={},
        )
        
        assert can_proceed is True
        assert request_id is None
    
    def test_check_operation_approval_with_workflow(self):
        """Test approval check for operation with workflow."""
        can_proceed, request_id = check_operation_approval(
            operation="write_file",
            operation_details={"path": "/tmp/test"},
        )
        
        assert can_proceed is False
        assert request_id is not None


class TestGateTypes:
    """Tests for different gate types."""
    
    def test_pre_gate(self, fresh_system):
        """Test pre-execution gate."""
        system = fresh_system
        
        wf = system.get_workflow_for_operation("write_file")
        pre_gate = next(g for g in wf.gates if g.gate_type == GateType.PRE)
        
        assert pre_gate is not None
        assert pre_gate.gate_type == GateType.PRE
    
    def test_post_gate(self, fresh_system):
        """Test post-execution gate."""
        system = fresh_system
        
        wf = system.get_workflow_for_operation("write_file")
        post_gate = next((g for g in wf.gates if g.gate_type == GateType.POST), None)
        
        assert post_gate is not None
        assert post_gate.gate_type == GateType.POST


class TestAuditTrail:
    """Tests for audit trail functionality."""
    
    def test_audit_entries_created(self, fresh_system):
        """Test that audit entries are created."""
        system = fresh_system
        
        system.create_approval_request(
            workflow_id="high-risk-ops",
            gate_id="pre-approval",
            requester="user1",
            operation="write_file",
            operation_details={},
        )
        
        audit_trail = system.get_audit_trail()
        assert len(audit_trail) > 0
        assert audit_trail[0].event_type == "created"
    
    def test_approval_audit_entry(self, fresh_system):
        """Test audit entry for approval."""
        system = fresh_system
        
        request = system.create_approval_request(
            workflow_id="high-risk-ops",
            gate_id="pre-approval",
            requester="user1",
            operation="write_file",
            operation_details={},
        )
        
        system.approve_request(request.id, "approver1")
        
        audit_trail = system.get_audit_trail(request_id=request.id)
        approval_entry = next(e for e in audit_trail if e.event_type == "approved")
        
        assert approval_entry.actor == "approver1"
    
    def test_filtered_audit_trail(self, fresh_system):
        """Test filtering audit trail."""
        system = fresh_system
        
        system.create_approval_request(
            workflow_id="high-risk-ops",
            gate_id="pre-approval",
            requester="user1",
            operation="write_file",
            operation_details={},
        )
        
        system.create_approval_request(
            workflow_id="medium-risk-ops",
            gate_id="pre-approval",
            requester="user2",
            operation="web_search",
            operation_details={},
        )
        
        # Filter by workflow
        audit_trail = system.get_audit_trail(workflow_id="high-risk-ops")
        assert all(e.workflow_id == "high-risk-ops" for e in audit_trail)
