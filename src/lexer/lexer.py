from __future__ import annotations

from .errors import LexerError
from .tokens import KEYWORDS, Token, TokenType

_SINGLE_CHAR: dict[str, TokenType] = {
    "+": TokenType.PLUS,
    "-": TokenType.MINUS,
    "*": TokenType.MULT,
    "/": TokenType.DIV,
    "=": TokenType.ASSIGN,
    "(": TokenType.LPAREN,
    ")": TokenType.RPAREN,
    ";": TokenType.SEMI,
}

def _is_digit(ch):
    return "0" <= ch <= "9"

def _is_non_digit(ch):
    return ("a" <= ch <= "z") or ("A" <= ch <= "Z") or ch == "_"

class Lexer:
    def __init__(self, source: str):
        self.src = source
        self.i = 0
        self.line = 1
        self.column = 1

    def _peek(self, k: int = 0) -> str:
        j = self.i + k
        return self.src[j] if j < len(self.src) else ""

    def _at_end(self) -> bool:
        return self.i >= len(self.src)

    def _advance(self):
        ch = self.src[self.i]
        self.i += 1
        if ch == "\n":
            self.line += 1
            self.column = 1
        else:
            self.column += 1
        return ch

    def _make(self, tokentype: TokenType, value: str,
              line: int, column: int) -> Token:
        return Token(tokentype, value, line, column)

    def _skip_comments(self, tokens: list[Token]):
        while not self._at_end():
            ch = self._peek()
            if ch in "\t\n\r ":
                self._advance()
                continue
            if ch == "/" and self._peek(1) == "/":
                self._advance()
                self._advance()
                while not self._at_end() and self._peek() != "\n":
                    self._advance()
                continue
            if ch == "/" and self._peek(1) == "*":
                closed = False
                start_line, start_column  = self.line, self.column
                self._advance()
                self._advance()
                while not self._at_end():
                    if self._peek() == "*" and self._peek(1) == "/":
                        self._advance()
                        self._advance()
                        closed = True
                        break
                    self._advance()
                if not closed:
                    tokens.append(self._make(TokenType.ERROR, "Unterminated multi-line comment",
                                             start_line, start_column))
                    return
            else:
                return



    def _read_identifier(self) -> Token:
        line, column = self.line, self.column
        chars: list[str] = [self._advance()]
        while not self._at_end():
            ch = self._peek()
            if _is_non_digit(ch) or _is_digit(ch):
                chars.append(self._advance())
            else:
                break
        text = "".join(chars)
        tokentype = KEYWORDS.get(text, TokenType.IDENT)
        return self._make(tokentype, text, line, column)

    def _read_number(self) -> Token:
        line, column = self.line, self.column
        chars: list[str] = []
        if self._peek() == "0":
            chars.append(self._advance())
            if not self._at_end() and _is_digit(self._peek()):
                while not self._at_end() and _is_digit(self._peek()):
                    chars.append(self._advance())
                return self._make(TokenType.ERROR, "".join(chars), line, column)
        else:
            chars.append(self._advance())
            while not self._at_end() and _is_digit(self._peek()):
                chars.append(self._advance())

        text = "".join(chars)
        return self._make(TokenType.INT, text, line, column)

    def _next_token(self) -> Token:
        line, column = self.line, self.column
        ch = self._peek()

        if _is_non_digit(ch):
            return self._read_identifier()
        if _is_digit(ch):
            return self._read_number()
        if ch in _SINGLE_CHAR:
            self._advance()
            return self._make(_SINGLE_CHAR[ch], ch, line, column)
        self._advance()
        return self._make(TokenType.ERROR, f"Unexpected character {ch}", line, column)

    def tokenize(self) -> list[Token]:
        tokens: list[Token] = []
        while True:
            self._skip_comments(tokens)
            if self._at_end():
                tokens.append(self._make(TokenType.EOF, "", self.line, self.column))
                return tokens
            tokens.append(self._next_token())
        return tokens