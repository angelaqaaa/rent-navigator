"""Read-only gold loading with explicit approval and corpus provenance checks."""

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from rent_navigator.corpus import Corpus
from rent_navigator.eval.models import CASE_IDS, Activation, GoldApproval, GoldCase


@dataclass(frozen=True)
class GoldDataset:
    cases: tuple[GoldCase, ...]
    gold_hash: str
    approval_hash: str
    approval: GoldApproval
    activation: Activation


def load_gold(data_dir: Path, corpus: Corpus, require_approved: bool = True) -> GoldDataset:
    """Validate bytes and references without executing retrieval, tools or providers.

    ``require_approved=False`` only enables read-only candidate validation. Callers
    must retain the default for every execution or scoring path.
    """
    gold_bytes = (data_dir / "gold.jsonl").read_bytes()
    approval_bytes = (data_dir / "gold-approval.json").read_bytes()
    approval = GoldApproval.model_validate_json(approval_bytes)
    activation = Activation.model_validate_json((data_dir / "activation.json").read_bytes())
    gold_hash = sha256(gold_bytes).hexdigest()
    if approval.gold_sha256 != gold_hash or approval.corpus_hash != corpus.corpus_hash:
        raise ValueError("gold approval hashes do not match dataset and supplied corpus")
    if require_approved and approval.status != "approved":
        raise ValueError("formal gold execution requires explicit approval of this dataset")
    if activation.baseline_phase == "active" and not (data_dir / "baseline.json").is_file():
        raise ValueError("active comparison requires baseline.json")
    cases = tuple(GoldCase.model_validate_json(line) for line in gold_bytes.splitlines())
    if tuple(case.id for case in cases) != CASE_IDS:
        raise ValueError("gold must contain exactly the sixteen fixed cases in contract order")
    canonical_ids = {chunk.id for chunk in corpus.chunks}
    if any(
        not set(case.evidence_ids) <= canonical_ids or not set(case.relevance) <= canonical_ids
        for case in cases
    ):
        raise ValueError("gold evidence or relevance IDs do not resolve in the supplied corpus")
    mixed_questions = sum(
        case.kind == "qa" and set(case.relevance.values()) == {1, 2} for case in cases
    )
    if mixed_questions < 2:
        raise ValueError("at least two questions require both relevance grades")
    return GoldDataset(
        cases=cases,
        gold_hash=gold_hash,
        approval_hash=sha256(approval_bytes).hexdigest(),
        approval=approval,
        activation=activation,
    )
