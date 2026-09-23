"""Shared raw-response, metadata and observed-serving evidence checks."""

import json
from typing import Any
from uuid import UUID

from anthropic.types import Message
from pydantic import TypeAdapter, ValidationError

from rent_navigator.corpus import Corpus
from rent_navigator.eval.collection import _verify_observations
from rent_navigator.eval.models import GoldCase, ResultRow
from rent_navigator.eval.recording import RawProviderRecord, verify_synthetic_records
from rent_navigator.eval.runner import config_map, row_config_hash
from rent_navigator.models import ErrorResponse
from rent_navigator.provider import ProviderFailure, _usage
from rent_navigator.trace import PRICING_HASH, ReturnedModel, TraceRecord, provider_cost_totals


def verify_raw_links(
    raw: list[RawProviderRecord], records: list[TraceRecord], run_id: UUID
) -> None:
    calls = {
        (r.attempt_id, r.trace_id, r.provider_call_index, r.provider_operation): r
        for r in records
        if r.record_kind == "provider_call"
    }
    if len(calls) != sum(r.record_kind == "provider_call" for r in records):
        raise ValueError("Duplicate provider metadata")
    found: dict[tuple[UUID, UUID, int, str], list[str]] = {}
    for item in raw:
        key = (item.attempt_id, item.trace_id, item.operation_index, item.operation)
        call = calls.get(key)
        if item.run_id != run_id or call is None or item.phase != call.phase:
            raise ValueError("Unlinked raw provider record")
        if item.event == "request" and item.value.get("model") != call.requested_model_id:
            raise ValueError("Raw request model differs from metadata")
        if item.event == "failure" and (
            call.response_code == "ok" or item.value.get("code") != call.response_code
        ):
            raise ValueError("Raw failure differs from metadata")
        if item.event == "response":
            if item.operation == "count_tokens":
                tokens = item.value.get("input_tokens")
                if set(item.value) != {"input_tokens"} or (
                    call.response_code == "ok" and (type(tokens) is not int or tokens < 0)
                ):
                    raise ValueError("Invalid token count evidence")
            else:
                if item.value.get("model") != call.requested_model_id and (
                    call.usage_complete
                    or call.actual_cost_usd is not None
                    or call.input_tokens is not None
                    or call.output_tokens is not None
                    or call.response_code != "provider_error"
                ):
                    raise ValueError("Raw model anomaly requires unknown pricing and a safe error")
                try:
                    returned_model: str | None = TypeAdapter(ReturnedModel).validate_python(
                        item.value.get("model"), strict=True
                    )
                except ValidationError:
                    returned_model = None
                if returned_model != call.returned_model_id:
                    raise ValueError("Raw model differs from returned identity")
                if call.usage_complete:
                    raw_usage = item.value.get("usage")
                    if not isinstance(raw_usage, dict) or any(
                        type(raw_usage.get(name)) is not int
                        for name in ("input_tokens", "output_tokens")
                    ):
                        raise ValueError("Raw billed token counts must be strict integers")
                    usage = _usage(Message.model_validate(item.value))
                    if usage is None or (usage.input_tokens, usage.output_tokens) != (
                        call.input_tokens,
                        call.output_tokens,
                    ):
                        raise ValueError("Raw usage differs from priced metadata")
        found.setdefault(key, []).append(item.event)
    if set(found) != set(calls) or any(
        events not in (["request", "response"], ["request", "failure"]) for events in found.values()
    ):
        raise ValueError("Missing, duplicate or reordered raw provider events")
    provider_cost_totals(records)


def verify_gold_observations(
    row: ResultRow,
    case: GoldCase,
    *,
    records: list[TraceRecord],
    raw: list[RawProviderRecord],
    corpus: Corpus,
    run_id: UUID,
    source_sha: str,
    gold_hash: str,
) -> None:
    if (
        row.run_id != run_id
        or row.source_sha != source_sha
        or row.gold_hash != gold_hash
        or row.corpus_hash != corpus.corpus_hash
        or row.pricing_hash != PRICING_HASH
        or row.config_hash != row_config_hash(case, row.arm)
    ):
        raise ValueError("Gold result provenance mismatch")
    if any(r.attempt_id != row.attempt_id for r in records):
        raise ValueError("Unlinked serving metadata")
    endpoints = [r for r in records if r.record_kind == "endpoint"]
    if len(endpoints) != len(row.trace_ids) or {r.trace_id for r in records} != set(row.trace_ids):
        raise ValueError("Missing serving trace")
    configs = config_map()
    for record in records:
        expected = (
            configs["extraction"]
            if record.phase == "extraction"
            else configs[f"{case.request.mode}:{row.arm}"]
        )
        if (
            record.phase == "judge"
            or record.config_hash != expected
            or record.source_commit != source_sha
            or record.corpus_hash != corpus.corpus_hash
        ):
            raise ValueError("Serving trace provenance mismatch")
    cost = provider_cost_totals(records)
    if (cost.input_tokens, cost.output_tokens, cost.actual_cost_usd, cost.usage_complete) != (
        row.input_tokens,
        row.output_tokens,
        row.serving_cost_usd,
        row.usage_complete,
    ):
        raise ValueError("Serving accounting mismatch")
    retrieved = [
        identifier
        for r in endpoints
        if r.phase == "analysis"
        for identifier in r.retrieved_evidence_ids
    ]
    if row.retrieved_ids != retrieved:
        raise ValueError("Retrieval observation mismatch")
    if row.response is not None and (
        row.response.attempt_id != row.attempt_id or row.response.trace_id != row.trace_ids[-1]
    ):
        raise ValueError("Response correlation mismatch")
    if isinstance(row.response, ErrorResponse):
        endpoint = next(r for r in endpoints if r.trace_id == row.response.trace_id)
        if (
            row.response.error.code != endpoint.response_code
            or row.response.error.message != ProviderFailure(row.response.error.code).message
        ):
            raise ValueError("Safe error disagrees with actual endpoint failure")
    verify_synthetic_records(
        raw,
        case=case,
        corpus=corpus,
        arm=row.arm,
        retrieved_ids=tuple(row.retrieved_ids) if row.arm == "production" else None,
    )
    values: list[dict[str, Any]] = [json.loads(r.model_dump_json()) for r in raw]
    _verify_observations(row, case, records, values, corpus)
