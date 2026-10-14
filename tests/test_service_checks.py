"""Frozen corpus ages and the shared collection stale check use synthetic dates."""

import asyncio
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from eval_fixtures import TOY_HASH, TOY_SOURCE_SHA, canonical_hits, write_toy_data
from test_eval_collection import RecordingBatchLedger, _environment
from test_eval_real_collection import SyntheticRealCompositionPort

from rent_navigator import service_checks
from rent_navigator.corpus import load_corpus
from rent_navigator.eval.collection import collect_real
from rent_navigator.eval.data import load_gold
from rent_navigator.provider import ProviderFailure, SpendLedger


@pytest.mark.parametrize("age", [0, 90, 91])
def test_ninety_days_is_the_last_fresh_day(age: int) -> None:
    snapshot = date(2026, 1, 1)
    if age > 90:
        with pytest.raises(ProviderFailure) as caught:
            service_checks.require_fresh_corpus(snapshot, today=snapshot + timedelta(days=age))
        assert caught.value.code == "stale_corpus"
    else:
        service_checks.require_fresh_corpus(snapshot, today=snapshot + timedelta(days=age))


def test_collection_records_stale_failure_before_any_provider_operation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    corpus = load_corpus()
    monkeypatch.setattr(
        service_checks, "utc_today", lambda: corpus.snapshot_date + timedelta(days=91)
    )
    dataset = load_gold(write_toy_data(tmp_path / "data", corpus), corpus)
    port = SyntheticRealCompositionPort()
    manifest = asyncio.run(
        collect_real(
            dataset,
            tmp_path / "results",
            corpus=corpus,
            source_sha=TOY_SOURCE_SHA,
            lock_hash=TOY_HASH,
            environment=_environment(),
            messages=port,
            budget=SpendLedger(Decimal("32")),
            batch_ledger=RecordingBatchLedger([]),
            forecast_usd=Decimal("29.15320"),
            retrieve=lambda query: canonical_hits(corpus, query),
        )
    )
    assert not manifest.collection_complete and not manifest.reportable
    assert len(manifest.started_attempts) == 1 and port.actor_calls == port.judge_calls == 0
    assert "collection_untrustworthy" in manifest.reasons
    assert '"code":"stale_corpus"' in (tmp_path / "results" / "warmups.jsonl").read_text()
