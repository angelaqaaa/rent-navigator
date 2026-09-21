"""Offline development-entry checks; no credentials or real requests."""

import asyncio
import json
from pathlib import Path
from typing import NoReturn

import pytest

import rent_navigator.smoke_extraction as smoke
from rent_navigator.provider import ProviderFailure
from rent_navigator.trace import ACTOR_MODEL, JUDGE_MODEL, TraceRecord, provider_cost_totals


def reject_real_client(*args: object, **kwargs: object) -> NoReturn:
    raise AssertionError("Offline smoke tried to create a real client")


def test_offline_smoke_records_synthetic_successes_without_real_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(smoke, "create_client", reject_real_client)
    evidence = tmp_path / "new-evidence"
    report = asyncio.run(smoke.run_smoke(source_commit="a" * 40, evidence_dir=evidence))
    assert report["mode"] == "offline_synthetic"
    assert report["live_acceptance"] == "NOT RUN"
    assert report["real_generation_attempts"] == 0
    assert report["generation_attempts"] == 2
    assert report["availability"] == []
    assert [case["matches_expected"] for case in report["cases"]] == [True, True]
    assert report["cases"][0]["extraction"] == {
        "current_cents": 123450,
        "proposed_cents": 125980,
        "effective_on": "2027-04-01",
    }
    assert all(value is None for value in report["cases"][1]["extraction"].values())
    assert json.loads((evidence / "report.json").read_text()) == report
    records = [
        TraceRecord.model_validate_json(line)
        for line in (evidence / "metadata.jsonl").read_text().splitlines()
    ]
    assert len(records) == 6
    assert provider_cost_totals(records).model_dump(mode="json") == report["token_accounting"]
    for _, letter, _ in smoke.CASES:
        assert letter not in (evidence / "metadata.jsonl").read_text()


def test_smoke_requires_new_directory_and_explicit_billing_confirmation(tmp_path: Path) -> None:
    with pytest.raises(FileExistsError):
        asyncio.run(smoke.run_smoke(source_commit="a" * 40, evidence_dir=tmp_path))
    with pytest.raises(ProviderFailure) as error:
        asyncio.run(
            smoke.run_smoke(source_commit="a" * 40, evidence_dir=tmp_path / "live", live=True)
        )
    assert error.value.code == "budget_exhausted"
    assert not (tmp_path / "live").exists()


def test_synthetic_redactor_refuses_arbitrary_letters() -> None:
    with pytest.raises(ValueError, match="preset synthetic"):
        smoke.synthetic_redactor("An arbitrary private letter must not pass this boundary")


@pytest.mark.parametrize(
    "outcome", ["success", "mismatch", "missing_usage", "refusal", "unavailable"]
)
def test_live_control_flow_with_fake_client_preserves_attempts_and_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    from typing import Any

    from anthropic.types import Message, TextBlock

    class FakeMessages(smoke._OfflineMessages):
        def __init__(self) -> None:
            self.calls = 0

        async def create(self, **kwargs: Any) -> Message:
            self.calls += 1
            response = await super().create(**kwargs)
            if outcome == "missing_usage":
                return response.model_copy(update={"usage": None})
            if outcome == "refusal":
                return response.model_copy(update={"stop_reason": "refusal"})
            if outcome == "mismatch":
                return response.model_copy(
                    update={"content": [TextBlock(type="text", text=smoke.CASES[1][2])]}
                )
            return response

    class FakeModels:
        def __init__(self) -> None:
            self.requested: list[str] = []

        async def retrieve(self, model: str, **kwargs: Any) -> Any:
            from types import SimpleNamespace

            self.requested.append(model)
            if outcome == "unavailable":
                raise RuntimeError("synthetic private SDK error")
            return SimpleNamespace(id=model + "-resolved")

    class FakeClient:
        def __init__(self) -> None:
            self.messages = FakeMessages()
            self.models = FakeModels()
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    client = FakeClient()
    monkeypatch.setattr(smoke, "create_client", lambda **kwargs: client)
    report = asyncio.run(
        smoke.run_smoke(
            source_commit="a" * 40,
            evidence_dir=tmp_path / outcome,
            live=True,
            billing_ready=True,
        )
    )
    assert client.closed
    assert "synthetic private SDK error" not in json.dumps(report)
    assert report["generation_attempts"] == client.messages.calls
    if outcome == "unavailable":
        assert client.messages.calls == 0
        assert report["availability"] == [{"requested_model": ACTOR_MODEL, "available": None}]
        assert report["error"] == "provider_error"
    else:
        assert client.models.requested == [ACTOR_MODEL, JUDGE_MODEL]
        assert [entry["returned_model"] for entry in report["availability"]] == [
            ACTOR_MODEL + "-resolved",
            JUDGE_MODEL + "-resolved",
        ]
        assert client.messages.calls == (2 if outcome == "success" else 1)
    assert report["live_acceptance"] == ("PASS" if outcome == "success" else "INCOMPLETE")
    if outcome == "missing_usage":
        assert report["token_accounting"]["actual_cost_usd"] is None
        assert report["token_accounting"]["reserved_cost_usd"] == "0.011"
        assert report["reforecast_required"]
