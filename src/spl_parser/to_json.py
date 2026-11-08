from __future__ import annotations

from .ast_nodes import (
    Assign, BinOp, Declare, Error, ExprStmt, Ident,
    IntLiteral, BoolLiteral, Program, Return, UnaryOp,
    If, While, Break, Continue, Block,
)


def node_to_dict(node):
    if node is None:
        return None

    if isinstance(node, Program):
        return {"line": node.line, "column": node.column,
                "kind": "Program",
                "body": [node_to_dict(s) for s in node.body]}

    if isinstance(node, Declare):
        return {"line": node.line, "column": node.column,
                "kind": "Declare", "mut": node.mut, "name": node.name,
                "init": node_to_dict(node.init)}

    if isinstance(node, Return):
        return {"line": node.line, "column": node.column,
                "kind": "Return", "value": node_to_dict(node.value)}

    if isinstance(node, ExprStmt):
        return {"line": node.line, "column": node.column,
                "kind": "ExprStmt", "value": node_to_dict(node.value)}

    if isinstance(node, Assign):
        return {"line": node.line, "column": node.column,
                "kind": "Assign",
                "target": node_to_dict(node.target),
                "value": node_to_dict(node.value)}

    if isinstance(node, If):
        return {"line": node.line, "column": node.column,
                "kind": "If",
                "cond": node_to_dict(node.cond),
                "then": node_to_dict(node.then_branch),
                "else": node_to_dict(node.else_branch)}

    if isinstance(node, While):
        return {"line": node.line, "column": node.column,
                "kind": "While",
                "cond": node_to_dict(node.cond),
                "body": node_to_dict(node.body)}

    if isinstance(node, Break):
        return {"line": node.line, "column": node.column, "kind": "Break"}

    if isinstance(node, Continue):
        return {"line": node.line, "column": node.column, "kind": "Continue"}

    if isinstance(node, Block):
        return {"line": node.line, "column": node.column,
                "kind": "Block",
                "body": [node_to_dict(s) for s in node.body]}

    if isinstance(node, IntLiteral):
        return {"line": node.line, "column": node.column,
                "kind": "IntLiteral", "value": node.value}

    if isinstance(node, BoolLiteral):
        return {"line": node.line, "column": node.column,
                "kind": "BoolLiteral", "value": node.value}

    if isinstance(node, Ident):
        return {"line": node.line, "column": node.column,
                "kind": "Ident", "name": node.name}

    if isinstance(node, UnaryOp):
        return {"line": node.line, "column": node.column,
                "kind": "UnaryOp", "op": node.op,
                "operand": node_to_dict(node.operand)}

    if isinstance(node, BinOp):
        return {"line": node.line, "column": node.column,
                "kind": "BinOp", "op": node.op,
                "left": node_to_dict(node.left),
                "right": node_to_dict(node.right)}

    if isinstance(node, Error):
        return {"line": node.line, "column": node.column,
                "kind": "Error", "message": node.message}

    raise TypeError(f"unknown AST node: {type(node).__name__}")