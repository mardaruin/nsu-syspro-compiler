from __future__ import annotations
from dataclasses import dataclass
from enum import Enum

class TokenType(str, Enum):
    RETURN = "RETURN"
    VAR = "VAR"
    VAL = "VAL"

    INT = "INT"
    IDENT = "IDENT"
    ASSIGN = "ASSIGN"

    PLUS = "PLUS"
    MINUS = "MINUS"
    MULT = "MULT"
    DIV = "DIV"

    LPAREN = "LPAREN"
    RPAREN = "RPAREN"

    SEMI = "SEMI"
    EOF = "EOF"

    ERROR = "ERROR"

KEYWORDS: dict[str, TokenType] = {
    "return": TokenType.RETURN,
    "var": TokenType.VAR,
    "val": TokenType.VAL,
}

@dataclass(frozen=True)
class Token:
    type: TokenType
    value: str
    line: int
    column: int

    def to_json(self):
        return {
            "kind": self.type.value,
            "value": self.value,
            "line": self.line,
            "column": self.column,
        }
