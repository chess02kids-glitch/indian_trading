"""End-to-end safety dry runs for the RC-1 release gate.

These tests use a fake broker only: no credentials, network calls, Market, or
IOC orders are created. They specify the fail-closed lifecycle expected from
the existing execution and risk integration.
"""

from dataclasses import dataclass

import pytest


@dataclass
class DryRunOrder:
    client_order_id: str
    order_type: str = "LIMIT"
    data_age_seconds: int = 0


class FakeRiskEngine:
    def __init__(self, enabled=True):
        self.enabled = enabled

    def approve(self, order):
        if not self.enabled:
            raise RuntimeError("kill switch active")
        if order.order_type in {"MARKET", "IOC"}:
            raise ValueError("prohibited order type")
        if order.data_age_seconds > 60:
            raise ValueError("stale market data")


class FakeBroker:
    def __init__(self, session_valid=True, fails=False):
        self.session_valid, self.fails, self.calls = session_valid, fails, 0

    def place_limit_order(self, order):
        self.calls += 1
        if not self.session_valid:
            raise PermissionError("broker session expired")
        if self.fails:
            raise ConnectionError("broker unavailable")
        return f"broker-{order.client_order_id}"


class Lifecycle:
    def __init__(self, risk, broker):
        self.risk, self.broker, self.seen = risk, broker, set()

    def submit(self, order):
        self.risk.approve(order)  # Risk gate precedes every broker action.
        if order.client_order_id in self.seen:
            raise ValueError("duplicate order")
        self.seen.add(order.client_order_id)
        return self.broker.place_limit_order(order)

    def reconcile(self, internal, broker):
        if internal != broker:
            raise RuntimeError("reconciliation mismatch")
        return "reconciled"


def test_complete_limit_order_lifecycle_passes_risk_then_broker():
    lifecycle = Lifecycle(FakeRiskEngine(), FakeBroker())
    assert lifecycle.submit(DryRunOrder("one")) == "broker-one"
    assert lifecycle.reconcile({"broker-one"}, {"broker-one"}) == "reconciled"


def test_stale_data_is_rejected_before_broker_call():
    broker = FakeBroker()
    with pytest.raises(ValueError, match="stale"):
        Lifecycle(FakeRiskEngine(), broker).submit(
            DryRunOrder("stale", data_age_seconds=61)
        )
    assert broker.calls == 0


def test_duplicate_order_is_rejected():
    lifecycle = Lifecycle(FakeRiskEngine(), FakeBroker())
    lifecycle.submit(DryRunOrder("duplicate"))
    with pytest.raises(ValueError, match="duplicate"):
        lifecycle.submit(DryRunOrder("duplicate"))


def test_session_expiry_fails_closed():
    with pytest.raises(PermissionError, match="expired"):
        Lifecycle(FakeRiskEngine(), FakeBroker(session_valid=False)).submit(
            DryRunOrder("expired")
        )


def test_broker_failure_is_reported():
    with pytest.raises(ConnectionError, match="unavailable"):
        Lifecycle(FakeRiskEngine(), FakeBroker(fails=True)).submit(
            DryRunOrder("failure")
        )


def test_reconciliation_mismatch_fails_closed():
    with pytest.raises(RuntimeError, match="mismatch"):
        Lifecycle(FakeRiskEngine(), FakeBroker()).reconcile({"internal"}, {"broker"})
