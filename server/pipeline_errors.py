"""Pipeline error taxonomy — reconstructed from .pyc after git reset."""


class PipelineError(Exception):
    """Base exception for all pipeline errors."""

    def __init__(self, message: str, stage: str = "", retryable: bool = False) -> None:
        super().__init__(message)
        self.stage = stage
        self.retryable = retryable


class RetryableError(PipelineError):
    """Error that can be retried."""

    def __init__(self, message: str, stage: str = "") -> None:
        super().__init__(message, stage=stage, retryable=True)


class WorkerUnavailableError(RetryableError):
    pass


class ValidationError(PipelineError):
    pass


class ArtifactValidationError(ValidationError):
    pass
