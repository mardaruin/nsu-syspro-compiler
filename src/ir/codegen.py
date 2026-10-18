from llvmlite import ir

from spl_parser.ast_nodes import (
    Expr, Stmt, IntLiteral, Ident, BinOp as AstBinOp, UnaryOp as AstUnaryOp,
    Assign, Declare, Return as AstReturn, ExprStmt, Program, Error as AstError,
)

from .errors import CodegenError

I64 = ir.IntType(64)
ALIGN = 8


class LLVMCodeGen:
    def __init__(self):
        self.module: ir.Module | None = None
        self.builder: ir.IRBuilder | None = None
        self.main_fn: ir.Function | None = None
        self.env: dict[str, ir.AllocaInstr] = {}

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

        for stmt in program.body:
            if self.builder.block is not None and self.builder.block.is_terminated:
                break
            self._gen_stmt(stmt)

        if self.builder.block is not None and not self.builder.block.is_terminated:
            self.builder.ret(ir.Constant(I64, 0))

        return self.module

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

    def _gen_expr(self, node: Expr) -> ir.Value:
        if isinstance(node, AstError):
            return ir.Constant(I64, 0)
        if isinstance(node, IntLiteral):
            return ir.Constant(I64, int(node.value))
        if isinstance(node, Ident):
            if node.name not in self.env:
                raise CodegenError(f"use of undeclared '{node.name}'")
            return self.builder.load(self.env[node.name], name=node.name)
        if isinstance(node, AstUnaryOp):
            operand = self._gen_expr(node.operand)
            if node.op == "-":
                return self.builder.neg(operand, name="neg")
            raise CodegenError(f"unknown unary operator: '{node.op}'")
        if isinstance(node, AstBinOp):
            left = self._gen_expr(node.left)
            right = self._gen_expr(node.right)
            if node.op == "+":
                return self.builder.add(left, right, name="add")
            if node.op == "-":
                return self.builder.sub(left, right, name="sub")
            if node.op == "*":
                return self.builder.mul(left, right, name="mul")
            if node.op == "/":
                return self.builder.sdiv(left, right, name="div")
            raise CodegenError(f"unknown operator: '{node.op}'")
        raise CodegenError(f"unknown expression: '{type(node).__name__}'")
