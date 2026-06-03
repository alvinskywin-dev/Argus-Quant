"""
Sprint 20E — unit tests for the pure safety rules (no DB required).
"""

from __future__ import annotations

from app.safety import rules


def test_correlation_cluster():
    assert rules.correlation_cluster("BTCUSDT") == "MAJOR"
    assert rules.correlation_cluster("ETHUSDT") == "MAJOR"
    assert rules.correlation_cluster("SOLUSDT") == "L1"
    assert rules.correlation_cluster("DOGEUSDT") == "MEME"
    assert rules.correlation_cluster("1000PEPEUSDT") == "MEME"
    assert rules.correlation_cluster("FOOUSDT") == "ALT"


def test_consecutive_losses():
    assert rules.consecutive_losses([-1, -2, -3, 5, -1]) == 3
    assert rules.consecutive_losses([5, -1, -2]) == 0  # most recent is a win
    assert rules.consecutive_losses([]) == 0
    assert rules.consecutive_losses([-1]) == 1
    assert rules.consecutive_losses([0, -1]) == 0  # 0 is not a loss


def test_loss_exceeds_limit():
    # 5% of 10,000 = 500 loss threshold
    assert rules.loss_exceeds_limit(-500, 10_000, 5.0)
    assert rules.loss_exceeds_limit(-600, 10_000, 5.0)
    assert not rules.loss_exceeds_limit(-499, 10_000, 5.0)
    assert not rules.loss_exceeds_limit(200, 10_000, 5.0)  # profit
    # guards
    assert not rules.loss_exceeds_limit(-999, 0, 5.0)
    assert not rules.loss_exceeds_limit(-999, 10_000, 0)


def test_count_correlated():
    openpos = [("MAJOR", "LONG"), ("MAJOR", "LONG"), ("L1", "LONG"), ("MAJOR", "SHORT")]
    assert rules.count_correlated(openpos, "MAJOR", "LONG") == 2
    assert rules.count_correlated(openpos, "MAJOR", "SHORT") == 1
    assert rules.count_correlated(openpos, "MEME", "LONG") == 0
