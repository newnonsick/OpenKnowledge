from __future__ import annotations

import json
from pathlib import Path


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "retrieval-evaluation-v2.json"


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_eval_fixture_covers_required_categories_with_at_least_30_cases() -> None:
    fixture = _fixture()
    assert fixture["version"] == 2
    cases = fixture["cases"]
    assert len(cases) >= 30

    thai = [case for case in cases if case["slice"].startswith("thai")]
    english = [case for case in cases if case["slice"] == "english"]
    code = [case for case in cases if case["slice"].startswith("code")]
    stale = [case for case in cases if case.get("superseded_by")]
    acl = [case for case in cases if case.get("visible_to")]
    revoked = [case for case in cases if case.get("revoked") is True]
    no_answer = [case for case in cases if case.get("expect_empty")]
    distractors = [case for case in cases if case.get("distractor_for")]

    assert len(thai) >= 6
    assert len(english) >= 4
    assert len(code) >= 4
    assert len(stale) >= 4
    assert len(acl) >= 2
    assert len(revoked) >= 1
    assert len(no_answer) >= 2
    assert len(distractors) >= 4


def test_eval_fixture_references_are_consistent() -> None:
    fixture = _fixture()
    cases = fixture["cases"]
    identifiers = [case["id"] for case in cases]
    assert len(identifiers) == len(set(identifiers))
    by_id = {case["id"]: case for case in cases}
    for case in cases:
        assert case["query"] and case["title"] and case["content"]
        assert isinstance(case["space_index"], int) and isinstance(case["slot"], int)
        for field in ("superseded_by", "distractor_for"):
            reference = case.get(field)
            if reference is not None:
                assert reference in by_id
                assert by_id[reference]["query"] == case["query"]


def test_eval_fixture_records_baseline_regression_thresholds() -> None:
    fixture = _fixture()
    assert fixture["minimum_lexical_recall"] == 1.0
    assert fixture["minimum_no_answer_precision"] == 1.0
    baseline = fixture["baseline"]
    assert baseline["lexical_recall"] == 1.0
    assert baseline["no_answer_precision"] == 1.0
    assert 0.0 < baseline["lexical_recall"] <= 1.0
    assert 0.0 < baseline["no_answer_precision"] <= 1.0
