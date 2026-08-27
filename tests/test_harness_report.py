"""`harness/report.py` turns a flat list of `SessionResult`s into the
metrics CLAUDE.md §8 requires. These use hand-built `SessionResult`s
instead of a real run so each aggregation rule is tested in isolation."""

from __future__ import annotations

import json

from harness.report import compute_metrics, write_report
from harness.simulator import SessionResult


def _result(**overrides: object) -> SessionResult:
    defaults: dict[str, object] = dict(
        session_id="s1",
        category="benign_green",
        arm="B",
        decision="ALLOW",
        reason_code=None,
        rule_id=None,
        band="green",
        captured_paise=1000,
        attempted_paise=1000,
        num_captures=1,
        latency_ms=5.0,
        is_attack=False,
    )
    defaults.update(overrides)
    return SessionResult(**defaults)  # type: ignore[arg-type]


_STUB_STATS = {
    "band_accuracy": {"total": 0, "matches": 0, "accuracy": None, "confusion": {}},
    "cooling_off_stats": {"held": 0, "cancelled": 0, "cancellation_rate": None},
    "dispute_pack_stats": {"total": 0, "complete": 0, "completeness": None},
}


def test_gmv_sums_captured_paise_per_arm() -> None:
    results = [
        _result(session_id="a1", arm="A", captured_paise=1000),
        _result(session_id="a2", arm="A", captured_paise=2000),
        _result(session_id="b1", arm="B", captured_paise=500),
    ]
    metrics = compute_metrics(results, **_STUB_STATS)
    assert metrics["gmv_paise"] == {"arm_a": 3000, "arm_b": 500}


def test_bad_transaction_requires_attack_and_positive_capture() -> None:
    results = [
        # attack that got through -> counts as bad
        _result(session_id="bad", arm="B", is_attack=True, captured_paise=999, decision="ALLOW"),
        # attack correctly blocked -> not bad (captured_paise == 0)
        _result(session_id="ok", arm="B", is_attack=True, captured_paise=0, decision="BLOCK"),
        # benign capture -> not bad regardless of amount
        _result(session_id="benign", arm="B", is_attack=False, captured_paise=500),
    ]
    metrics = compute_metrics(results, **_STUB_STATS)
    assert metrics["bad_transactions"]["arm_b"] == {"count": 1, "value_paise": 999}


def test_precision_recall_only_counts_adversarial_categories() -> None:
    results = [
        # true positive: attack in an adversarial category, correctly blocked
        _result(
            session_id="tp",
            arm="B",
            category="stale_quote_race",
            is_attack=True,
            decision="BLOCK",
        ),
        # false negative: attack in an adversarial category, let through
        _result(
            session_id="fn",
            arm="B",
            category="envelope_escape",
            is_attack=True,
            decision="ALLOW",
        ),
        # a benign_green BLOCK would be a false positive if it counted --
        # this is deliberately outside the adversarial set and must not
        # contribute a false positive.
        _result(
            session_id="not-fp",
            arm="B",
            category="benign_green",
            is_attack=False,
            decision="BLOCK",
        ),
    ]
    metrics = compute_metrics(results, **_STUB_STATS)
    pr = metrics["gate_precision_recall"]
    assert pr["true_positive"] == 1
    assert pr["false_negative"] == 1
    # false_positive counts non-attack BLOCKs across ALL of arm B, not just
    # the adversarial split -- confirm the benign_green BLOCK above is
    # picked up there instead of silently vanishing.
    assert pr["false_positive"] == 1
    assert pr["precision"] == 1 / 2
    assert pr["recall"] == 1 / 2


def test_false_positive_cost_sums_attempted_paise_of_wrongly_blocked_benign() -> None:
    results = [
        _result(session_id="fp1", arm="B", is_attack=False, decision="BLOCK", attempted_paise=700),
        _result(session_id="fp2", arm="B", is_attack=False, decision="BLOCK", attempted_paise=300),
        # attack blocked -- not a false positive, must not be counted
        _result(session_id="tp", arm="B", is_attack=True, decision="BLOCK", attempted_paise=5000),
    ]
    metrics = compute_metrics(results, **_STUB_STATS)
    assert metrics["false_positive_cost_paise"] == 1000


def test_injection_results_excluded_from_arm_totals_and_counted_separately() -> None:
    results = [
        _result(session_id="held", decision="INVARIANT_HELD", arm="B"),
        _result(session_id="broken", decision="INVARIANT_BROKEN", arm="B"),
        _result(session_id="normal", decision="ALLOW", arm="B"),
    ]
    metrics = compute_metrics(results, **_STUB_STATS)
    assert metrics["sessions"]["arm_b"] == 1  # only the non-injection result
    assert metrics["injection_invariance"] == {
        "total": 2,
        "passed": 1,
        "pass_rate": 0.5,
    }


def test_gate_latency_percentiles_computed_over_arm_b_only() -> None:
    results = [
        _result(session_id="a", arm="A", latency_ms=999.0),
        _result(session_id="b1", arm="B", latency_ms=1.0),
        _result(session_id="b2", arm="B", latency_ms=3.0),
    ]
    metrics = compute_metrics(results, **_STUB_STATS)
    assert metrics["gate_latency_ms"]["p50"] in (1.0, 2.0, 3.0)  # interpolated
    assert metrics["gate_latency_ms"]["mean"] == 2.0


def test_write_report_produces_valid_json_and_nonempty_markdown(tmp_path: object) -> None:
    from pathlib import Path

    out_dir = Path(str(tmp_path))
    metrics = compute_metrics([_result()], **_STUB_STATS)
    write_report(metrics, out_dir)

    results_json = json.loads((out_dir / "results.json").read_text())
    assert results_json == metrics

    md = (out_dir / "RESULTS.md").read_text()
    assert md.startswith("# RESULTS")
    assert "Net GMV captured" in md
    assert "Gate precision / recall" in md
