"""Exceptions that communicate non-fatal source failures to orchestration."""


class SourceFetchError(RuntimeError):
    """A source made no successful requests during this pipeline run."""
