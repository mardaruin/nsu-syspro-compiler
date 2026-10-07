from __future__ import annotations

from lexer.tokens import Token, TokenType
from .ast_nodes import (
    Node, Expr, Stmt, IntLiteral, Ident, BinOp, UnaryOp,
    Assign, Declare, Return, ExprStmt, Program, Error,
)

class Parser:
    def __init__(self, tokens: list[Token]):
        self.tokens = tokens
        self.pos = 0
        self._declared: dict[str, str] = {}
        self.errors: list[Error] = []

    def _peek(self, k: int=0):
        j = self.pos + k
        if j >= len(self.tokens):
            return self.tokens[-1]
        return self.tokens[j]

    def _check(self, type: TokenType):
        return self._peek().type == type

    def _at_end(self):
        return self._peek().type == TokenType.EOF

    def _advance(self) -> Token:
        t = self._peek()
        if not self._at_end():
            self.pos += 1
        return t

    def _error_at_current(self) -> Error:
        t = self._peek()
        return Error(line=t.line, column=t.column, message=f"unexpected token '{t.type.name}'")

    def _sync_error_at_current(self) -> Error:
        t = self._peek()
        err = Error(line=t.line, column=t.column, message=f"unexpected token '{t.type.name}'")
        self.errors.append(err)
        self._synchronize()
        return err

    def _synchronize(self):
        while not self._at_end() and self._peek().type not in (TokenType.SEMI, TokenType.EOF,):
            self._advance()
        if self._check(TokenType.SEMI):
            self._advance()

    def parse(self) -> Program:
        self._declared = {}
        self.errors = []
        body: list[Stmt] = []
        while not self._at_end():
            before = self.pos
            stmt = self.parse_statement()
            body.append(stmt)
            if isinstance(stmt, Error) and self.pos == before:
                self._synchronize()

        if body:
            last = body[-1]
            return Program(line=last.line, column=last.column, body=body)
        first = self.tokens[0] if self.tokens else None
        return Program(line=first.line if first else 1,
                       column=first.column if first else 1,
                       body=body)


    def parse_statement(self) -> Stmt | Error:
        t = self._peek()
        if t.type == TokenType.RETURN:
            return self._parse_return()
        if t.type in (TokenType.VAR, TokenType.VAL):
            return self._parse_declare()
        if t.type == TokenType.IDENT and self._peek(1).type == TokenType.ASSIGN:
            return self._parse_assign()
        return self._parse_expr_stmt()

    def _parse_return(self) -> Return | Error:
        self._advance()

        if self._check(TokenType.SEMI):
            semi = self._peek()
            err = Error(line=semi.line, column=semi.column, message="unexpected token 'SEMI'")
            self._advance()
            return Return(line=semi.line, column=semi.column, value=err)

        value = self._parse_expr()
        semi = self._consume_semi()

        if semi:
            return Return(line=semi.line, column=semi.column, value=value)
        return self._sync_error_at_current()

    def _parse_declare(self) -> Declare | Error:
        it = self._advance()
        mut = it.value

        if self._check(TokenType.IDENT):
            tok = self._advance()
            name = tok.value
        else:
            name = ""
            err = self._error_at_current()
            while not self._at_end() and not self._check(TokenType.SEMI):
                self._advance()
            semi = self._consume_semi()
            line = semi.line if semi else err.line
            column = semi.column if semi else err.column
            return Declare(line=line, column=column, mut=mut, name=name, init=err)

        if self._check(TokenType.ASSIGN):
            self._advance()
            init = self._parse_expr()
        else:
            return self._sync_error_at_current()

        semi = self._consume_semi()

        if name and name in self._declared:
            self.errors.append(Error(line=tok.line, column=tok.column, message=f"redeclaration of '{name}'"))
        else:
            self._declared[name] = mut

        if semi:
            return Declare(line=semi.line, column=semi.column, mut=mut, name=name, init=init)
        err = self._sync_error_at_current()
        return Declare(line=err.line, column=err.column, mut=mut, name=name, init=err)

    def _parse_assign(self) -> Assign | Error:
        name_t = self._advance()
        self._advance()
        value = self._parse_expr()
        semi = self._consume_semi()
        target=Ident(line=name_t.line, column=name_t.column, name=name_t.value)
        if name_t.value not in self._declared:
            self.errors.append(Error(line=name_t.line, column=name_t.column,
                                     message=f"undefined variable '{name_t.value}'"))
        elif self._declared.get(name_t.value) == "val":
            self.errors.append(Error(line=name_t.line, column=name_t.column,
                                     message=f"cannot assign to immutable '{name_t.value}'"))
        if semi:
            return Assign(line=semi.line, column=semi.column,
                              target=target,
                              value=value)
        return self._sync_error_at_current()

    def _parse_expr_stmt(self) -> ExprStmt | Error:
        value = self._parse_expr()
        semi = self._consume_semi()
        if semi:
            return ExprStmt(line=semi.line, column=semi.column, value=value)
        return self._sync_error_at_current()

    def _consume_semi(self) -> Token | None:
        if self._check(TokenType.SEMI):
            return self._advance()
        return None

    def _parse_expr(self) -> Expr:
        return self._parse_additive()

    def _parse_additive(self) -> Expr:
        left = self._parse_multiplicative()
        while True:
            t = self._peek()
            if t.type in (TokenType.PLUS, TokenType.MINUS):
                op = self._advance()
                right = self._parse_multiplicative()
                left = BinOp(line=right.line, column=right.column,
                             op=op.value, left=left, right=right)
            else:
                break
        return left

    def _parse_multiplicative(self) -> Expr:
        left = self._parse_unary()
        while True:
            t = self._peek()
            if t.type in (TokenType.MULT, TokenType.DIV):
                op = self._advance()
                right = self._parse_unary()
                left = BinOp(line=right.line, column=right.column,
                             op=op.value, left=left, right=right)
            else:
                break
        return left

    def _parse_unary(self) -> Expr:
        t = self._peek()
        if t.type == TokenType.MINUS:
            op = self._advance()
            operand = self._parse_unary()
            return UnaryOp(line=op.line, column=op.column, op=op.value, operand=operand)
        return self._parse_primary()

    def _parse_primary(self) -> Expr | Error:
        t = self._peek()
        if t.type == TokenType.INT:
            self._advance()
            return IntLiteral(line=t.line, column=t.column, value=int(t.value))
        if t.type == TokenType.IDENT:
            self._advance()
            return Ident(line=t.line, column=t.column, name=t.value)
        if t.type == TokenType.LPAREN:
            self._advance()
            expr = self._parse_expr()
            if self._check(TokenType.RPAREN):
                self._advance()
                return expr
            err = Error(line=self._peek().line, column=self._peek().column,
                         message=f"unclosed lparen'")
            self.errors.append(err)
            return expr
        return self._error_at_current()

