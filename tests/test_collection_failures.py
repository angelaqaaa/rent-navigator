"""Serving failure streaks preserve the fixed schedule and recorded denominators."""

import json
from pathlib import Path
from typing import Any

import pytest
from anthropic.types import MessageTokensCount
from eval_fixtures import write_toy_data
from test_eval_real_collection import SyntheticRealCompositionPort, run

from rent_navigator.corpus import load_corpus
from rent_navigator.eval import collection
from rent_navigator.eval.data import load_gold
from rent_navigator.eval.models import ResultRow
from rent_navigator.eval.runner import run_attempt
from rent_navigator.models import ErrorCode, ErrorResponse
from rent_navigator.provider import ProviderFailure


class FailingCountPort(SyntheticRealCompositionPort):
    pending: ErrorCode | None = None

    async def count_tokens(self, **kwargs: Any) -> MessageTokensCount:
        if self.pending is not None:
            code, self.pending = self.pending, None
            raise ProviderFailure(code)
        return await super().count_tokens(**kwargs)


def execute(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failures: set[int],
    code: ErrorCode,
    *,
    fail_judge: bool = False,
) -> tuple[collection.RealCollectionManifest, list[ResultRow], list[ResultRow]]:
    corpus = load_corpus()
    dataset = load_gold(write_toy_data(tmp_path / "data", corpus), corpus)
    port = FailingCountPort(fail_judge=fail_judge)
    original = run_attempt
    count = 0

    async def attempt(*args: Any, **kwargs: Any) -> Any:
        nonlocal count
        port.pending = code if count in failures else None
        count += 1
        return await original(*args, **kwargs)

    monkeypatch.setattr(collection, "run_attempt", attempt)
    directory = tmp_path / "results"
    manifest = run(directory, dataset, corpus, port)
    rows = [
        ResultRow.model_validate_json(line)
        for line in (directory / "results.jsonl").read_text().splitlines()
    ]
    warmups = [
        ResultRow.model_validate_json(line)
        for line in (directory / "warmups.jsonl").read_text().splitlines()
    ]
    return manifest, rows, warmups


@pytest.mark.parametrize("code", ["provider_error", "deadline_exceeded"])
def test_isolated_measured_serving_failure_keeps_the_denominator_and_continues(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, code: ErrorCode
) -> None:
    manifest, rows, warmups = execute(monkeypatch, tmp_path, {6}, code)
    assert manifest.collection_complete and manifest.evaluation_complete
    assert len(rows) == 160 and len(warmups) == 6
    assert isinstance(rows[0].response, ErrorResponse)
    assert rows[0].response.error.code == code and rows[0].classification == "error"
    assert "collection_untrustworthy" not in manifest.reasons
    summary = json.loads((tmp_path / "results" / "summary.json").read_text())
    assert sum(row.arm == "production" for row in rows) == 80
    assert sum(row.arm == "baseline" for row in rows) == 80
    assert summary["arms"]["production"]["attempts"] == 80
    assert summary["arms"]["production"]["errors"] == 1


@pytest.mark.parametrize(
    "failures,started,warmup_count", [({6, 7, 8, 9, 10}, 11, 6), ({4, 5, 6, 7, 8}, 9, 6)]
)
def test_five_consecutive_failures_stop_including_streaks_from_warmups(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failures: set[int],
    started: int,
    warmup_count: int,
) -> None:
    manifest, rows, warmups = execute(monkeypatch, tmp_path, failures, "provider_error")
    assert not manifest.collection_complete and not manifest.evaluation_complete
    assert "collection_untrustworthy" in manifest.reasons
    assert len(manifest.started_attempts) == started
    assert len(warmups) == warmup_count and len(rows) == started - warmup_count


@pytest.mark.parametrize("failures", [{6, 7, 8, 9}, {4, 5, 6, 7}])
def test_four_failures_then_success_reset_the_streak(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failures: set[int]
) -> None:
    manifest, rows, warmups = execute(monkeypatch, tmp_path, failures, "provider_error")
    assert manifest.collection_complete and manifest.evaluation_complete
    assert len(rows) == 160 and len(warmups) == 6
    assert "collection_untrustworthy" not in manifest.reasons


def test_judge_invalid_still_stops_on_first_occurrence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest, rows, warmups = execute(
        monkeypatch, tmp_path, set(), "provider_error", fail_judge=True
    )
    assert "collection_untrustworthy" in manifest.reasons
    assert len(rows) == 1 and len(warmups) == 6
    assert len(manifest.started_attempts) == 7


def test_budget_exhausted_still_stops_on_first_occurrence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest, rows, warmups = execute(monkeypatch, tmp_path, {0}, "budget_exhausted")
    assert "collection_untrustworthy" in manifest.reasons
    assert len(warmups) == 1 and rows == []
    assert len(manifest.started_attempts) == 1
