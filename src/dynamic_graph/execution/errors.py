from ..contracts import DynamicGraphError


class RunFailure(DynamicGraphError):
    def __init__(
        self, code, message, *, phase="execution", node_id=None, details=None, retryable=False
    ):
        super().__init__(message)
        self.code, self.phase, self.node_id = code, phase, node_id
        self.details, self.retryable = details or {}, retryable


class ToolCallError(DynamicGraphError):
    def __init__(self, message="Tool failed", *, retryable=False):
        super().__init__(message)
        self.retryable = retryable
