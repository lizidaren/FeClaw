#!/usr/bin/env python3
"""Static configuration contracts for FeClaw (T01, wave W0).

This gate is deliberately offline and side-effect free:

* standard library only (``ast``, ``re``, ``json``, ...); the application is
  never imported, so no model client, database, COS or scheduler starts;
* no network, database, storage or application files are written;
* every problem is printed as ``repo/relative/path:line: severity: message [rule]``;
* ``--json`` emits the same findings in a machine-readable form.

Checked contracts
-----------------
1. ``python-syntax`` -- every Python file under the repository parses.
2. ``tool-duplicate`` -- each tool name is registered exactly once.
3. ``tool-default-*`` -- every tool ID in the default enabled/disabled config
   of ``services/agent_init_service.py`` exists in the registry built from
   ``@tool`` decorators; defaults must use canonical names.
4. ``reminder-alias-*`` -- the standard reminder tool is ``create_cron_job``.
   ``schedule_reminder`` may exist only as one compatibility alias in
   ``services/tool_registry.py``, resolved by the registry and by the
   enabled/disabled filtering, so disabling the canonical name cannot be
   bypassed by calling the alias.
5. ``resolver-*`` -- ``_resolve_group_path`` has a single implementation; a
   module-level import is only valid when the target module really exports a
   module-level name (a class-level ``FileOpsMixin`` method is not an export).
6. ``vfs-path-case`` -- ``workspace/agent/{user,soul,identity}.md`` is always
   lowercase in runtime code, prompts, templates and assets.  Linux/COS are
   case-sensitive, so ``USER.md`` is a second file and silently loses data.

Exit status: ``0`` no errors (warnings allowed), ``1`` at least one error,
``2`` invalid invocation.  Warnings never fail the gate.
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import os
import re
import sys
import tokenize
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

DEFAULT_ROOT = Path(__file__).resolve().parents[1]

REGISTRY_REL = "services/tool_registry.py"
AGENT_INIT_REL = "services/agent_init_service.py"
AGENT_EXECUTOR_REL = "services/agent_executor.py"
FILE_OPS_REL = "services/tools/file_ops.py"

REMINDER_CANONICAL = "create_cron_job"
REMINDER_ALIAS = "schedule_reminder"
RESOLVER_NAME = "_resolve_group_path"

VFS_PATH_RE = re.compile(r"(?i)(?:/)?workspace/agent/(?:user|soul|identity)\.md")
VFS_BARE_RE = re.compile(r"(?i)\b(?:user|soul|identity)\.md\b")
REMINDER_ALIAS_RE = re.compile(r"\b" + re.escape(REMINDER_ALIAS) + r"\b")

# Names that a normalization implementation may use.  They are accepted so a
# single canonical helper can own the mapping instead of duplicating strings.
NORMALIZER_NAMES = frozenset(
    {
        "normalize_tool_name",
        "_normalize_tool_name",
        "canonical_tool_name",
        "_canonical_tool_name",
        "resolve_tool_alias",
        "_resolve_tool_alias",
        "resolve_tool_name",
        "_resolve_tool_name",
    }
)

# Directories that are generated, vendored, archived or runtime storage.
EXCLUDED_DIR_NAMES = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        "_archive",
        "claude-audits",
        "test-results",
        "feclaw-storage",
        "workspace",
        "site-packages",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
    }
)

# Directories whose content is runtime code, prompt text or shipped assets.
RUNTIME_ROOTS = (
    "services",
    "routers",
    "models",
    "utils",
    "templates",
    "static",
    "public",
    "app",
    "prompts",
    "agent_templates",
)
RUNTIME_FILES = ("config.py", "main.py")
RUNTIME_TEXT_SUFFIXES = frozenset(
    {
        ".py",
        ".md",
        ".txt",
        ".json",
        ".yaml",
        ".yml",
        ".js",
        ".mjs",
        ".html",
        ".htm",
        ".j2",
        ".jinja",
        ".prompt",
        ".toml",
        ".ini",
        ".sh",
        ".ps1",
        ".example",
    }
)

MAX_TEXT_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class Finding:
    """One contract violation (or advisory warning)."""

    rule: str
    severity: str
    path: str
    line: int
    message: str
    hint: str = ""


@dataclass(frozen=True)
class ToolRegistration:
    """One ``@tool`` registration site."""

    name: str
    path: str
    function_line: int
    decorator_line: int
    explicit_name: bool


@dataclass(frozen=True)
class AliasOccurrence:
    """A literal occurrence of the reminder alias outside the alias mapping."""

    path: str
    line: int
    context: str


@dataclass(frozen=True)
class ResolverDefinition:
    """A ``_resolve_group_path`` definition with its class nesting."""

    path: str
    line: int
    class_stack: tuple[str, ...]
    node: ast.AST


class ContractChecker:
    """Run all static checks against one repository root."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.findings: list[Finding] = []
        self._seen: set[tuple[str, str, int, str]] = set()
        self._trees: dict[str, tuple[Optional[ast.AST], Optional[str]]] = {}
        self._parents: dict[str, dict[int, ast.AST]] = {}
        self._python_files: list[str] = []
        self._registrations: list[ToolRegistration] = []
        self._registry_names: set[str] = set()
        self._alias_decls: list[tuple[str, int, str]] = []
        self._alias_keys: dict[str, str] = {}

    # ------------------------------------------------------------------
    # findings and files
    # ------------------------------------------------------------------
    def add(
        self,
        rule: str,
        severity: str,
        path: str,
        line: int,
        message: str,
        hint: str = "",
    ) -> None:
        key = (rule, path, max(1, int(line)), message)
        if key in self._seen:
            return
        self._seen.add(key)
        self.findings.append(
            Finding(
                rule=rule,
                severity=severity,
                path=path,
                line=max(1, int(line)),
                message=message,
                hint=hint,
            )
        )

    def iter_files(
        self,
        suffixes: Optional[Iterable[str]] = None,
        roots: Optional[Iterable[str]] = None,
    ):
        suffix_set = {s.lower() for s in suffixes} if suffixes is not None else None
        root_set = tuple(roots) if roots is not None else None
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDED_DIR_NAMES)
            for fname in sorted(filenames):
                path = Path(dirpath) / fname
                try:
                    rel = path.relative_to(self.root).as_posix()
                except ValueError:  # pragma: no cover - os.walk stays inside root
                    continue
                if suffix_set is not None and path.suffix.lower() not in suffix_set:
                    continue
                if root_set is not None and not self._under_any(rel, root_set):
                    continue
                yield rel, path

    @staticmethod
    def _under_any(rel: str, roots: tuple[str, ...]) -> bool:
        return any(rel == root or rel.startswith(root + "/") for root in roots)

    @staticmethod
    def _is_runtime_python(rel: str) -> bool:
        if rel == "config.py" or rel == "main.py":
            return True
        return any(rel == r or rel.startswith(r + "/") for r in RUNTIME_ROOTS)

    def _read(self, rel: str) -> Optional[str]:
        try:
            data = (self.root / rel).read_bytes()
        except OSError:
            self.add("file-read", "warning", rel, 1, f"cannot read {rel}")
            return None
        if len(data) > MAX_TEXT_BYTES:
            return None
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return data.decode("utf-8", errors="replace")

    def _tree(self, rel: str) -> Optional[ast.AST]:
        if rel in self._trees:
            return self._trees[rel][0]
        text = self._read(rel)
        tree: Optional[ast.AST] = None
        if text is not None:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                try:
                    tree = ast.parse(text, filename=rel)
                except SyntaxError as exc:
                    self.add(
                        "python-syntax",
                        "error",
                        rel,
                        exc.lineno or 1,
                        f"Python syntax error: {exc.msg}",
                        hint="fix the syntax error; the remaining checks for this file are skipped",
                    )
        self._trees[rel] = (tree, text)
        return tree

    def _load_python(self) -> None:
        for rel, _path in self.iter_files({".py"}):
            self._python_files.append(rel)
            self._tree(rel)

    def _parents_for(self, rel: str) -> dict[int, ast.AST]:
        cached = self._parents.get(rel)
        if cached is not None:
            return cached
        tree = self._tree(rel)
        parents: dict[int, ast.AST] = {}
        if tree is not None:
            for parent in ast.walk(tree):
                for child in ast.iter_child_nodes(parent):
                    parents[id(child)] = parent
        self._parents[rel] = parents
        return parents

    # ------------------------------------------------------------------
    # small AST helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _const_str(node: Optional[ast.AST]) -> Optional[str]:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        return None

    @staticmethod
    def _find_function(tree: Optional[ast.AST], name: str):
        if tree is None:
            return None
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                return node
        return None

    @staticmethod
    def _target_names(node: ast.AST) -> list[str]:
        return [target.id for target in node.targets if isinstance(target, ast.Name)]

    @staticmethod
    def _literal_list(node: Optional[ast.AST]) -> list[tuple[str, int]]:
        if not isinstance(node, (ast.List, ast.Tuple)):
            return []
        items: list[tuple[str, int]] = []
        for element in node.elts:
            value = ContractChecker._const_str(element)
            if value is not None:
                items.append((value, element.lineno))
        return items

    # ------------------------------------------------------------------
    # tool registry: unique names from @tool decorators
    # ------------------------------------------------------------------
    @staticmethod
    def _tool_decorator(decorator: ast.AST):
        """Return ``(name, explicit, dynamic)`` for an ``@tool`` decorator."""
        if not isinstance(decorator, ast.Call):
            return None
        func = decorator.func
        if isinstance(func, ast.Name):
            is_tool = func.id == "tool"
        elif isinstance(func, ast.Attribute):
            is_tool = func.attr == "tool"
        else:
            return None
        if not is_tool:
            return None
        for keyword in decorator.keywords:
            if keyword.arg == "name":
                value = ContractChecker._const_str(keyword.value)
                if value is None:
                    return "", True, True
                return value, True, False
        return None, False, False  # caller fills in the function name

    def _check_tool_registry(self) -> None:
        for rel in self._python_files:
            if not rel.startswith("services/"):
                continue
            tree = self._tree(rel)
            if tree is None:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for decorator in node.decorator_list:
                    parsed = self._tool_decorator(decorator)
                    if parsed is None:
                        continue
                    name, explicit, dynamic = parsed
                    if dynamic:
                        self.add(
                            "tool-name-dynamic",
                            "warning",
                            rel,
                            decorator.lineno,
                            "cannot statically resolve the @tool(name=...) value; "
                            "registry contracts are not checked for this function",
                            hint="use a string literal tool name",
                        )
                        continue
                    if not name:
                        name = node.name
                    self._registrations.append(
                        ToolRegistration(
                            name=name,
                            path=rel,
                            function_line=node.lineno,
                            decorator_line=decorator.lineno,
                            explicit_name=explicit,
                        )
                    )

        groups: dict[str, list[ToolRegistration]] = {}
        for registration in self._registrations:
            groups.setdefault(registration.name, []).append(registration)
        self._registry_names = set(groups)

        for name, registrations in sorted(groups.items()):
            if len(registrations) == 1:
                continue
            sites = ", ".join(
                f"{item.path}:{item.decorator_line}" for item in registrations
            )
            same_function = (
                len({(item.path, item.function_line) for item in registrations}) == 1
            )
            if same_function:
                first = registrations[0]
                self.add(
                    "tool-duplicate",
                    "error",
                    first.path,
                    first.decorator_line,
                    f"tool {name!r} is registered {len(registrations)} times by "
                    "stacked @tool decorators; the later registration silently "
                    "overwrites the earlier schema/description",
                    hint="keep exactly one @tool decorator per tool function",
                )
                continue
            for extra in registrations[1:]:
                self.add(
                    "tool-duplicate",
                    "error",
                    extra.path,
                    extra.decorator_line,
                    f"tool name {name!r} is registered more than once ({sites}); "
                    "the duplicate silently overwrites the earlier registry entry",
                    hint="keep a single registration point per tool name",
                )

    # ------------------------------------------------------------------
    # reminder alias: create_cron_job is canonical, schedule_reminder is one alias
    # ------------------------------------------------------------------
    def _collect_alias_declarations(self) -> None:
        for rel in self._python_files:
            if not self._is_runtime_python(rel):
                continue
            tree = self._tree(rel)
            if tree is None:
                continue
            parents = self._parents_for(rel)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Dict):
                    continue
                for key, value in zip(node.keys, node.values):
                    if (
                        self._const_str(key) == REMINDER_ALIAS
                        and self._const_str(value) == REMINDER_CANONICAL
                    ):
                        assigned = self._assigned_name(node, parents)
                        self._alias_decls.append((rel, key.lineno, assigned))
                        self._alias_keys[REMINDER_ALIAS] = REMINDER_CANONICAL

    def _assigned_name(self, node: ast.AST, parents: dict[int, ast.AST]) -> str:
        current: Optional[ast.AST] = node
        while current is not None:
            parent = parents.get(id(current))
            if isinstance(parent, (ast.Assign, ast.AnnAssign)):
                targets = parent.targets if isinstance(parent, ast.Assign) else [parent.target]
                names = [t.id for t in targets if isinstance(t, ast.Name)]
                return names[0] if names else "?"
            current = parent
        return "?"

    def _alias_var_names(self) -> set[str]:
        names = {
            "TOOL_ALIASES",
            "TOOL_ALIAS_MAP",
            "TOOL_NAME_ALIASES",
            "TOOL_COMPAT_ALIASES",
        }
        for _rel, _line, assigned in self._alias_decls:
            if assigned and assigned != "?":
                names.add(assigned)
        return names

    def _node_references_alias(self, node: ast.AST) -> bool:
        names = self._alias_var_names() | set(NORMALIZER_NAMES)
        for child in ast.walk(node):
            if isinstance(child, ast.Name) and child.id in names:
                return True
            if isinstance(child, ast.Attribute) and child.attr in names:
                return True
        return False

    def _alias_occurrences(self) -> list[AliasOccurrence]:
        occurrences: list[AliasOccurrence] = []
        for rel in self._python_files:
            if not self._is_runtime_python(rel):
                continue
            tree = self._tree(rel)
            if tree is None:
                continue
            parents = self._parents_for(rel)
            # Exempt the alias mapping itself and explicit compatibility block
            # lists that mention both the alias and the canonical name.
            exempt_lines: set[int] = set()
            for node in ast.walk(tree):
                if self._const_str(node) != REMINDER_ALIAS:
                    continue
                if self._is_alias_decl_key(node, parents) or self._in_paired_block_list(
                    node, parents
                ):
                    exempt_lines.add(node.lineno)

            ranges = self._context_ranges(tree)
            reported_lines: set[int] = set()
            for token in self._python_tokens(rel, tree):
                if token.type == tokenize.COMMENT:
                    continue
                if REMINDER_ALIAS not in token.string:
                    continue
                for offset, piece in enumerate(token.string.split("\n")):
                    if not REMINDER_ALIAS_RE.search(piece):
                        continue
                    line = token.start[0] + offset
                    if line in exempt_lines or line in reported_lines:
                        continue
                    reported_lines.add(line)
                    occurrences.append(
                        AliasOccurrence(
                            path=rel,
                            line=line,
                            context=self._context_for_line(ranges, line),
                        )
                    )

        for rel in self._runtime_text_files():
            if rel.endswith(".py"):
                continue
            text = self._read(rel)
            if text is None:
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if not REMINDER_ALIAS_RE.search(line):
                    continue
                # A compatibility list that mentions both names keeps the
                # disable semantics explicit; treat it as intentional.
                if REMINDER_CANONICAL in line:
                    continue
                occurrences.append(
                    AliasOccurrence(path=rel, line=number, context="prompt/UI text")
                )
        return occurrences

    def _is_alias_decl_key(self, node: ast.AST, parents: dict[int, ast.AST]) -> bool:
        parent = parents.get(id(node))
        if not isinstance(parent, ast.Dict):
            return False
        for key, value in zip(parent.keys, parent.values):
            if key is node and self._const_str(value) == REMINDER_CANONICAL:
                return True
        return False

    def _in_paired_block_list(self, node: ast.AST, parents: dict[int, ast.AST]) -> bool:
        current: Optional[ast.AST] = parents.get(id(node))
        while current is not None:
            if isinstance(
                current,
                (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module),
            ):
                return False
            if isinstance(current, (ast.List, ast.Tuple, ast.Set)):
                if any(
                    self._const_str(element) == REMINDER_CANONICAL
                    for element in current.elts
                ):
                    return True
            current = parents.get(id(current))
        return False

    @staticmethod
    def _docstring_spans(tree: ast.AST) -> list[tuple[int, int]]:
        spans: list[tuple[int, int]] = []
        for node in ast.walk(tree):
            if not isinstance(
                node,
                (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
            ):
                continue
            body = node.body
            if not body or not isinstance(body[0], ast.Expr):
                continue
            value = body[0].value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                spans.append((value.lineno, getattr(value, "end_lineno", value.lineno)))
        return spans

    def _python_tokens(self, rel: str, tree: ast.AST):
        """Yield tokens except comments and docstrings.

        ``tokenize`` gives exact physical lines for embedded JS/HTML and
        multi-line prompts, which is what makes ``file:line`` reporting useful.
        """
        text = self._trees.get(rel, (None, None))[1]
        if text is None:
            text = self._read(rel)
        if text is None:
            return
        try:
            tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
        except (tokenize.TokenError, IndentationError):
            return
        docstrings = self._docstring_spans(tree)
        for token in tokens:
            if token.type == tokenize.COMMENT:
                continue
            start, end = token.start[0], token.end[0]
            if any(doc_start <= start and end <= doc_end for doc_start, doc_end in docstrings):
                continue
            yield token

    @staticmethod
    def _context_ranges(tree: ast.AST) -> list[tuple[int, int, str]]:
        ranges: list[tuple[int, int, str]] = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                end = getattr(node, "end_lineno", node.lineno)
                ranges.append((node.lineno, end, node.name))
        return ranges

    @staticmethod
    def _context_for_line(ranges: list[tuple[int, int, str]], line: int) -> str:
        best: Optional[tuple[int, int, str]] = None
        for start, end, name in ranges:
            if start <= line <= end:
                if best is None or (end - start) < (best[1] - best[0]):
                    best = (start, end, name)
        return best[2] if best is not None else "<module>"

    def _runtime_text_files(self):
        for rel, _path in self.iter_files(RUNTIME_TEXT_SUFFIXES, RUNTIME_ROOTS):
            yield rel
        for name in RUNTIME_FILES:
            if (self.root / name).is_file():
                yield name

    def _check_reminder_alias(self) -> None:
        occurrences = self._alias_occurrences()

        for registration in self._registrations:
            if registration.name == REMINDER_ALIAS:
                self.add(
                    "reminder-alias-registration",
                    "error",
                    registration.path,
                    registration.decorator_line,
                    f"{REMINDER_ALIAS!r} must not be a second @tool registration; "
                    f"the standard tool is {REMINDER_CANONICAL!r}",
                    hint="declare the alias only in the registry mapping; never as a "
                    "parallel tool implementation",
                )

        if REMINDER_CANONICAL not in self._registry_names:
            self.add(
                "reminder-canonical-missing",
                "error",
                REGISTRY_REL,
                1,
                f"the canonical reminder tool {REMINDER_CANONICAL!r} is not registered "
                "by any @tool decorator",
                hint="register create_cron_job in services/tools/cron_tools.py",
            )

        if self._alias_decls:
            if len(self._alias_decls) > 1:
                sites = ", ".join(f"{r}:{l}" for r, l, _v in self._alias_decls)
                for rel, line, _name in self._alias_decls[1:]:
                    self.add(
                        "reminder-alias-duplicate",
                        "error",
                        rel,
                        line,
                        "the reminder alias mapping is declared more than once "
                        f"({sites}); aliases must have exactly one authority",
                        hint="keep one mapping in services/tool_registry.py and delete "
                        "the other copies",
                    )
            if not any(rel == REGISTRY_REL for rel, _line, _name in self._alias_decls):
                for rel, line, _name in self._alias_decls:
                    self.add(
                        "reminder-alias-location",
                        "error",
                        rel,
                        line,
                        "the reminder alias mapping must live in the single registry "
                        f"authority ({REGISTRY_REL}), not in a caller module",
                        hint="move the mapping to services/tool_registry.py and expose "
                        "one normalization helper",
                    )

            registry_tree = self._tree(REGISTRY_REL)
            get_tool = self._find_function(registry_tree, "get_tool")
            if get_tool is None:
                self.add(
                    "reminder-alias-normalization",
                    "error",
                    REGISTRY_REL,
                    1,
                    "services/tool_registry.py defines no get_tool(); registry alias "
                    "resolution cannot be verified",
                    hint="resolve aliases to canonical names inside get_tool()",
                )
            elif not self._node_references_alias(get_tool):
                self.add(
                    "reminder-alias-normalization",
                    "error",
                    REGISTRY_REL,
                    get_tool.lineno,
                    f"get_tool() does not resolve {REMINDER_ALIAS!r} through the alias "
                    f"mapping; callers looking up the alias receive no {REMINDER_CANONICAL!r} entry",
                    hint="normalize the requested name (alias -> canonical) before the "
                    "registry lookup",
                )
            self._check_config_normalization()
        elif occurrences:
            self.add(
                "reminder-alias-missing",
                "error",
                REGISTRY_REL,
                1,
                f"{REMINDER_ALIAS!r} is used as a tool name but no alias mapping to "
                f"{REMINDER_CANONICAL!r} exists anywhere in the runtime code",
                hint="declare exactly one alias mapping in services/tool_registry.py "
                f"({REMINDER_ALIAS!r} -> {REMINDER_CANONICAL!r}) and resolve it in "
                "get_tool() and in the enabled/disabled filtering",
            )

        for occurrence in occurrences:
            self.add(
                "reminder-alias-usage",
                "error",
                occurrence.path,
                occurrence.line,
                f"non-standard reminder tool name {REMINDER_ALIAS!r} used in "
                f"{occurrence.context}; use the canonical name {REMINDER_CANONICAL!r}",
                hint=f"defaults, UI and prompts must use {REMINDER_CANONICAL!r}; keep "
                f"{REMINDER_ALIAS!r} only in the registry compatibility mapping",
            )

    def _check_config_normalization(self) -> None:
        tree = self._tree(AGENT_EXECUTOR_REL)
        if tree is None:
            return

        candidates = [
            self._find_function(tree, "_get_enabled_tools"),
            self._find_function(tree, "_get_tool_definitions"),
        ]
        candidates = [node for node in candidates if node is not None]
        if candidates and not any(self._node_references_alias(node) for node in candidates):
            node = candidates[0]
            self.add(
                "reminder-alias-disable-bypass",
                "error",
                AGENT_EXECUTOR_REL,
                node.lineno,
                "enabled/disabled tool filtering does not normalize names through the "
                f"{REMINDER_ALIAS!r} alias; a disabled {REMINDER_CANONICAL!r} could still "
                "be invoked through its alias",
                hint="normalize every configured name through the registry helper before "
                "building the enabled set",
            )

        execute_tool = self._find_function(tree, "execute_tool")
        if execute_tool is None:
            return
        alias_lines = [
            child.lineno
            for child in ast.walk(execute_tool)
            if self._node_references_alias(child)
        ]
        blocked_lines = [
            child.lineno
            for child in ast.walk(execute_tool)
            if isinstance(child, ast.Attribute) and child.attr == "blocked_tools"
        ]
        if not alias_lines:
            self.add(
                "reminder-alias-disable-bypass",
                "error",
                AGENT_EXECUTOR_REL,
                execute_tool.lineno,
                "execute_tool() does not normalize the incoming tool name through the "
                f"{REMINDER_ALIAS!r} alias, so the blocked-tools check can be bypassed",
                hint="canonicalize the requested tool name before checking blocked_tools",
            )
        elif blocked_lines and min(alias_lines) > min(blocked_lines):
            self.add(
                "reminder-alias-disable-bypass",
                "error",
                AGENT_EXECUTOR_REL,
                min(alias_lines),
                "alias normalization happens after the blocked_tools check; a blocked "
                "canonical tool could be invoked through its alias",
                hint="normalize the tool name before comparing it with blocked_tools",
            )

    # ------------------------------------------------------------------
    # default tool configuration
    # ------------------------------------------------------------------
    def _check_default_tools(self) -> None:
        tree = self._tree(AGENT_INIT_REL)
        if tree is None:
            return

        default_dicts: list[ast.Dict] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            if "default_tools" not in self._target_names(node):
                continue
            if isinstance(node.value, ast.Dict):
                default_dicts.append(node.value)

        if not default_dicts:
            self.add(
                "tool-default-config-missing",
                "warning",
                AGENT_INIT_REL,
                1,
                "could not locate the default_tools literal; default tool IDs were not "
                "checked",
                hint="keep the default enabled/disabled lists as literal lists, or update "
                "this checker if the configuration moved",
            )

        for default_dict in default_dicts:
            for kind in ("enabled", "disabled"):
                items: list[tuple[str, int]] = []
                for key, value in zip(default_dict.keys, default_dict.values):
                    if self._const_str(key) == kind:
                        items.extend(self._literal_list(value))
                for name, line in items:
                    if name in self._registry_names:
                        continue
                    if name == REMINDER_ALIAS or name in self._alias_keys:
                        canonical = self._alias_keys.get(name, REMINDER_CANONICAL)
                        self.add(
                            "tool-default-alias",
                            "error",
                            AGENT_INIT_REL,
                            line,
                            f"default {kind} tool uses the compatibility alias {name!r}; "
                            f"the standard name is {canonical!r}",
                            hint="defaults must use the canonical tool name; keep the alias "
                            "only for old stored configurations",
                        )
                        continue
                    self.add(
                        "tool-default-missing",
                        "error",
                        AGENT_INIT_REL,
                        line,
                        f"default {kind} tool {name!r} is not registered in "
                        "services/tool_registry.py",
                        hint="register the tool or remove it from the default list",
                    )

        for name, line in self._available_tools(tree):
            if name in self._registry_names:
                continue
            if name == REMINDER_ALIAS or name in self._alias_keys:
                if self._alias_decls:
                    continue  # alias accepted for validating old stored configs
                self.add(
                    "tool-available-unregistered",
                    "warning",
                    AGENT_INIT_REL,
                    line,
                    f"AVAILABLE_TOOLS lists {name!r} but no alias mapping or registry "
                    "entry exists",
                    hint=f"use {REMINDER_CANONICAL!r} or add the compatibility mapping",
                )
                continue
            self.add(
                "tool-available-unregistered",
                "warning",
                AGENT_INIT_REL,
                line,
                f"AVAILABLE_TOOLS lists {name!r}, which is not registered in "
                "services/tool_registry.py",
                hint="remove stale validation-only names or register the tool",
            )

    def _available_tools(self, tree: ast.AST) -> list[tuple[str, int]]:
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef) or node.name != "AgentInitService":
                continue
            for statement in node.body:
                if not isinstance(statement, ast.Assign):
                    continue
                if "AVAILABLE_TOOLS" not in self._target_names(statement):
                    continue
                return self._literal_list(statement.value)
        return []

    # ------------------------------------------------------------------
    # _resolve_group_path: one implementation, honest re-exports
    # ------------------------------------------------------------------
    def _check_resolver(self) -> None:
        definitions: list[ResolverDefinition] = []
        for rel in self._python_files:
            tree = self._tree(rel)
            if tree is None:
                continue
            visitor = _ResolverVisitor()
            visitor.visit(tree)
            for node, stack in visitor.definitions:
                definitions.append(
                    ResolverDefinition(
                        path=rel,
                        line=node.lineno,
                        class_stack=tuple(stack),
                        node=node,
                    )
                )

        if not definitions:
            if (self.root / FILE_OPS_REL).is_file():
                self.add(
                    "resolver-missing",
                    "warning",
                    FILE_OPS_REL,
                    1,
                    f"{RESOLVER_NAME!r} was not found; if it was renamed, update this "
                    "contract check",
                )
        else:
            class_level = [item for item in definitions if item.class_stack]
            primary = class_level[0] if len(class_level) == 1 else definitions[0]
            for item in definitions:
                if item is primary:
                    continue
                if not item.class_stack and self._is_resolver_bridge(item.node):
                    continue
                self.add(
                    "resolver-duplicate",
                    "error",
                    item.path,
                    item.line,
                    f"duplicate {RESOLVER_NAME!r} implementation; only one implementation "
                    f"is allowed (primary: {primary.path}:{primary.line})",
                    hint="do not copy the resolver; if a module-level export is needed, "
                    "delegate to the single implementation",
                )

        for rel in self._python_files:
            tree = self._tree(rel)
            if tree is None:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom):
                    continue
                if not any(alias.name == RESOLVER_NAME for alias in node.names):
                    continue
                target_rel = self._resolve_import_module(node, rel)
                if target_rel is None:
                    self.add(
                        "resolver-import-unresolved",
                        "warning",
                        rel,
                        node.lineno,
                        f"cannot resolve the module for `from {node.module or '.'} import "
                        f"{RESOLVER_NAME}`; export relation was not checked",
                    )
                    continue
                target_tree = self._tree(target_rel)
                exports, dynamic = self._module_level_names(target_tree)
                if RESOLVER_NAME in exports:
                    continue
                if dynamic:
                    self.add(
                        "resolver-import-dynamic",
                        "warning",
                        rel,
                        node.lineno,
                        f"{target_rel} uses a module-level __getattr__, so the "
                        f"{RESOLVER_NAME!r} export cannot be proven",
                    )
                    continue
                implementation = self._class_implementation_hint(target_rel)
                self.add(
                    "resolver-export-missing",
                    "error",
                    rel,
                    node.lineno,
                    f"`from {node.module or '.'} import {RESOLVER_NAME}` imports a name "
                    f"that {target_rel} does not define at module level"
                    + (f"; the only implementation is {implementation}" if implementation else ""),
                    hint=f"call {implementation or RESOLVER_NAME} directly, or add a thin "
                    "module-level re-export that delegates to the single implementation",
                )

    @staticmethod
    def _is_resolver_bridge(node: ast.AST) -> bool:
        for child in ast.walk(node):
            if isinstance(child, ast.Attribute) and child.attr == RESOLVER_NAME:
                return True
            if isinstance(child, ast.Name) and child.id in {"FileOpsMixin", "FileOps"}:
                return True
        return False

    def _module_level_names(self, tree: Optional[ast.AST]) -> tuple[set[str], bool]:
        names: set[str] = set()
        dynamic = False
        if tree is None:
            return names, dynamic
        for statement in tree.body:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(statement.name)
            elif isinstance(statement, ast.Assign):
                for target in statement.targets:
                    if isinstance(target, ast.Name):
                        names.add(target.id)
            elif isinstance(statement, ast.AnnAssign):
                if isinstance(statement.target, ast.Name):
                    names.add(statement.target.id)
            elif isinstance(statement, ast.ImportFrom):
                for alias in statement.names:
                    names.add(alias.asname or alias.name)
            elif isinstance(statement, ast.Import):
                for alias in statement.names:
                    names.add(alias.asname or alias.name.split(".")[0])
        # ast nodes do not carry parents; scan the module body directly.
        for statement in tree.body:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                statement.name == "__getattr__"
            ):
                dynamic = True
        return names, dynamic

    def _class_implementation_hint(self, rel: str) -> str:
        tree = self._tree(rel)
        if tree is None:
            return ""
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for statement in node.body:
                if (
                    isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and statement.name == RESOLVER_NAME
                ):
                    return f"{node.name}.{RESOLVER_NAME}"
        return ""

    def _resolve_import_module(self, node: ast.ImportFrom, current_rel: str) -> Optional[str]:
        level = node.level or 0
        module = node.module
        if level == 0:
            if not module:
                return None
            candidate = module.replace(".", "/") + ".py"
            return candidate if (self.root / candidate).is_file() else None

        base = Path(current_rel).parent
        for _ in range(max(0, level - 1)):
            base = base.parent
        if module:
            candidate_path = base / (module.replace(".", "/") + ".py")
        else:
            candidate_path = base / "__init__.py"
        candidate = candidate_path.as_posix()
        return candidate if (self.root / candidate).is_file() else None

    # ------------------------------------------------------------------
    # VFS core path casing
    # ------------------------------------------------------------------
    def _check_vfs_paths(self) -> None:
        for rel in self._runtime_text_files():
            if rel.endswith(".py"):
                if not self._is_runtime_python(rel):
                    continue
                tree = self._tree(rel)
                if tree is None:
                    continue
                string_types = {tokenize.STRING, getattr(tokenize, "FSTRING_MIDDLE", -1)}
                for token in self._python_tokens(rel, tree):
                    if token.type not in string_types:
                        continue
                    for offset, piece in enumerate(token.string.split("\n")):
                        self._scan_vfs_line(rel, token.start[0] + offset, piece)
                continue

            text = self._read(rel)
            if text is None:
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if self._is_comment_line(rel, line):
                    continue
                self._scan_vfs_line(rel, number, line)

    @staticmethod
    def _is_comment_line(rel: str, line: str) -> bool:
        stripped = line.lstrip()
        if rel.endswith((".html", ".htm")):
            return stripped.startswith("<!--")
        if rel.endswith((".js", ".mjs")):
            return (
                stripped.startswith("//")
                or stripped.startswith("/*")
                or stripped.startswith("* ")
                or stripped == "*"
            )
        if rel.endswith((".css", ".scss")):
            return stripped.startswith("/*") or stripped.startswith("* ")
        return False

    def _scan_vfs_line(self, rel: str, number: int, line: str) -> None:
        path_spans: list[tuple[int, int]] = []
        for match in VFS_PATH_RE.finditer(line):
            token = match.group(0)
            path_spans.append((match.start(), match.end()))
            if token == token.lower():
                continue
            self.add(
                "vfs-path-case",
                "error",
                rel,
                number,
                f"VFS core path {token!r} is not lowercase; canonical path is "
                f"{token.lower()!r}",
                hint="Linux/COS are case-sensitive; mixed case reads and writes a second "
                "file and silently loses data",
            )
        for match in VFS_BARE_RE.finditer(line):
            if any(start <= match.start() < end for start, end in path_spans):
                continue
            token = match.group(0)
            if token == token.lower():
                continue
            self.add(
                "vfs-path-case",
                "error",
                rel,
                number,
                f"VFS core filename {token!r} is not lowercase; canonical name is "
                f"{token.lower()!r}",
                hint="Linux/COS are case-sensitive; use the lowercase filename from "
                "workspace/agent/ consistently in code, prompts and UI",
            )

    # ------------------------------------------------------------------
    # driver
    # ------------------------------------------------------------------
    def run(self) -> list[Finding]:
        self._load_python()
        self._check_tool_registry()
        self._collect_alias_declarations()
        self._check_reminder_alias()
        self._check_default_tools()
        self._check_resolver()
        self._check_vfs_paths()
        return sorted(self.findings, key=lambda f: (f.path, f.line, f.rule, f.message))


