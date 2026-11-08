from __future__ import annotations
from dataclasses import dataclass
from enum import Enum

class TokenType(str, Enum):
    RETURN = "RETURN"
    VAR = "VAR"
    VAL = "VAL"
    IF = "IF"
    ELSE = "ELSE"
    WHILE = "WHILE"
    BREAK = "BREAK"
    CONTINUE = "CONTINUE"
    TRUE = "TRUE"
    FALSE = "FALSE"

    INT = "INT"
    IDENT = "IDENT"
    ASSIGN = "ASSIGN"

    PLUS = "PLUS"
    MINUS = "MINUS"
    MULT = "MULT"
    DIV = "DIV"

    EQ = "EQ"
    NE = "NE"
    LT = "LT"
    GT = "GT"
    LE = "LE"
    GE = "GE"

    AND = "AND"
    OR = "OR"
    NOT = "NOT"

    LPAREN = "LPAREN"
    RPAREN = "RPAREN"
    LBRACE = "LBRACE"
    RBRACE = "RBRACE"

    SEMI = "SEMI"
    EOF = "EOF"

    ERROR = "ERROR"

KEYWORDS: dict[str, TokenType] = {
    "return": TokenType.RETURN,
    "var": TokenType.VAR,
    "val": TokenType.VAL,
    "if": TokenType.IF,
    "else": TokenType.ELSE,
    "while": TokenType.WHILE,
    "break": TokenType.BREAK,
    "continue": TokenType.CONTINUE,
    "true": TokenType.TRUE,
    "false": TokenType.FALSE,
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
