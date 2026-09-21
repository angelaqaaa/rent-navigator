"""Metadata-only timing and exact token accounting; no provider requests."""

import json
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from types import TracebackType
from typing import Annotated, Final, Literal, TextIO

from pydantic import (
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_serializer,
    field_validator,
    model_validator,
)

from rent_navigator.models import (
    CanonicalUUID,
    CheckId,
    CheckStatus,
    ErrorCode,
    Sha256,
    SourceCommit,
    StrictModel,
    ToolName,
)

ACTOR_MODEL: Final = "claude-haiku-4-5-20251001"
JUDGE_MODEL: Final = "claude-sonnet-5"
RequestedModel = Literal["claude-haiku-4-5-20251001", "claude-sonnet-5"]
ReturnedModel = Annotated[
    str,
    StringConstraints(pattern=r"^claude-[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=100),
]
Phase = Literal["extraction", "analysis", "judge"]
Operation = Literal["generation", "count_tokens"]
Stage = Literal["redaction", "preflight", "retrieval", "tool_execution", "generation", "validation"]
ResponseCode = Literal["ok", "answered", "refused"] | ErrorCode
NonNegativeInt = Annotated[int, Field(ge=0)]
Milliseconds = Annotated[float, Field(ge=0, allow_inf_nan=False)]
Usd = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]

