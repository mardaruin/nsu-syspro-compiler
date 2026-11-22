from __future__ import annotations

from lexer.tokens import Token, TokenType
from .ast_nodes import (
    Node, Expr, Stmt, IntLiteral, Ident, BinOp, UnaryOp,
    Assign, Declare, Return, ExprStmt, Program, Error,
    If, While, Break, Continue, Block, BoolLiteral,
)

class Parser:
    def __init__(self, tokens: list[Token]):
        self.tokens = tokens
        self.pos = 0
        self._loop_depth = 0
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

    def _expect(self, type: TokenType) -> Token | None:
        if self._check(type):
            return self._advance()
        return None

    def _synchronize(self):
        stop = {
            TokenType.SEMI,
            TokenType.EOF,
            TokenType.RBRACE,
            TokenType.ELSE,
        }
        if self._at_end():
            return
        if self._peek().type in stop:
            self._advance()
            return
        while not self._at_end() and self._peek().type not in stop:
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
            if self.pos == before:
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
        if t.type == TokenType.IF:
            return self._parse_if()
        if t.type == TokenType.WHILE:
            return self._parse_while()
        if t.type == TokenType.BREAK:
            return self._parse_break()
        if t.type == TokenType.CONTINUE:
            return self._parse_continue()
        if t.type == TokenType.LBRACE:
            return self._parse_block()
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

    def _parse_if(self) -> If | Error:
        self._advance()

        if not self._check(TokenType.LPAREN):
            t = self._peek()
            cond = Error(line=t.line, column=t.column, message="missing condition")
            self.errors.append(cond)
            while not self._at_end() and not self._check(TokenType.LBRACE) and not self._check(TokenType.ELSE):
                self._advance()
        else:
            self._advance()

            cond = self._parse_expr()

            if not self._expect(TokenType.RPAREN):
                return self._sync_error_at_current()

        then_branch = self.parse_statement()

        else_branch: Stmt | None = None
        if self._expect(TokenType.ELSE):
            else_branch = self.parse_statement()

        tail = else_branch if else_branch is not None else then_branch
        return If(line=tail.line, column=tail.column, cond=cond, then_branch=then_branch, else_branch=else_branch)

    def _parse_while(self) -> While | Error:
        kw = self._advance()
        self._loop_depth += 1
        try:
            if not self._expect(TokenType.LPAREN):
                return self._sync_error_at_current()

            cond = self._parse_expr()

            if not self._expect(TokenType.RPAREN):
                return self._sync_error_at_current()

            body = self.parse_statement()
        finally:
            self._loop_depth -= 1

        return While(line=body.line, column=body.column, cond=cond, body=body)

    def _parse_break(self) -> Break | Error:
        kw = self._advance()
        # if self._loop_depth == 0:
        #     self.errors.append(Error(line=kw.line, column=kw.column, message="'break' outside of loop"))
        semi = self._consume_semi()
        if not semi:
            return self._sync_error_at_current()
        return Break(line=semi.line, column=semi.column)

    def _parse_continue(self) -> Continue | Error:
        kw = self._advance()
        # if self._loop_depth == 0:
        #     self.errors.append(Error(line=kw.line, column=kw.column, message="'continue' outside of loop"))
        semi = self._consume_semi()
        if not semi:
            return self._sync_error_at_current()
        return Continue(line=semi.line, column=semi.column)

    def _parse_block(self) -> Block | Error:
        lb = self._advance() # '{'
        body: list[Stmt] = []
        while not self._at_end() and not self._check(TokenType.RBRACE):
            before = self.pos
            stmt = self.parse_statement()
            body.append(stmt)
            if self.pos == before:
                self._synchronize()

        if self._check(TokenType.RBRACE):
            rb = self._advance()
            return Block(line=rb.line, column=rb.column, body=body)
        err = self._sync_error_at_current()
        return Block(line=err.line, column=err.column, body=body)

    def _parse_expr(self) -> Expr:
        return self._parse_logical_or_expr()

    def _parse_logical_or_expr(self) -> Expr:
        left = self._parse_logical_and_expr()
        while self._check(TokenType.OR):
            op = self._advance()
            right = self._parse_logical_and_expr()
            left = BinOp(line=right.line, column=right.column, op=op.value, left=left, right=right)

        return left

    def _parse_logical_and_expr(self) -> Expr:
        left = self._parse_equality_expr()
        while self._check(TokenType.AND):
            op = self._advance()
            right = self._parse_equality_expr()
            left = BinOp(line=right.line, column=right.column, op=op.value, left=left, right=right)

        return left

    def _parse_equality_expr(self) -> Expr:
        left = self._parse_relational_expr()
        while self._peek().type in (TokenType.EQ, TokenType.NE):
            op = self._advance()
            right = self._parse_relational_expr()
            left = BinOp(line=right.line, column=right.column, op=op.value, left=left, right=right)

        return left

    def _parse_relational_expr(self) -> Expr:
        left = self._parse_additive()
        while self._peek().type in (TokenType.LT, TokenType.LE, TokenType.GT, TokenType.GE):
            op = self._advance()
            right = self._parse_additive()
            left = BinOp(line=right.line, column=right.column, op=op.value, left=left, right=right)

        return left

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
        if t.type in (TokenType.MINUS, TokenType.NOT):
            op = self._advance()
            operand = self._parse_unary()
            return UnaryOp(line=op.line, column=op.column, op=op.value, operand=operand)
        return self._parse_primary()

    def _parse_primary(self) -> Expr | Error:
        t = self._peek()
        if t.type == TokenType.INT:
            self._advance()
            return IntLiteral(line=t.line, column=t.column, value=int(t.value))
        if t.type == TokenType.TRUE:
            self._advance()
            return BoolLiteral(line=t.line, column=t.column, value=True)
        if t.type == TokenType.FALSE:
            self._advance()
            return BoolLiteral(line=t.line, column=t.column, value=False)
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
                         message=f"unclosed lparen")
            self.errors.append(err)
            return expr
        return self._error_at_current()

