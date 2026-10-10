"""User-facing failures with content-safe messages."""

from session_migrate.model import TargetFormat


class SessionMigrateError(Exception):
    """Base error for expected parse, validation, and installation failures."""


class JsonlError(SessionMigrateError):
    """A JSONL stream is unreadable or structurally invalid."""


class FormatDetectionError(SessionMigrateError):
    """The agent session format cannot be identified safely."""


class NativeImportPartialError(SessionMigrateError):
    """Native import returned success; local verification/finalization then failed."""

    import_succeeded = True
    needs_reconcile = True

    def __init__(
        self,
        message: str,
        *,
        target_format: TargetFormat,
        session_id: str,
        manifest: str | None = None,
        root: str | None = None,
        stage: str | None = None,
    ) -> None:
        self.target_format = target_format
        self.session_id = session_id
        self.manifest = manifest
        self.root = root
        self.stage = stage
        super().__init__(message)


class PublishedArtifactPartialError(SessionMigrateError):
    """Convert output exists, but its separate title metadata could not be finalized."""

    publication_succeeded = True
    import_succeeded = False
    needs_reconcile = True

    def __init__(
        self,
        *,
        target_format: TargetFormat,
        session_id: str,
        output: str,
        manifest: str,
        stage: str,
    ) -> None:
        self.target_format = target_format
        self.session_id = session_id
        self.output = output
        self.manifest = manifest
        self.stage = stage
        super().__init__("Converted artifact exists; title metadata needs reconciliation.")


class OperationCancelledError(SessionMigrateError):
    """An operation stopped before publication began."""


class SourceReadLimitError(SessionMigrateError):
    """A source cannot be read within the requested interactive byte budget."""