_PRICING = {
    ACTOR_MODEL: {"input_per_million": "1", "output_per_million": "5", "max_output": 600},
    JUDGE_MODEL: {"input_per_million": "2", "output_per_million": "10", "max_output": 800},
    "reserved_input_tokens": 8000,
}
PRICING_HASH = sha256(
    json.dumps(_PRICING, sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()


class MetadataModel(StrictModel):
    model_config = ConfigDict(frozen=True, revalidate_instances="always")


class Usage(MetadataModel):
    """Billed token categories; extra categories require explicit reconciliation."""

    input_tokens: NonNegativeInt | None
    output_tokens: NonNegativeInt | None


class CostSummary(Usage):
    usage_complete: bool
    actual_cost_usd: Usd | None
    reserved_cost_usd: Usd

    @model_validator(mode="after")
    def validate_completeness(self) -> "CostSummary":
        complete = self.input_tokens is not None and self.output_tokens is not None
        if self.usage_complete != complete or complete != (self.actual_cost_usd is not None):
            raise ValueError("Usage completeness and actual cost must agree")
        return self

    @field_serializer("actual_cost_usd", "reserved_cost_usd")
    def serialize_cost(self, value: Decimal | None) -> str | None:
        if value is None:
            return None
        return format(value, "f").rstrip("0").rstrip(".") if value % 1 else str(int(value))


def cost_for_usage(model_id: RequestedModel, usage: Usage | None) -> CostSummary:
    """Use the frozen rates, retaining the full reservation when usage is missing."""
    if model_id == ACTOR_MODEL:
        input_rate, output_rate, max_output = Decimal(1), Decimal(5), 600
    elif model_id == JUDGE_MODEL:
        input_rate, output_rate, max_output = Decimal(2), Decimal(10), 800
    else:
        raise ValueError("Unsupported requested model")
    reserved = (8000 * input_rate + max_output * output_rate) / 1_000_000
    input_tokens = usage.input_tokens if usage is not None else None
    output_tokens = usage.output_tokens if usage is not None else None
    complete = input_tokens is not None and output_tokens is not None
    actual = (
        (input_tokens * input_rate + output_tokens * output_rate) / 1_000_000
        if input_tokens is not None and output_tokens is not None
        else None
    )
    return CostSummary(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        usage_complete=complete,
        actual_cost_usd=actual,
        reserved_cost_usd=reserved,
    )


class StageDuration(MetadataModel):
    stage: Stage
    duration_ms: Milliseconds
    status: Literal["completed", "failed"]


class CheckMetadata(MetadataModel):
    id: CheckId
    status: CheckStatus


class TraceContext(MetadataModel):
    attempt_id: CanonicalUUID | None
    trace_id: CanonicalUUID
    phase: Phase
    source_commit: SourceCommit
    config_hash: Sha256
    corpus_hash: Sha256 | None
    pricing_hash: Sha256

    @field_validator("pricing_hash")
    @classmethod
    def validate_pricing_hash(cls, value: str) -> str:
        if value != PRICING_HASH:
            raise ValueError("Pricing hash does not match the fixed rates")
        return value


class TraceRecord(TraceContext, CostSummary):
    timestamp: datetime
    record_kind: Literal["endpoint", "provider_call"]
    provider_call_index: Annotated[int, Field(gt=0)] | None
    provider_operation: Operation | None
    requested_model_id: RequestedModel | None
    returned_model_id: ReturnedModel | None
    duration_ms: Milliseconds
    stage_durations: tuple[StageDuration, ...]
    tool_name: ToolName | None
    check_statuses: tuple[CheckMetadata, ...]
    response_code: ResponseCode
    retrieved_evidence_ids: tuple[Sha256, ...]
    cited_evidence_ids: tuple[Sha256, ...]

    @field_validator("timestamp")
    @classmethod
    def validate_utc(cls, value: datetime) -> datetime:
        offset = value.utcoffset()
        if offset is None or offset.total_seconds() != 0:
            raise ValueError("Trace timestamps must be UTC")
        return value

    @model_validator(mode="after")
    def validate_record(self) -> "TraceRecord":
        if self.record_kind == "endpoint":
            if self.provider_call_index is not None or self.provider_operation is not None:
                raise ValueError("Endpoint summaries cannot identify a provider call")
        else:
            if self.provider_call_index is None or self.provider_operation is None:
                raise ValueError("Provider records require call identity and operation")
            if self.requested_model_id is None:
                raise ValueError("Provider records require the requested model")
            if self.provider_operation == "generation":
                expected = cost_for_usage(
                    self.requested_model_id,
                    Usage(input_tokens=self.input_tokens, output_tokens=self.output_tokens),
                )
            else:
                expected = _zero_cost()
            for field in CostSummary.model_fields:
                if getattr(self, field) != getattr(expected, field):
                    raise ValueError("Provider accounting does not match its operation and usage")
        check_ids = [check.id for check in self.check_statuses]
        if len(check_ids) != len(set(check_ids)):
            raise ValueError("Duplicate check metadata")
        if len(self.retrieved_evidence_ids) != len(set(self.retrieved_evidence_ids)):
            raise ValueError("Duplicate retrieved evidence IDs")
        if self.cited_evidence_ids != tuple(sorted(set(self.cited_evidence_ids))):
            raise ValueError("Cited evidence IDs must be sorted and unique")
        return self


def _zero_cost() -> CostSummary:
    return CostSummary(
        input_tokens=0,
        output_tokens=0,
        usage_complete=True,
        actual_cost_usd=Decimal(0),
        reserved_cost_usd=Decimal(0),
    )


def provider_cost_totals(records: Iterable[TraceRecord]) -> CostSummary:
    """Count details once, rejecting duplicates or mismatched endpoint summaries."""
    checked = [TraceRecord.model_validate(record) for record in records]
    details = [record for record in checked if record.record_kind == "provider_call"]
    identities = {(record.trace_id, record.provider_call_index) for record in details}
    if len(identities) != len(details):
        raise ValueError("Duplicate provider call record")
    for trace_id in {record.trace_id for record in details}:
        indices = sorted(
            record.provider_call_index
            for record in details
            if record.trace_id == trace_id and record.provider_call_index is not None
        )
        if indices != list(range(1, len(indices) + 1)):
            raise ValueError("Incomplete provider call collection")
    endpoints = [record for record in checked if record.record_kind == "endpoint"]
    if len({record.trace_id for record in endpoints}) != len(endpoints):
        raise ValueError("Duplicate endpoint summary")
    for endpoint in endpoints:
        expected = _sum_costs(
            [record for record in details if record.trace_id == endpoint.trace_id]
        )
        if any(
            getattr(endpoint, field) != getattr(expected, field)
            for field in CostSummary.model_fields
        ):
            raise ValueError("Endpoint summary does not match provider call details")
    return _sum_costs(details)


def _sum_costs(details: list[TraceRecord]) -> CostSummary:
    if not details:
        return _zero_cost()
    complete_input = all(record.input_tokens is not None for record in details)
    complete_output = all(record.output_tokens is not None for record in details)
    return CostSummary(
        input_tokens=sum(record.input_tokens or 0 for record in details)
        if complete_input
        else None,
        output_tokens=sum(record.output_tokens or 0 for record in details)
        if complete_output
        else None,
        usage_complete=complete_input and complete_output,
        actual_cost_usd=(
            sum((record.actual_cost_usd or Decimal(0) for record in details), Decimal(0))
            if complete_input and complete_output
            else None
        ),
        reserved_cost_usd=sum((record.reserved_cost_usd for record in details), Decimal(0)),
    )


class UnsafeMetadataError(ValueError):
    """Safe rejection without including rejected values or validation details."""


class MetadataSink:
    """Write validated JSON lines to a caller-owned stream, with no payload logging."""

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream

    def emit(self, record: TraceRecord) -> None:
        try:
            if type(record) is not TraceRecord:
                raise ValueError("Unexpected metadata type")
            checked = TraceRecord.model_validate(record)
        except (ValidationError, ValueError, TypeError):
            raise UnsafeMetadataError("Invalid trace metadata") from None
        self._stream.write(checked.model_dump_json() + "\n")


class ProviderCall:
    """A call's safe completion fields; exceptions themselves are never retained."""

    def __init__(self) -> None:
        self.usage: Usage | None = None
        self.returned_model_id: ReturnedModel | None = None
        self.response_code: ResponseCode = "ok"


class TraceRecorder:
    """Measure an endpoint; finish explicitly or capture failure on context exit."""

    def __init__(
        self,
        context: TraceContext,
        sink: MetadataSink,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        utc_now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._context = context
        self._sink = sink
        self._monotonic = monotonic
        self._utc_now = utc_now
        self._started = monotonic()
        self._timestamp = utc_now()
        self._stages: list[StageDuration] = []
        self._calls: list[TraceRecord] = []
        self._finished = False
        self._active_scopes = 0
        self._next_call_index = 0

    def __enter__(self) -> "TraceRecorder":
        self._ensure_open()
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        _exception: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        if not self._finished:
            self.finish("provider_error" if exception_type is not None else "ok")

    def _ensure_open(self) -> None:
        if self._finished:
            raise ValueError("Trace is already finished")

    @contextmanager
    def stage(self, stage: Stage) -> Iterator[None]:
        self._ensure_open()
        started = self._monotonic()
        self._active_scopes += 1
        status: Literal["completed", "failed"] = "completed"
        try:
            yield
        except BaseException:
            status = "failed"
            raise
        finally:
            self._active_scopes -= 1
            self._stages.append(
                StageDuration(
                    stage=stage,
                    duration_ms=(self._monotonic() - started) * 1000,
                    status=status,
                )
            )

    @contextmanager
    def provider_call(
        self, model_id: RequestedModel, *, operation: Operation = "generation"
    ) -> Iterator[ProviderCall]:
        self._ensure_open()
        if model_id not in (ACTOR_MODEL, JUDGE_MODEL) or operation not in (
            "generation",
            "count_tokens",
        ):
            raise ValueError("Unsupported provider call configuration")
        started, timestamp = self._monotonic(), self._utc_now()
        self._active_scopes += 1
        self._next_call_index += 1
        call_index = self._next_call_index
        call = ProviderCall()
        try:
            yield call
        except BaseException:
            if call.response_code == "ok":
                call.response_code = "provider_error"
            raise
        finally:
            self._active_scopes -= 1
            # Token counting is unbilled, so zero here describes known billed usage.
            cost = (
                cost_for_usage(model_id, call.usage) if operation == "generation" else _zero_cost()
            )
            # Preserve accounting independently of optional completion metadata.
            record = self._record(
                cost,
                timestamp=timestamp,
                record_kind="provider_call",
                provider_call_index=call_index,
                provider_operation=operation,
                requested_model_id=model_id,
                returned_model_id=None,
                duration_ms=(self._monotonic() - started) * 1000,
                stage_durations=(),
                response_code="provider_error",
            )
            invalid_completion = False
            try:
                record = TraceRecord.model_validate(
                    record.model_copy(
                        update={
                            "returned_model_id": call.returned_model_id,
                            "response_code": call.response_code,
                        }
                    )
                )
            except ValidationError:
                invalid_completion = True
            self._calls.append(record)
            self._sink.emit(record)
            if invalid_completion:
                raise UnsafeMetadataError("Invalid provider completion metadata") from None

    def _record(
        self,
        cost: CostSummary,
        *,
        timestamp: datetime,
        record_kind: Literal["endpoint", "provider_call"],
        provider_call_index: int | None,
        provider_operation: Operation | None,
        requested_model_id: RequestedModel | None,
        returned_model_id: ReturnedModel | None,
        duration_ms: float,
        stage_durations: tuple[StageDuration, ...],
        response_code: ResponseCode,
        tool_name: ToolName | None = None,
        check_statuses: tuple[CheckMetadata, ...] = (),
        retrieved_evidence_ids: tuple[Sha256, ...] = (),
        cited_evidence_ids: tuple[Sha256, ...] = (),
    ) -> TraceRecord:
        return TraceRecord(
            attempt_id=self._context.attempt_id,
            trace_id=self._context.trace_id,
            phase=self._context.phase,
            source_commit=self._context.source_commit,
            config_hash=self._context.config_hash,
            corpus_hash=self._context.corpus_hash,
            pricing_hash=self._context.pricing_hash,
            input_tokens=cost.input_tokens,
            output_tokens=cost.output_tokens,
            usage_complete=cost.usage_complete,
            actual_cost_usd=cost.actual_cost_usd,
            reserved_cost_usd=cost.reserved_cost_usd,
            timestamp=timestamp,
            record_kind=record_kind,
            provider_call_index=provider_call_index,
            provider_operation=provider_operation,
            requested_model_id=requested_model_id,
            returned_model_id=returned_model_id,
            duration_ms=duration_ms,
            stage_durations=stage_durations,
            tool_name=tool_name,
            check_statuses=check_statuses,
            response_code=response_code,
            retrieved_evidence_ids=retrieved_evidence_ids,
            cited_evidence_ids=cited_evidence_ids,
        )

    def finish(
        self,
        response_code: ResponseCode,
        *,
        tool_name: ToolName | None = None,
        check_statuses: tuple[CheckMetadata, ...] = (),
        retrieved_evidence_ids: tuple[Sha256, ...] = (),
        cited_evidence_ids: tuple[Sha256, ...] = (),
    ) -> TraceRecord:
        self._ensure_open()
        if self._active_scopes:
            raise ValueError("Cannot finish with active trace scopes")
        requested = {record.requested_model_id for record in self._calls}
        returned = {record.returned_model_id for record in self._calls}
        record = self._record(
            provider_cost_totals(self._calls),
            timestamp=self._timestamp,
            record_kind="endpoint",
            provider_call_index=None,
            provider_operation=None,
            requested_model_id=next(iter(requested)) if len(requested) == 1 else None,
            returned_model_id=next(iter(returned)) if len(returned) == 1 else None,
            duration_ms=(self._monotonic() - self._started) * 1000,
            stage_durations=tuple(self._stages),
            response_code=response_code,
            tool_name=tool_name,
            check_statuses=check_statuses,
            retrieved_evidence_ids=retrieved_evidence_ids,
            cited_evidence_ids=cited_evidence_ids,
        )
        self._sink.emit(record)
        self._finished = True
        return record
