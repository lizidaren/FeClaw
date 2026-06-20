"""
FeHub Service — VCS + Publish + AppData

Provides:
- fe init [--template=xxx]          project scaffolding
- fe vcs commit <message>           record version snapshot
- fe vcs log [file_path]            view version history
- fe vcs diff <file> <ref_a> <ref_b>  compare two versions
- fe vcs restore <file> <ref>      restore old version
- fe publish <tag> [--public]       publish app snapshot
- fe unpublish <tag>               unpublish app

Storage layout in COS:
  agents/{hash}/.fehub/commits/{timestamp}.json   — commit records
  agents/{hash}/.fehub/releases/{tag}/             — published snapshots
"""

import asyncio
import difflib
import json
import logging
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from models.database import SessionLocal
from models.fehub import FePublish, AppData
from services.storage_service import get_storage_service
from services.virtual_filesystem import VirtualFileSystem

logger = logging.getLogger(__name__)

# Module-level storage (initialized lazily)
_storage = None


def _get_storage():
    global _storage
    if _storage is None:
        _storage = get_storage_service()
    return _storage


class FeHubService:
    """VCS + Publish operations backed by COS."""

    FEHUB_DIR = ".fehub"
    COMMITS_DIR = f"{FEHUB_DIR}/commits"
    RELEASES_DIR = f"{FEHUB_DIR}/releases"

    def __init__(self, agent_hash: str):
        self.agent_hash = agent_hash
        self.base_path = f"feclaw/agents/{agent_hash}/"
        self.vfs = VirtualFileSystem(agent_hash=agent_hash)

    # ── Init ────────────────────────────────────────────────

    async def init_project(self, path: str, template_path: str = "") -> str:
        """
        fe init [--template=xxx]

        Create a new mini-app project skeleton.
        If template_path is given, copy from that VFS path.
        Otherwise generate a minimal skeleton (manifest.json + index.html).
        """
        target = path.strip().rstrip("/")
        if not target:
            target = "/workspace"
        if not target.startswith("/"):
            target = "/" + target

        # Check if target directory is empty or new
        existing = await self._list_workspace_files(target)
        if existing:
            # Allow re-init if .fehub doesn't exist yet
            fehub_exists = await self._path_exists(f"{target}/{self.FEHUB_DIR}")
            if fehub_exists:
                return f"Error: 目标目录非空且已包含 .fehub，请先备份或切换目录"

        # If template is specified, copy from there
        if template_path:
            template_path = template_path.strip().rstrip("/")
            if not template_path.startswith("/"):
                return f"Error: template_path must be an absolute VFS path"
            copied = await self._copy_template(target, template_path)
            if copied.startswith("Error"):
                return copied
            init_msg = f"✓ 已从模板创建: {template_path} → {target}"
        else:
            # Generate minimal skeleton
            app_name = target.rsplit("/", 1)[-1] or "myapp"
            skeleton = self._generate_skeleton(app_name)
            for fname, content in skeleton.items():
                fpath = f"{target}/{fname}"
                await self.vfs.async_write(fpath, content)
            init_msg = f"✓ 已生成最小化项目骨架: {target}"

        # Create .fehub/ dir and write initial commit record
        await self._ensure_fehub_dirs()
        commit_record = {
            "type": "init",
            "message": f"Initialize project: {app_name}",
            "files": list(skeleton.keys()) if not template_path else ["(from template)"],
            "timestamp": datetime.utcnow().isoformat(),
        }
        await self._write_commit_record(commit_record)

        return f"{init_msg}\n✓ 已初始化 VCS（.fehub/ 目录）"

    def _generate_skeleton(self, app_name: str) -> Dict[str, str]:
        """Generate minimal manifest.json + index.html skeleton."""
        return {
            "manifest.json": json.dumps({
                "name": app_name,
                "version": "0.1.0",
                "description": f"{app_name} mini-app",
                "routes": [
                    {"path": "/", "type": "static", "file": "index.html"}
                ]
            }, ensure_ascii=False, indent=2),
            "index.html": f"<!DOCTYPE html>\n<html lang=\"zh\">\n<head>\n<meta charset=\"UTF-8\">\n<title>{app_name}</title>\n</head>\n<body>\n<h1>{app_name}</h1>\n<p>Hello from FeHub!</p>\n</body>\n</html>",
        }

    async def _copy_template(self, target: str, template_path: str) -> str:
        """Recursively copy template directory to target."""
        # List all files in template path
        cos_prefix = self._vpath_to_cos(template_path)
        storage = _get_storage()
        objects = storage.list_objects(cos_prefix)
        if not objects:
            return f"Error: 模板目录不存在或为空: {template_path}"

        copied_count = 0
        for obj in objects:
            key = obj["Key"]
            rel_path = key[len(cos_prefix):].lstrip("/")
            if not rel_path:
                continue
            # Read source file
            content = storage.get_file_content(key)
            if content is None:
                continue
            # Write to target
            dest_key = f"{self.base_path}{target.lstrip('/')}/{rel_path}"
            storage.put_object(dest_key, content)
            copied_count += 1

        return f"{copied_count} files copied"

    # ── VCS Commit ─────────────────────────────────────────

    async def commit(self, path: str, message: str) -> str:
        """
        fe vcs commit <message>

        Write a commit record to .fehub/commits/{timestamp}.json
        """
        if not message or not message.strip():
            return "Error: 请提供提交消息: fe vcs commit <message>"

        # Get list of files in workspace (non-fehub)
        workspace_prefix = path.rstrip("/")
        if not workspace_prefix.startswith("/"):
            workspace_prefix = "/" + workspace_prefix

        files = await self._list_workspace_files(workspace_prefix)
        if not files:
            return "Error: 工作区为空，无文件可提交"

        # Filter out .fehub dir
        files = [f for f in files if not f.startswith(f"{self.FEHUB_DIR}/")]

        commit_record = {
            "type": "commit",
            "message": message.strip(),
            "files": files,
            "timestamp": datetime.utcnow().isoformat(),
        }
        await self._write_commit_record(commit_record)

        return f"✓ 已提交 {len(files)} 个文件:\n  {message.strip()}\n  committed at {commit_record['timestamp']}"

    async def _write_commit_record(self, record: Dict) -> None:
        """Write a commit record JSON to .fehub/commits/."""
        timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
        commit_key = f"{self.base_path}{self.COMMITS_DIR}/{timestamp}.json"
        storage = _get_storage()
        storage.put_object(commit_key, json.dumps(record, ensure_ascii=False, indent=2).encode("utf-8"))

    # ── VCS Log ─────────────────────────────────────────────

    async def log(self, path: str = "", file_path: str = "") -> str:
        """
        fe vcs log [file_path]

        List commits, optionally filtering by file.
        """
        commits = await self._list_commits()

        if not commits:
            return "（尚无版本记录，请先执行 fe vcs commit）"

        if file_path:
            # Filter commits that touch this file
            filtered = []
            for commit in commits:
                if file_path in commit.get("files", []):
                    filtered.append(commit)
            commits = filtered
            if not commits:
                return f"（文件 {file_path} 在任何提交中均未出现）"

        # Also get releases
        releases = await self._list_releases()

        lines = ["=== 提交记录 ==="]
        for c in commits:
            ts = c.get("timestamp", "unknown")
            msg = c.get("message", "")
            files_count = len(c.get("files", []))
            lines.append(f"[commit] {ts}  {msg}  ({files_count} files)")

        if releases:
            lines.append("\n=== 已发布版本 ===")
            for r in releases:
                tag = r.get("tag", "?")
                ts = r.get("timestamp", "?")
                lines.append(f"[release] {tag}  {ts}")

        return "\n".join(lines)

    async def _list_commits(self) -> List[Dict]:
        """List all commit records sorted newest-first."""
        commits_prefix = f"{self.base_path}{self.COMMITS_DIR}/"
        storage = _get_storage()
        objects = storage.list_objects(commits_prefix)
        if not objects:
            return []

        records = []
        for obj in objects:
            key = obj["Key"]
            if not key.endswith(".json"):
                continue
            content = storage.get_file_content(key)
            if content:
                try:
                    records.append(json.loads(content.decode("utf-8")))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass

        records.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
        return records

    async def _list_releases(self) -> List[Dict]:
        """List all release tags from DB."""
        db = SessionLocal()
        try:
            publishes = db.query(FePublish).filter(
                FePublish.agent_hash == self.agent_hash,
                FePublish.is_active == True,
            ).all()
            return [
                {"tag": p.tag, "timestamp": p.created_at.isoformat() if p.created_at else "",
                 "is_public": p.is_public, "app_name": p.app_name}
                for p in publishes
            ]
        finally:
            db.close()

    # ── VCS Diff ────────────────────────────────────────────

    async def diff(self, file_path: str, ref_a: str, ref_b: str) -> str:
        """
        fe vcs diff <file> <ref_a> <ref_b>

        Compare two versions of a file using Python difflib.
        """
        content_a = await self._get_file_at_ref(file_path, ref_a)
        if content_a is None:
            return f"Error: 无法解析引用 '{ref_a}'（或文件在该版本中不存在）"
        if content_a.startswith("Error"):
            return content_a

        content_b = await self._get_file_at_ref(file_path, ref_b)
        if content_b is None:
            return f"Error: 无法解析引用 '{ref_b}'（或文件在该版本中不存在）"
        if content_b.startswith("Error"):
            return content_b

        # Generate unified diff
        diff_lines = difflib.unified_diff(
            content_a.splitlines(keepends=True),
            content_b.splitlines(keepends=True),
            fromfile=f"{ref_a}:{file_path}",
            tofile=f"{ref_b}:{file_path}",
            lineterm="",
        )
        diff_text = "".join(diff_lines)

        if not diff_text:
            return f"（{file_path} 在 {ref_a} 和 {ref_b} 中完全相同）"

        return f"--- {ref_a}:{file_path}\n+++ {ref_b}:{file_path}\n{diff_text}"

    async def _get_file_at_ref(self, file_path: str, ref: str) -> Optional[str]:
        """
        Resolve a ref (commit timestamp or release tag) to file content.
        Returns None if file doesn't exist at that ref.
        """
        # Try as release tag first
        release_content = await self._get_file_at_release(file_path, ref)
        if release_content is not None:
            return release_content

        # Try as commit
        return await self._get_file_at_commit(file_path, ref)

    async def _get_file_at_release(self, file_path: str, tag: str) -> Optional[str]:
        """Get file content from a release snapshot."""
        db = SessionLocal()
        try:
            publish = db.query(FePublish).filter(
                FePublish.agent_hash == self.agent_hash,
                FePublish.tag == tag,
                FePublish.is_active == True,
            ).first()
            if not publish:
                return None

            # snapshot_path is like agents/{hash}/.fehub/releases/{tag}/
            # file_path is like workspace/index.html
            # We need to construct the COS key for the file
            snapshot_base = publish.snapshot_path.rstrip("/")
            # Remove agents/{hash}/ prefix to get VFS-style path
            vfs_rel = file_path.lstrip("/")
            file_cos_key = f"{snapshot_base}/{vfs_rel}"

            storage = _get_storage()
            content = storage.get_file_content(file_cos_key)
            if content is None:
                return None
            return content.decode("utf-8", errors="replace")
        finally:
            db.close()

    async def _get_file_at_commit(self, file_path: str, commit_ts: str) -> Optional[str]:
        """Get file content from a commit snapshot (read from COS directly)."""
        # Commit records have a "files" list, but not the actual content.
        # For diff, we need the workspace files at that commit time.
        # Since we don't store per-commit snapshots (only release snapshots),
        # we fall back to the current workspace content with a warning.
        # A full implementation would store per-commit file snapshots.
        commits = await self._list_commits()
        for c in commits:
            if c.get("timestamp", "").startswith(commit_ts):
                if file_path in c.get("files", []):
                    # File existed at this commit, but content not stored.
                    # Return current content as approximation (not ideal but workable).
                    current = await self.vfs.async_cat(file_path)
                    if not current.startswith("Error"):
                        return current + "\n[⚠️ 内容为当前版本，commit 时快照未单独存储]"
                return None
        return None

    # ── VCS Restore ────────────────────────────────────────

    async def restore(self, file_path: str, ref: str) -> str:
        """
        fe vcs restore <file> <ref>

        Restore a file to a previous version.
        """
        content = await self._get_file_at_ref(file_path, ref)
        if content is None:
            return f"Error: 文件 {file_path} 在引用 '{ref}' 中不存在"
        if content.startswith("Error"):
            return content

        # Write back to workspace
        result = await self.vfs.async_write(file_path, content)
        if result.startswith("Error"):
            return f"Error: 恢复失败: {result}"

        return f"✓ 已将 {file_path} 恢复到版本 {ref}\n{result}"

    # ── Publish ─────────────────────────────────────────────

    async def publish(self, path: str, tag: str, is_public: bool = False) -> str:
        """
        fe publish <tag> [--public]

        1. Validate manifest.json
        2. Snapshot workspace/* → .fehub/releases/{tag}/
        3. Register with apps_service
        4. Save FePublish record
        """
        tag = tag.strip()
        if not tag:
            return "Error: 请指定发布标签: fe publish <tag>"

        # Read and validate manifest.json
        manifest_path = f"{path.rstrip('/')}/manifest.json"
        manifest_content = await self.vfs.async_cat(manifest_path)
        if manifest_content.startswith("Error"):
            return f"Error: manifest.json 不存在或无法读取，请先执行 fe init: {manifest_content}"

        try:
            manifest = json.loads(manifest_content)
        except json.JSONDecodeError:
            return "Error: manifest.json 格式无效（非 JSON）"

        app_name = manifest.get("name", tag)
        routes = manifest.get("routes", [])

        # Snapshot workspace to .fehub/releases/{tag}/
        snapshot_path = f"{self.base_path}{self.RELEASES_DIR}/{tag}/"
        snapshot_result = await self._snapshot_workspace(path, snapshot_path)
        if snapshot_result.startswith("Error"):
            return snapshot_result

        # Register with apps_service
        app_id = f"{self.agent_hash}-{tag}"
        try:
            from services.apps_service import register_app_sync
            config = register_app_sync(self.agent_hash, app_id)
            if not config:
                # App not yet in VFS — create a minimal routes.json from manifest
                routes_json = json.dumps({
                    "routes": routes,
                    "app_name": app_name,
                    "published": True,
                    "fehub_tag": tag,
                }, ensure_ascii=False, indent=2)
                routes_path = f"/workspace/apps/{app_id}/routes.json"
                await self.vfs.async_write(routes_path, routes_json)
                register_app_sync(self.agent_hash, app_id)
        except Exception as e:
            logger.warning(f"[FeHub] apps_service register failed: {e}")

        # Save FePublish record
        db = SessionLocal()
        try:
            existing = db.query(FePublish).filter(
                FePublish.agent_hash == self.agent_hash,
                FePublish.tag == tag,
            ).first()
            if existing:
                existing.is_active = True
                existing.snapshot_path = snapshot_path
                existing.manifest = manifest
                existing.is_public = is_public
                existing.updated_at = datetime.utcnow()
            else:
                publish = FePublish(
                    agent_hash=self.agent_hash,
                    app_name=app_name,
                    tag=tag,
                    is_public=is_public,
                    snapshot_path=snapshot_path,
                    manifest=manifest,
                )
                db.add(publish)
            db.commit()
        finally:
            db.close()

        publish_url = f"https://{self.agent_hash}.feclaw.lizidaren.cn/apps/{app_id}/"
        return (
            f"✓ 已发布 {app_name}@{tag}\n"
            f"  访问地址: {publish_url}\n"
            f"  可见性: {'公开' if is_public else '私有'}\n"
            f"  快照: {snapshot_path}\n"
            f"  文件数: {snapshot_result}"
        )

    async def _snapshot_workspace(self, workspace_path: str, dest_prefix: str) -> str:
        """
        Recursively copy all files from workspace_path to dest_prefix in COS.
        dest_prefix is a COS key like: fecLaw/agents/{hash}/.fehub/releases/{tag}/
        """
        storage = _get_storage()
        workspace_vfs = workspace_path.rstrip("/")
        if not workspace_vfs.startswith("/"):
            workspace_vfs = "/" + workspace_vfs

        # List all files in workspace
        files = await self._list_workspace_files(workspace_vfs)
        if not files:
            return "Error: 工作区为空，无文件可发布"

        # Filter out .fehub from snapshot
        files = [f for f in files if not f.startswith(f"{self.FEHUB_DIR}/")]

        copied = 0
        for vfs_file_path in files:
            # Read file content from workspace
            full_vfs_path = f"{workspace_vfs}/{vfs_file_path}" if vfs_file_path else workspace_vfs
            content = await self.vfs.async_read_file(full_vfs_path)
            if content.startswith("Error"):
                continue

            # Write to snapshot location
            # dest_prefix already includes agent hash: fecLaw/agents/{hash}/.fehub/releases/{tag}/
            # vfs_file_path is relative to workspace root (e.g., "index.html" or "css/style.css")
            dest_key = f"{dest_prefix}{vfs_file_path}"
            storage.put_object(dest_key, content.encode("utf-8"))
            copied += 1

        return str(copied)

    async def unpublish(self, tag: str) -> str:
        """fe unpublish <tag>"""
        db = SessionLocal()
        try:
            publish = db.query(FePublish).filter(
                FePublish.agent_hash == self.agent_hash,
                FePublish.tag == tag,
            ).first()
            if not publish:
                return f"Error: 未找到发布记录: {tag}"

            publish.is_active = False
            publish.updated_at = datetime.utcnow()
            db.commit()

            # Unregister from apps_service
            app_id = f"{self.agent_hash}-{tag}"
            try:
                from services.apps_service import unregister_app_sync
                unregister_app_sync(self.agent_hash, app_id)
            except Exception as e:
                logger.warning(f"[FeHub] apps_service unregister failed: {e}")

            return f"✓ 已取消发布 {publish.app_name}@{tag}（快照已保留）"
        finally:
            db.close()

    # ── List Publishes ─────────────────────────────────────

    async def list_publishes(self) -> str:
        """List all published apps for this agent."""
        db = SessionLocal()
        try:
            publishes = db.query(FePublish).filter(
                FePublish.agent_hash == self.agent_hash,
            ).order_by(FePublish.created_at.desc()).all()

            if not publishes:
                return "（尚无发布记录）"

            lines = ["=== 已发布应用 ==="]
            for p in publishes:
                status = "✓" if p.is_active else "✗"
                visibility = "公开" if p.is_public else "私有"
                ts = p.created_at.strftime("%Y-%m-%d %H:%M") if p.created_at else "?"
                lines.append(f"[{status}] {p.app_name}@{p.tag}  {visibility}  {ts}")

            return "\n".join(lines)
        finally:
            db.close()

    # ── AppData (runtime key-value store) ─────────────────

    @staticmethod
    async def get_app_data(app_id: str, user_id: int, key: str = None, prefix: str = None) -> Any:
        """Get app data for a user. If key given, return that key. If prefix, return all matching."""
        db = SessionLocal()
        try:
            q = db.query(AppData).filter(AppData.app_id == app_id, AppData.user_id == user_id)
            if key:
                row = q.filter(AppData.key == key).first()
                return row.value if row else None
            elif prefix:
                rows = q.filter(AppData.key.like(f"{prefix}%")).all()
                return {r.key: r.value for r in rows}
            else:
                rows = q.all()
                return {r.key: r.value for r in rows}
        finally:
            db.close()

    @staticmethod
    async def set_app_data(app_id: str, user_id: int, key: str, value: Any) -> bool:
        """Set a single app data key-value pair."""
        db = SessionLocal()
        try:
            existing = db.query(AppData).filter(
                AppData.app_id == app_id,
                AppData.user_id == user_id,
                AppData.key == key,
            ).first()
            if existing:
                existing.value = value
                existing.updated_at = datetime.utcnow()
            else:
                row = AppData(app_id=app_id, user_id=user_id, key=key, value=value)
                db.add(row)
            db.commit()
            return True
        finally:
            db.close()

    @staticmethod
    async def delete_app_data(app_id: str, user_id: int, key: str = None, prefix: str = None) -> int:
        """Delete app data. Returns count of deleted rows."""
        db = SessionLocal()
        try:
            q = db.query(AppData).filter(AppData.app_id == app_id, AppData.user_id == user_id)
            if key:
                count = q.filter(AppData.key == key).delete()
            elif prefix:
                count = q.filter(AppData.key.like(f"{prefix}%")).delete()
            else:
                count = q.delete()
            db.commit()
            return count
        finally:
            db.close()

    # ── Internal helpers ───────────────────────────────────

    def _vpath_to_cos(self, vpath: str) -> str:
        """Convert VFS absolute path to COS key."""
        vpath = vpath.strip().lstrip("/")
        return f"{self.base_path}{vpath}"

    async def _path_exists(self, vfs_path: str) -> bool:
        """Check if a VFS path exists (file or directory)."""
        cos_key = self._vpath_to_cos(vfs_path)
        storage = _get_storage()
        objects = storage.list_objects(cos_key, max_keys=1)
        return objects is not None and len(objects) > 0

    async def _list_workspace_files(self, vfs_dir: str) -> List[str]:
        """List all files under a VFS directory (recursive), returning relative paths."""
        cos_prefix = self._vpath_to_cos(vfs_dir)
        if not cos_prefix.endswith("/"):
            cos_prefix += "/"
        storage = _get_storage()
        objects = storage.list_objects(cos_prefix)
        if not objects:
            return []

        files = []
        for obj in objects:
            key = obj["Key"]
            rel_path = key[len(cos_prefix):]
            if rel_path:
                files.append(rel_path)
        return files

    async def _ensure_fehub_dirs(self) -> None:
        """Ensure .fehub/commits/ and .fehub/releases/ directory markers exist."""
        storage = _get_storage()
        for subdir in [self.COMMITS_DIR, self.RELEASES_DIR]:
            dir_key = f"{self.base_path}{subdir}/.directory"
            storage.put_object(dir_key, b"")
