"""Stable failures suitable for command-line reporting without a traceback."""


class MigrationError(RuntimeError):
    def __init__(self, message: str, code: str = "migration_error") -> None:
        super().__init__(message)
        self.code = code
