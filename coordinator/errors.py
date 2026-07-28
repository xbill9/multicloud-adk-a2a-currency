from enum import StrEnum


class FailureKind(StrEnum):
    VALIDATION = "validation"
    PROVIDER = "provider"
    AUTHENTICATION = "authentication"
    TRANSPORT = "transport"
    TIMEOUT = "timeout"
    PROTOCOL = "protocol"


class AdapterError(RuntimeError):
    def __init__(self, kind: FailureKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind

    def safe_message(self) -> str:
        return f"{self.kind.value}: {self}"
