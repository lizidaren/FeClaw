"""
Tokenizer - lexical analysis for bash-like commands.
"""
from enum import Enum
from dataclasses import dataclass
from typing import List, Optional


class TokenType(Enum):
    WORD = "WORD"
    PIPE = "|"
    AND = "&&"
    OR = "||"
    SEMICOLON = ";"
    REDIR_OUT = ">"
    REDIR_APPEND = ">>"
    REDIR_IN = "<"
    REDIR_ERR = "2>"
    REDIR_ERR_AND_OUT = "2>&1"
    REDIR_OUT_NULL = ">/dev/null"
    EOF = "EOF"


@dataclass
class Token:
    type: TokenType
    value: str
    position: int = 0


class Tokenizer:
    """
    Tokenizer for bash-like commands.

    Handles:
    - Words (command names, arguments)
    - Pipe: |
    - And: &&
    - Or: ||
    - Semicolon: ;
    - Redirections: >, >>, <, 2>, 2>&1, >/dev/null, 2>/dev/null
    """

    def __init__(self, command: str):
        self.command = command
        self.pos = 0
        self._length = len(command)

    def _skip_whitespace(self):
        """Skip whitespace characters."""
        while self.pos < self._length and self.command[self.pos] in " \t":
            self.pos += 1

    def _read_word(self) -> str:
        """Read a word (non-whitespace, non-operator sequence)."""
        start = self.pos
        while self.pos < self._length:
            c = self.command[self.pos]
            if c in " \t|;&><\"":
                break
            self.pos += 1
        return self.command[start:self.pos]

    def _read_quoted(self, quote_char: str) -> str:
        """Read a quoted string."""
        self.pos += 1  # skip opening quote
        start = self.pos
        while self.pos < self._length and self.command[self.pos] != quote_char:
            if self.command[self.pos] == '\\' and self.pos + 1 < self._length:
                self.pos += 2  # skip escaped char
            else:
                self.pos += 1
        if self.pos < self._length:
            self.pos += 1  # skip closing quote
        return self.command[start:self.pos - 1]

    def _peek(self, offset: int = 0) -> Optional[str]:
        """Peek at character at current position + offset."""
        idx = self.pos + offset
        if idx < self._length:
            return self.command[idx]
        return None

    def _match(self, expected: str) -> bool:
        """Check if expected string matches at current position."""
        return self.command[self.pos:self.pos + len(expected)] == expected

    def next_token(self) -> Optional[Token]:
        """Get the next token from the command string."""
        self._skip_whitespace()

        if self.pos >= self._length:
            return None

        # Check for 2>&1 first (must check before 2>)
        if self._match("2>&1"):
            pos = self.pos
            self.pos += 4
            return Token(TokenType.REDIR_ERR_AND_OUT, "2>&1", pos)

        # Check for 2> (must check before >)
        if self._match("2>"):
            pos = self.pos
            self.pos += 2
            return Token(TokenType.REDIR_ERR, "2>", pos)

        # Check for >>
        if self._match(">>"):
            pos = self.pos
            self.pos += 2
            return Token(TokenType.REDIR_APPEND, ">>", pos)

        # Check for >/dev/null (must check before >)
        if self._match(">/dev/null"):
            pos = self.pos
            self.pos += 10
            return Token(TokenType.REDIR_OUT_NULL, ">/dev/null", pos)

        # Check for 2>/dev/null
        if self._match("2>/dev/null"):
            pos = self.pos
            self.pos += 11
            return Token(TokenType.REDIR_ERR, "2>/dev/null", pos)

        # Check for >
        if self._match(">"):
            pos = self.pos
            self.pos += 1
            return Token(TokenType.REDIR_OUT, ">", pos)

        # Check for <
        if self._match("<"):
            pos = self.pos
            self.pos += 1
            return Token(TokenType.REDIR_IN, "<", pos)

        # Check for &&
        if self._match("&&"):
            pos = self.pos
            self.pos += 2
            return Token(TokenType.AND, "&&", pos)

        # Check for ||
        if self._match("||"):
            pos = self.pos
            self.pos += 2
            return Token(TokenType.OR, "||", pos)

        # Check for |
        if self._match("|"):
            pos = self.pos
            self.pos += 1
            return Token(TokenType.PIPE, "|", pos)

        # Check for ;
        if self._match(";"):
            pos = self.pos
            self.pos += 1
            return Token(TokenType.SEMICOLON, ";", pos)

        # Read quoted string
        c = self._peek()
        if c in ('"', "'"):
            pos = self.pos
            word = self._read_quoted(c)
            return Token(TokenType.WORD, word, pos)

        # Read word
        pos = self.pos
        word = self._read_word()
        if word:
            return Token(TokenType.WORD, word, pos)

        return None

    def tokenize(self) -> List[Token]:
        """Tokenize the entire command string."""
        tokens = []
        while True:
            token = self.next_token()
            if token is None:
                break
            tokens.append(token)
        tokens.append(Token(TokenType.EOF, "", self.pos))
        return tokens


def tokenize(command: str) -> List[Token]:
    """Convenience function to tokenize a command."""
    return Tokenizer(command).tokenize()
