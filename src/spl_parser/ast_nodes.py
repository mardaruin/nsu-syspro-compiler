from __future__ import annotations
from dataclasses import dataclass, field


class Node:
    line: int
    column: int

class Expr(Node):
    pass

class Stmt(Node):
    pass

@dataclass
class Error(Expr):
    line: int
    column: int
    message: str

@dataclass
class IntLiteral(Expr):
    line: int
    column: int
    value: int

@dataclass
class Ident(Expr):
    line: int
    column: int
    name: str

@dataclass
class BinOp(Expr):
    line: int
    column: int
    op: str
    left: Expr
    right: Expr

@dataclass
class UnaryOp(Expr):
    line: int
    column: int
    op: str
    operand: Expr


@dataclass
class Assign(Stmt):
    line: int
    column: int
    target: Ident
    value: Expr

@dataclass
class Declare(Stmt):
    line: int
    column: int
    mut: str # val | var
    name: str
    init: Expr

@dataclass
class Return(Stmt):
    line: int
    column: int
    value: Expr

@dataclass
class ExprStmt(Stmt):
    """expressionStatement ::= expression ';'"""
    line: int
    column: int
    value: Expr

@dataclass
class Program(Node):
    line: int
    column: int
    body: list[Stmt] = field(default_factory=list)