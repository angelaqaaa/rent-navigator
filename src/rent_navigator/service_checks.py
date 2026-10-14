"""Essential serving checks shared by HTTP requests and local collection."""

from datetime import UTC, date, datetime

from rent_navigator.provider import ProviderFailure


def utc_today() -> date:
    return datetime.now(UTC).date()


def require_fresh_corpus(snapshot_date: date, *, today: date | None = None) -> None:
    """An age of exactly ninety days remains within the serving window."""
    if ((utc_today() if today is None else today) - snapshot_date).days > 90:
        raise ProviderFailure("stale_corpus")
