"""
Parser - parses tokenized commands into AST.
"""
from typing import List, Optional
from .tokens import Token, TokenType, Tokenizer
from .ast import ASTNode, SimpleCommand, Pipeline, Sequence, Redirection


class Parser:
    """
    Parser for bash-like commands.

    Parses tokenized input into an AST with proper precedence handling.
    """

    def __init__(self, command: str):
        self.command = command
        self.tokenizer = Tokenizer(command)
        self.tokens: List[Token] = []
        self.pos = 0

    def _advance(self) -> Optional[Token]:
        """Advance to the next token."""
        if self.pos < len(self.tokens):
            token = self.tokens[self.pos]
            self.pos += 1
            return token
        return None

    def _peek(self, offset: int = 0) -> Optional[Token]:
        """Peek at a token at current position + offset."""
        idx = self.pos + offset
        if idx < len(self.tokens):
            return self.tokens[idx]
        return None

    def _parse_sequence(self) -> ASTNode:
        """
        Parse a sequence: cmd1; cmd2 & cmd3

        Sequence has lowest precedence.
        """
        items = []

        # Parse first and-or item
        node = self._parse_and_or()
        if node is None:
            return Sequence(items=[])

        items.append((node, ""))

        while True:
            # Look for ; or &
            token = self._peek()
            if token is None or token.type == TokenType.EOF:
                break

            if token.type == TokenType.SEMICOLON:
                self._advance()  # consume ;
                node = self._parse_and_or()
                if node:
                    items.append((node, ";"))
            else:
                break

        return Sequence(items=items)

    def _parse_and_or(self) -> Optional[ASTNode]:
        """
        Parse AND-OR list: cmd1 && cmd2 || cmd3

        && and || have same precedence, evaluated left-to-right.
        """
        node = self._parse_pipeline()
        if node is None:
            return None

        items = [(node, "")]

        while True:
            token = self._peek()
            if token is None:
                break

            if token.type == TokenType.AND:
                self._advance()  # consume &&
                next_node = self._parse_pipeline()
                if next_node:
                    items.append((next_node, "&&"))
            elif token.type == TokenType.OR:
                self._advance()  # consume ||
                next_node = self._parse_pipeline()
                if next_node:
                    items.append((next_node, "||"))
            else:
                break

        if len(items) == 1:
            return node

        return AndOrList(items=items)

    def _parse_pipeline(self) -> Optional[ASTNode]:
        """
        Parse pipeline: cmd1 | cmd2 | cmd3
        """
        command = self._parse_simple_command()
        if command is None:
            return None

        commands = [command]

        while True:
            token = self._peek()
            if token is None or token.type != TokenType.PIPE:
                break

            self._advance()  # consume |

            next_command = self._parse_simple_command()
            if next_command is None:
                break
            commands.append(next_command)

        if len(commands) == 1:
            return command

        return Pipeline(commands=commands)

    def _parse_simple_command(self) -> Optional[SimpleCommand]:
        """
        Parse a simple command with arguments and redirections.
        """
        token = self._peek()
        if token is None or token.type == TokenType.EOF:
            return None

        # Skip semicolons and such that start new commands
        if token.type in (TokenType.SEMICOLON, TokenType.AND, TokenType.OR):
            return None

        command_name = ""
        args = []

        # Parse command name
        if token.type == TokenType.WORD:
            command_name = token.value
            self._advance()
            args = [command_name]
        else:
            return None

        # Parse arguments and redirections
        stdin_redir = None
        stdout_redir = None
        stderr_redir = None

        while True:
            token = self._peek()
            if token is None or token.type == TokenType.EOF:
                break

            if token.type == TokenType.PIPE:
                break
            elif token.type == TokenType.SEMICOLON:
                break
            elif token.type == TokenType.AND:
                break
            elif token.type == TokenType.OR:
                break
            elif token.type == TokenType.REDIR_IN:
                self._advance()  # consume <
                target_token = self._peek()
                if target_token and target_token.type == TokenType.WORD:
                    stdin_redir = Redirection(type="stdin", target=target_token.value)
                    self._advance()
                continue
            elif token.type == TokenType.REDIR_OUT:
                self._advance()  # consume >
                mode = "w"
                target_token = self._peek()
                if target_token and target_token.type == TokenType.WORD:
                    stdout_redir = Redirection(type="stdout", target=target_token.value, mode=mode)
                    self._advance()
                continue
            elif token.type == TokenType.REDIR_APPEND:
                self._advance()  # consume >>
                target_token = self._peek()
                if target_token and target_token.type == TokenType.WORD:
                    stdout_redir = Redirection(type="stdout", target=target_token.value, mode="a")
                    self._advance()
                continue
            elif token.type == TokenType.REDIR_ERR:
                self._advance()  # consume 2>
                target_token = self._peek()
                if target_token and target_token.type == TokenType.WORD:
                    stderr_redir = Redirection(type="stderr", target=target_token.value)
                    self._advance()
                continue
            elif token.type == TokenType.REDIR_ERR_AND_OUT:
                self._advance()  # consume 2>&1
                stderr_redir = Redirection(type="stdout_stderr", target="")
                continue
            elif token.type == TokenType.REDIR_OUT_NULL:
                self._advance()  # consume >/dev/null
                stdout_redir = Redirection(type="stdout", target="/dev/null", mode="w")
                continue
            elif token.type == TokenType.WORD:
                args.append(token.value)
                self._advance()
            else:
                self._advance()  # Skip unknown tokens

        return SimpleCommand(
            command=command_name,
            args=args,
            stdin_redir=stdin_redir,
            stdout_redir=stdout_redir,
            stderr_redir=stderr_redir
        )

    def parse(self) -> ASTNode:
        """Parse the command string into an AST."""
        # If tokens were already provided (list), use them directly
        if isinstance(self.command, list):
            self.tokens = self.command
        else:
            self.tokens = self.tokenize()
        self.pos = 0

        if not self.tokens or (len(self.tokens) == 1 and self.tokens[0].type == TokenType.EOF):
            return Sequence(items=[])

        return self._parse_sequence()

    def tokenize(self) -> List[Token]:
        """Tokenize the command string."""
        return self.tokenizer.tokenize()


# Add the missing AndOrList class
from dataclasses import dataclass, field


@dataclass
class AndOrList(ASTNode):
    """AND-OR list: cmd1 && cmd2 || cmd3"""
    items: List = field(default_factory=list)


def parse(command: str) -> ASTNode:
    """Convenience function to parse a command string into AST."""
    return Parser(command).parse()
