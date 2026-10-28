"""Contract-gate tests for T01 / W0.

Every test builds its own miniature repository under ``tmp_path`` and runs
``scripts/check_contracts.py`` as a subprocess.  The checked-in repository is
never modified, and the checker never imports the application, so these tests
need no database, model or network.

The tests cover the failure modes required by T01: a missing default tool, a
duplicate tool name, mixed-case VFS paths, an alias impersonating the standard
reminder name, and an invalid ``_resolve_group_path`` export/duplication.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CHECKER = REPO_ROOT / "scripts" / "check_contracts.py"


def _write(root: Path, rel: str, content: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")
    return path


def _make_sample(tmp_path: Path) -> Path:
    """Create a minimal repository that satisfies every checked contract."""
    root = tmp_path / "sample"
    root.mkdir()

    _write(
        root,
        "services/tool_registry.py",
        """
        TOOL_REGISTRY = {}
        TOOL_ALIASES = {"schedule_reminder": "create_cron_job"}


        def tool(name=None, description="", category="general"):
            def decorator(func):
                registry_name = name or func.__name__
                TOOL_REGISTRY[registry_name] = {"name": registry_name, "func": func}
                return func

            return decorator


        def normalize_tool_name(name):
            return TOOL_ALIASES.get(name, name)


        def get_tool(name):
            return TOOL_REGISTRY.get(normalize_tool_name(name))


        def get_tool_schemas():
            return list(TOOL_REGISTRY.values())
        """,
    )
    _write(root, "services/tools/__init__.py", "")
    _write(
        root,
        "services/tools/cron_tools.py",
        """
        from services.tool_registry import tool


        class CronToolsMixin:
            @tool(description="create a scheduled task", category="agent")
            def create_cron_job(self, prompt: str, schedule: str) -> str:
                return ""

            @tool(description="list pending reminders", category="agent")
            def list_reminders(self) -> str:
                return ""

            @tool(description="cancel a reminder", category="agent")
            def cancel_reminder(self, task_id: str) -> str:
                return ""
        """,
    )
    _write(
        root,
        "services/tools/file_ops.py",
        """
        from services.tool_registry import tool


        class FileOpsMixin:
            @staticmethod
            def _resolve_group_path(path):
                return path, None

            @tool(description="read a file", category="file")
            def file_read(self, path: str) -> str:
                return ""
        """,
    )
    _write(
        root,
        "services/tools/permission_tools.py",
        """
        from services.tools.file_ops import FileOpsMixin


        def ask(path):
            return FileOpsMixin._resolve_group_path(path)
        """,
    )
    _write(
        root,
        "services/agent_init_service.py",
        """
        class AgentInitService:
            AVAILABLE_TOOLS = [
                "file_read",
                "create_cron_job",
                "list_reminders",
                "cancel_reminder",
            ]

            def initialize_agent(self):
                default_tools = {
                    "enabled": ["file_read", "create_cron_job"],
                    "disabled": [],
                }
                return default_tools
        """,
    )
    _write(
        root,
        "services/agent_executor.py",
        """
        from services.tool_registry import normalize_tool_name

        SUBAGENT_BLOCKED = ["create_cron_job", "schedule_reminder"]


        class AgentExecutor:
            def _get_enabled_tools(self):
                return {normalize_tool_name(name) for name in self._raw_enabled}

            def execute_tool(self, tool_name, arguments=None):
                tool_name = normalize_tool_name(tool_name)
                if tool_name in self.blocked_tools:
                    return "Error: tool is disabled"
                return tool_name
        """,
    )
    _write(
        root,
        "services/user_profile_service.py",
        """
        USER_PROFILE_FILE_PATH = "/workspace/agent/user.md"


        def build_prompt():
            return "将整理结果写入 workspace/agent/user.md。"
        """,
    )
    _write(
        root,
        "prompts/hatch.md",
        """
        初始化完成后，把用户画像写入 workspace/agent/user.md。
        """,
    )
    return root


def _run(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CHECKER), "--root", str(root), *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


def _run_json(root: Path) -> tuple[subprocess.CompletedProcess, dict]:
    completed = _run(root, "--json")
    assert completed.stdout.strip(), completed.stderr
    payload = json.loads(completed.stdout)
    return completed, payload


def _rules(payload: dict) -> set[str]:
    return {finding["rule"] for finding in payload["findings"]}


def _findings_for(payload: dict, rule: str) -> list[dict]:
    return [finding for finding in payload["findings"] if finding["rule"] == rule]


def test_valid_sample_passes(tmp_path):
    root = _make_sample(tmp_path)

    completed, payload = _run_json(root)

    assert completed.returncode == 0, payload
    assert payload["ok"] is True
    assert payload["errors"] == 0
    assert payload["findings"] == []


def test_python_syntax_error_is_reported(tmp_path):
    root = _make_sample(tmp_path)
    _write(
        root,
        "services/broken_module.py",
        """
        def broken(:
            pass
        """,
    )

    completed, payload = _run_json(root)

    assert completed.returncode == 1
    findings = _findings_for(payload, "python-syntax")
    assert findings, payload
    assert any(finding["path"] == "services/broken_module.py" for finding in findings)


def test_missing_default_tool_is_reported(tmp_path):
    root = _make_sample(tmp_path)
    init = root / "services/agent_init_service.py"
    init.write_text(
        init.read_text(encoding="utf-8").replace(
            '"enabled": ["file_read", "create_cron_job"],',
            '"enabled": ["file_read", "create_cron_job", "missing_tool"],',
        ),
        encoding="utf-8",
    )

    completed, payload = _run_json(root)

    assert completed.returncode == 1
    findings = _findings_for(payload, "tool-default-missing")
    assert findings, payload
    assert any("missing_tool" in finding["message"] for finding in findings)
    assert all(finding["path"] == "services/agent_init_service.py" for finding in findings)
    assert all(finding["line"] > 0 for finding in findings)


def test_duplicate_tool_registration_is_reported(tmp_path):
    root = _make_sample(tmp_path)
    _write(
        root,
        "services/tools/extra.py",
        """
        from services.tool_registry import tool


        @tool(description="a second file_read", category="file")
        def file_read(self, path: str) -> str:
            return ""
        """,
    )

    completed, payload = _run_json(root)

    assert completed.returncode == 1
    findings = _findings_for(payload, "tool-duplicate")
    assert findings, payload
    assert any("file_read" in finding["message"] for finding in findings)
    assert any("services/tools/extra.py" in finding["message"] for finding in findings)


def test_stacked_tool_decorators_are_reported(tmp_path):
    root = _make_sample(tmp_path)
    _write(
        root,
        "services/tools/extra.py",
        """
        from services.tool_registry import tool


        class ExtraMixin:
            @tool(description="first decorator", category="file")
            @tool(description="second decorator", category="file")
            def doubled_tool(self, path: str) -> str:
                return ""
        """,
    )

    completed, payload = _run_json(root)

    assert completed.returncode == 1
    findings = _findings_for(payload, "tool-duplicate")
    assert findings, payload
    assert any("doubled_tool" in finding["message"] for finding in findings)


def test_vfs_path_case_is_reported(tmp_path):
    root = _make_sample(tmp_path)
    profile = root / "services/user_profile_service.py"
    profile.write_text(
        profile.read_text(encoding="utf-8").replace(
            "/workspace/agent/user.md", "/workspace/agent/USER.md"
        ),
        encoding="utf-8",
    )

    completed, payload = _run_json(root)

    assert completed.returncode == 1
    findings = _findings_for(payload, "vfs-path-case")
    assert findings, payload
    assert any("USER.md" in finding["message"] for finding in findings)
    assert any(finding["path"] == "services/user_profile_service.py" for finding in findings)


def test_reminder_alias_cannot_be_registered_as_a_tool(tmp_path):
    root = _make_sample(tmp_path)
    _write(
        root,
        "services/tools/extra.py",
        """
        from services.tool_registry import tool


        @tool(name="schedule_reminder", description="alias imposter", category="agent")
        def reminder_imposter(self, prompt: str, schedule: str) -> str:
            return ""
        """,
    )

    completed, payload = _run_json(root)

    assert completed.returncode == 1
    findings = _findings_for(payload, "reminder-alias-registration")
    assert findings, payload
    assert any(finding["path"] == "services/tools/extra.py" for finding in findings)


def test_default_config_must_use_canonical_reminder_name(tmp_path):
    root = _make_sample(tmp_path)
    init = root / "services/agent_init_service.py"
    init.write_text(
        init.read_text(encoding="utf-8").replace(
            '"enabled": ["file_read", "create_cron_job"],',
            '"enabled": ["file_read", "schedule_reminder"],',
        ),
        encoding="utf-8",
    )

    completed, payload = _run_json(root)

    assert completed.returncode == 1
    alias_findings = _findings_for(payload, "tool-default-alias")
    usage_findings = _findings_for(payload, "reminder-alias-usage")
    assert alias_findings, payload
    assert usage_findings, payload
    assert any("create_cron_job" in finding["message"] for finding in alias_findings)


def test_alias_without_registry_mapping_is_reported(tmp_path):
    root = _make_sample(tmp_path)
    registry = root / "services/tool_registry.py"
    registry.write_text(
        registry.read_text(encoding="utf-8").replace(
            'TOOL_ALIASES = {"schedule_reminder": "create_cron_job"}', "TOOL_ALIASES = {}"
        ),
        encoding="utf-8",
    )
    init = root / "services/agent_init_service.py"
    init.write_text(
        init.read_text(encoding="utf-8").replace(
            '"enabled": ["file_read", "create_cron_job"],',
            '"enabled": ["file_read", "schedule_reminder"],',
        ),
        encoding="utf-8",
    )

    completed, payload = _run_json(root)

    assert completed.returncode == 1
    rules = _rules(payload)
    assert "reminder-alias-missing" in rules, payload
    assert "tool-default-alias" in rules, payload


def test_resolver_import_without_module_level_export_is_reported(tmp_path):
    root = _make_sample(tmp_path)
    _write(
        root,
        "services/tools/permission_tools.py",
        """
        from services.tools.file_ops import _resolve_group_path


        def ask(path):
            return _resolve_group_path(path)
        """,
    )

    completed, payload = _run_json(root)

    assert completed.returncode == 1
    findings = _findings_for(payload, "resolver-export-missing")
    assert findings, payload
    assert any(finding["path"] == "services/tools/permission_tools.py" for finding in findings)
    assert any("FileOpsMixin._resolve_group_path" in finding["message"] for finding in findings)


def test_duplicate_resolver_implementation_is_reported(tmp_path):
    root = _make_sample(tmp_path)
    _write(
        root,
        "services/tools/permission_tools.py",
        """
        from services.tools.file_ops import FileOpsMixin


        def _resolve_group_path(path):
            return path, None


        def ask(path):
            return FileOpsMixin._resolve_group_path(path)
        """,
    )

    completed, payload = _run_json(root)

    assert completed.returncode == 1
    findings = _findings_for(payload, "resolver-duplicate")
    assert findings, payload
    assert any(finding["path"] == "services/tools/permission_tools.py" for finding in findings)


def test_json_and_text_reports_expose_file_and_line(tmp_path):
    root = _make_sample(tmp_path)
    init = root / "services/agent_init_service.py"
    init.write_text(
        init.read_text(encoding="utf-8").replace(
            '"enabled": ["file_read", "create_cron_job"],',
            '"enabled": ["file_read", "missing_tool"],',
        ),
        encoding="utf-8",
    )

    completed, payload = _run_json(root)
    assert completed.returncode == 1
    assert payload["ok"] is False
    assert payload["errors"] >= 1
    for finding in payload["findings"]:
        assert set(finding) == {"path", "line", "severity", "rule", "message", "hint"}
        assert isinstance(finding["line"], int) and finding["line"] > 0
        assert finding["severity"] in {"error", "warning"}

    text_run = _run(root)
    assert text_run.returncode == 1
    assert "services/agent_init_service.py:" in text_run.stdout
    assert "error:" in text_run.stdout
    assert "[tool-default-missing]" in text_run.stdout
    assert "result: FAIL" in text_run.stdout