class _ResolverVisitor(ast.NodeVisitor):
    """Collect ``_resolve_group_path`` definitions and their class nesting."""

    def __init__(self) -> None:
        self.class_stack: list[str] = []
        self.definitions: list[tuple[ast.AST, list[str]]] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802 - ast API
        self.class_stack.append(node.name)
        self.generic_visit(node)
        self.class_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        if node.name == RESOLVER_NAME:
            self.definitions.append((node, list(self.class_stack)))
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self.visit_FunctionDef(node)  # type: ignore[arg-type]


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Static configuration contract gate for FeClaw (T01/W0).",
    )
    parser.add_argument(
        "--root",
        default=str(DEFAULT_ROOT),
        help="repository root to check (default: the checkout containing this script)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON instead of one line per finding",
    )
    args = parser.parse_args(argv)

    try:
        root = Path(args.root).resolve(strict=True)
    except OSError as exc:
        print(f"error: cannot resolve --root {args.root!r}: {exc}", file=sys.stderr)
        return 2
    if not root.is_dir():
        print(f"error: --root {str(root)!r} is not a directory", file=sys.stderr)
        return 2

    checker = ContractChecker(root)
    findings = checker.run()
    errors = [finding for finding in findings if finding.severity == "error"]
    warnings_count = len(findings) - len(errors)

    if args.json:
        payload = {
            "root": str(root),
            "ok": not errors,
            "errors": len(errors),
            "warnings": warnings_count,
            "findings": [
                {
                    "path": finding.path,
                    "line": finding.line,
                    "severity": finding.severity,
                    "rule": finding.rule,
                    "message": finding.message,
                    "hint": finding.hint,
                }
                for finding in findings
            ],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for finding in findings:
            print(
                f"{finding.path}:{finding.line}: {finding.severity}: "
                f"{finding.message} [{finding.rule}]"
            )
            if finding.hint:
                print(f"    hint: {finding.hint}")
        affected = len({finding.path for finding in findings})
        print()
        print(
            f"contracts: {len(errors)} error(s), {warnings_count} warning(s), "
            f"{affected} file(s) affected"
        )
        print("result: FAIL" if errors else "result: PASS")

    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
