from __future__ import annotations
from llvmlite import ir

from spl_parser.ast_nodes import (
    Expr, Stmt, IntLiteral, BoolLiteral, Ident, BinOp as AstBinOp, UnaryOp as AstUnaryOp,
    Assign, Declare, Return as AstReturn, ExprStmt, Program, Error as AstError,
    If, While, Break, Continue, Block,
)

from .errors import CodegenError

I64 = ir.IntType(64)
ALIGN = 8

ZERO = ir.Constant(I64, 0)
ONE = ir.Constant(I64, 1)

class LLVMCodeGen:
    def __init__(self):
        self.module: ir.Module | None = None
        self.builder: ir.IRBuilder | None = None
        self.main_fn: ir.Function | None = None
        self.env: dict[str, ir.AllocaInstr] = {}
        self.loop_stack: list[tuple[ir.BasicBlock, ir.BasicBlock]] = []

    def generate(self, program: Program) -> ir.Module:
        self.module = ir.Module(name="spl")
        self.module.source_filename = "spl"
        self.module.triple = "x86_64-unknown-linux-gnu"
        self.module.data_layout = (
            "e-m:e-p270:32:32-p271:32:32-p272:64:64-i64:64-i128:128-"
            "f80:128-n8:16:32:64-S128"
        )
        fnty = ir.FunctionType(I64, [])
        self.main_fn = ir.Function(self.module, fnty, name="main")
        entry = self.main_fn.append_basic_block(name="entry")
        self.builder = ir.IRBuilder(entry)
        self.env = {}
        self.loop_stack = []

        for stmt in program.body:
            if self._terminated():
                break
            self._gen_stmt(stmt)

        if not self._terminated():
            self.builder.ret(ZERO)

        return self.module

    def _terminated(self) -> bool:
        return self.builder.block is not None and self.builder.block.is_terminated

    def _branch_if_open(self, target: ir.BasicBlock) -> None:
        if not self._terminated():
            self.builder.branch(target)

    def _to_bool_i64(self, value: ir.Value) -> ir.Value:
        cmp = self.builder.icmp_signed("!=", value, ZERO)
        return self.builder.zext(cmp, I64)

    def _gen_stmt(self, node: Stmt) -> None:
        if isinstance(node, AstError):
            return
        if isinstance(node, Declare):
            self._gen_declare(node)
        elif isinstance(node, Assign):
            self._gen_assign(node)
        elif isinstance(node, AstReturn):
            self._gen_return(node)
        elif isinstance(node, ExprStmt):
            self._gen_expr_stmt(node)
        elif isinstance(node, If):
            self._gen_if(node)
        elif isinstance(node, While):
            self._gen_while(node)
        elif isinstance(node, Break):
            self._gen_break(node)
        elif isinstance(node, Continue):
            self._gen_continue(node)
        elif isinstance(node, Block):
            self._gen_block(node)
        else:
            raise CodegenError(f"unknown statement: {type(node).__name__}")

    def _gen_declare(self, node: Declare) -> None:
        with self.builder.goto_entry_block():
            ptr = self.builder.alloca(I64, name=node.name or "tmp")
        value = self._gen_expr(node.init)
        self.builder.store(value, ptr)
        self.env[node.name] = ptr


    def _gen_assign(self, node: Assign) -> None:
        if node.target.name not in self.env:
            raise CodegenError(f"assignment to undeclared '{node.target.name}'")
        value = self._gen_expr(node.value)
        self.builder.store(value, self.env[node.target.name])

    def _gen_return(self, node: AstReturn) -> None:
        value = self._gen_expr(node.value)
        self.builder.ret(value)

    def _gen_expr_stmt(self, node: ExprStmt) -> None:
        self._gen_expr(node.value)

    def _gen_if(self, node: If) -> None:
        cond = self._gen_expr(node.cond)
        is_true = self.builder.icmp_signed("!=", cond, ZERO)

        then_bb = self.builder.append_basic_block("if.then")
        merge_bb = self.builder.append_basic_block("if.end")
        else_bb = None

        if node.else_branch is not None:
            else_bb = self.builder.append_basic_block("if.else")

        if else_bb is not None:
            self.builder.cbranch(is_true, then_bb, else_bb)
        else:
            self.builder.cbranch(is_true, then_bb, merge_bb)

        self.builder.position_at_start(then_bb)
        self._gen_stmt(node.then_branch)
        then_falls = not self._terminated()
        self._branch_if_open(merge_bb)

        if node.else_branch is not None:
            self.builder.position_at_start(else_bb)
            self._gen_stmt(node.else_branch)
            else_falls = not self._terminated()
            self._branch_if_open(merge_bb)
        else:
            else_falls = True

        self.builder.position_at_start(merge_bb)

        if not (then_falls or else_falls):
            self.builder.unreachable()



    def _gen_while(self, node: While) -> None:
        cond_bb = self.builder.append_basic_block("while.cond")
        body_bb = self.builder.append_basic_block("while.body")
        exit_bb = self.builder.append_basic_block("while.end")

        self._branch_if_open(cond_bb)

        self.builder.position_at_start(cond_bb)
        cond = self._gen_expr(node.cond)
        is_true = self.builder.icmp_signed("!=", cond, ZERO)
        self.builder.cbranch(is_true, body_bb, exit_bb)

        self.builder.position_at_start(body_bb)
        self.loop_stack.append((cond_bb, exit_bb))

        try:
            self._gen_stmt(node.body)
        finally:
            self.loop_stack.pop()
        self._branch_if_open(cond_bb)

        self.builder.position_at_start(exit_bb)

    def _gen_break(self, node: Break) -> None:
        if not self.loop_stack:
            raise CodegenError("'break' outside of loop")
        self.builder.branch(self.loop_stack[-1][1])

    def _gen_continue(self, node: Continue) -> None:
        if not self.loop_stack:
            raise CodegenError("'continue' outside of loop")
        self.builder.branch(self.loop_stack[-1][0])

    def _gen_block(self, node: Block) -> None:
        for stmt in node.body:
            if self._terminated():
                break
            self._gen_stmt(stmt)

    def _gen_expr(self, node: Expr) -> ir.Value:
        if isinstance(node, AstError):
            return ir.Constant(I64, 0)
        if isinstance(node, IntLiteral):
            return ir.Constant(I64, int(node.value))
        if isinstance(node, BoolLiteral):
            return ir.Constant(I64, 1 if node.value else 0)
        if isinstance(node, Ident):
            if node.name not in self.env:
                raise CodegenError(f"use of undeclared '{node.name}'")
            return self.builder.load(self.env[node.name], name=node.name)
        if isinstance(node, AstUnaryOp):
            return self._gen_unary_op(node)
        if isinstance(node, AstBinOp):
            return self._gen_bin_op(node)
        raise CodegenError(f"unknown expression: '{type(node).__name__}'")

    def _gen_unary_op(self, node: AstUnaryOp) -> ir.Value:
        operand = self._gen_expr(node.operand)
        if node.op == "-":
            return self.builder.neg(operand, name="neg")
        if node.op == "!":
            is_zero = self.builder.icmp_signed("==", operand, ZERO)
            return self.builder.zext(is_zero, I64)
        raise CodegenError(f"unknown unary operator: '{node.op}'")

    def _gen_bin_op(self, node: AstBinOp) -> ir.Value:
        op = node.op
        if op == "&&":
            return self._gen_logical_and(node)
        if op == "||":
            return self._gen_logical_or(node)

        left = self._gen_expr(node.left)
        right = self._gen_expr(node.right)
        if op == "+":
            return self.builder.add(left, right, name="add")
        if op == "-":
            return self.builder.sub(left, right, name="sub")
        if op == "*":
            return self.builder.mul(left, right, name="mul")
        if op == "/":
            return self.builder.sdiv(left, right, name="div")

        pred = {
            "==": "==",
            "!=": "!=",
            ">": ">",
            "<": "<",
            "<=": "<=",
            ">=": ">=",
        }.get(op)

        if pred is not None:
            cmp = self.builder.icmp_signed(pred, left, right)
            return self.builder.zext(cmp, I64)

        raise CodegenError(f"unknown operator: '{node.op}'")

    def _gen_logical_and(self, node: AstBinOp) -> ir.Value:
        rhs_bb = self.builder.append_basic_block("and.rhs")
        done_bb = self.builder.append_basic_block("and.end")

        left = self._gen_expr(node.left)
        left_end = self.builder.block
        is_false = self.builder.icmp_signed("==", left, ZERO)
        self.builder.cbranch(is_false, done_bb, rhs_bb)

        self.builder.position_at_start(rhs_bb)
        right = self._gen_expr(node.right)
        right_bool = self._to_bool_i64(right)
        rhs_end = self.builder.block

        self._branch_if_open(done_bb)

        self.builder.position_at_start(done_bb)
        phi = self.builder.phi(I64, name="and.result")
        phi.add_incoming(ZERO, left_end)
        phi.add_incoming(right_bool, rhs_end)
        return phi

    def _gen_logical_or(self, node: AstBinOp) -> ir.Value:
        rhs_bb = self.builder.append_basic_block("or.rhs")
        done_bb = self.builder.append_basic_block("or.end")

        left = self._gen_expr(node.left)
        left_end = self.builder.block
        is_true = self.builder.icmp_signed("!=", left, ZERO)
        self.builder.cbranch(is_true, done_bb, rhs_bb)

        self.builder.position_at_start(rhs_bb)
        right = self._gen_expr(node.right)
        right_bool = self._to_bool_i64(right)
        rhs_end = self.builder.block
        self._branch_if_open(done_bb)

        self.builder.position_at_start(done_bb)
        phi = self.builder.phi(I64, name="or.result")
        phi.add_incoming(ONE, left_end)
        phi.add_incoming(right_bool, rhs_end)
        return phi