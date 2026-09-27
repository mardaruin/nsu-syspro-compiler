from __future__ import annotations

class LexerError(Exception):
    def __init__(self, message: str, line: int, column: int):
        super().__init__(message)
        self.message = message
        self.line = line
        self.column = column

    def to_json(self) -> dict:
        return {"error": self.message, "line": self.line, "column": self.column}

    def __str__(self):
        return f"{self.line}:{self.column}:{self.message}"
