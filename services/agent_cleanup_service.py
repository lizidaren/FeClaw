"""
Agent 清理服务
处理 Agent 删除时的资源清理
"""

import logging
from typing import Dict, Any
from sqlalchemy.orm import Session

from config import settings
from models.database import (
    AgentProfile,
    AgentConfig as AgentConfigModel,
    AgentUsageLog,
    ChatHistory,
    ConversationSession,
    FilePermission,
    ScheduledTask,
    SessionLocal,
    ShareMapping,
    WeChatBinding,
    WeChatMessage,
)
from services.storage_service import get_storage_service

logger = logging.getLogger(__name__)


class AgentCleanupService:
    """Agent 清理服务"""

    def __init__(self):
        self._storage = None

    @property
    def storage(self):
        """懒加载 StorageService"""
        if self._storage is None:
            try:
                self._storage = get_storage_service()
            except Exception as e:
                logger.warning(f"StorageService initialization skipped: {e}")
                self._storage = None
        return self._storage

    def cleanup_agent(self, db: Session, agent: AgentProfile) -> Dict[str, Any]:
        """
        清理 Agent 的所有资源

        Args:
            db: 数据库会话
            agent: AgentProfile 实例

        Returns:
            清理结果摘要
        """
        agent_hash = agent.hash
        user_id = agent.user_id

        results = {
            "agent_hash": agent_hash,
            "database_records": {},
            "vfs_files": {},
            "local_files": {},
            "errors": []
        }

        # 1. 清理数据库记录
        try:
            db_results = self._cleanup_database_records(db, agent_hash)
            results["database_records"] = db_results
        except Exception as e:
            error_msg = f"Database cleanup failed: {str(e)}"
            logger.error(error_msg)
            results["errors"].append(error_msg)

        # 2. 清理 VFS 存储数据（COS）
        try:
            vfs_results = self._cleanup_vfs_storage(agent_hash)
            results["vfs_files"] = vfs_results
        except Exception as e:
            error_msg = f"VFS cleanup failed: {str(e)}"
            logger.error(error_msg)
            results["errors"].append(error_msg)

        # 3. 清理本地配置文件
        try:
            local_results = self._cleanup_local_files(agent_hash)
            results["local_files"] = local_results
        except Exception as e:
            error_msg = f"Local files cleanup failed: {str(e)}"
            logger.error(error_msg)
            results["errors"].append(error_msg)

        logger.info(f"Agent {agent_hash} cleanup completed: {results}")
        return results

    def _cleanup_database_records(
        self,
        db: Session,
        agent_hash: str,
    ) -> Dict[str, int]:
        """
        清理数据库中的 Agent 相关记录

        Args:
            db: 数据库会话
            agent_hash: Agent hash

        Returns:
            各表删除的记录数量
        """
        results = {}

        # 1. WeChat 消息（需要先删除消息，再删除绑定）
        wechat_bindings = db.query(WeChatBinding).filter(
            WeChatBinding.agent_hash == agent_hash
        ).all()
        binding_ids = [b.id for b in wechat_bindings]
        
        if binding_ids:
            deleted_messages = db.query(WeChatMessage).filter(
                WeChatMessage.binding_id.in_(binding_ids)
            ).delete(synchronize_session=False)
            results["wechat_messages"] = deleted_messages
            logger.info(f"Deleted {deleted_messages} WeChat messages for agent {agent_hash}")

        # 2. WeChat 绑定
        deleted_bindings = db.query(WeChatBinding).filter(
            WeChatBinding.agent_hash == agent_hash
        ).delete(synchronize_session=False)
        results["wechat_bindings"] = deleted_bindings
        if deleted_bindings > 0:
            logger.info(f"Deleted {deleted_bindings} WeChat bindings for agent {agent_hash}")

        # 3. 文件权限
        deleted_permissions = db.query(FilePermission).filter(
            FilePermission.agent_hash == agent_hash
        ).delete(synchronize_session=False)
        results["file_permissions"] = deleted_permissions
        if deleted_permissions > 0:
            logger.info(f"Deleted {deleted_permissions} file permissions for agent {agent_hash}")

        # 4. Agent 配置
        deleted_configs = db.query(AgentConfigModel).filter(
            AgentConfigModel.agent_hash == agent_hash
        ).delete(synchronize_session=False)
        results["agent_configs"] = deleted_configs
        if deleted_configs > 0:
            logger.info(f"Deleted {deleted_configs} agent configs for agent {agent_hash}")

        # 5. Agent 使用日志
        deleted_logs = db.query(AgentUsageLog).filter(
            AgentUsageLog.agent_hash == agent_hash
        ).delete(synchronize_session=False)
        results["agent_usage_logs"] = deleted_logs
        if deleted_logs > 0:
            logger.info(f"Deleted {deleted_logs} usage logs for agent {agent_hash}")

        # 6. 对话会话
        deleted_sessions = db.query(ConversationSession).filter(
            ConversationSession.agent_hash == agent_hash
        ).delete(synchronize_session=False)
        results["conversation_sessions"] = deleted_sessions
        if deleted_sessions > 0:
            logger.info(f"Deleted {deleted_sessions} conversation sessions for agent {agent_hash}")

        # 7. 定时任务
        deleted_tasks = db.query(ScheduledTask).filter(
            ScheduledTask.agent_hash == agent_hash
        ).delete(synchronize_session=False)
        results["scheduled_tasks"] = deleted_tasks
        if deleted_tasks > 0:
            logger.info(f"Deleted {deleted_tasks} scheduled tasks for agent {agent_hash}")

        # 8. 分享映射
        deleted_shares = db.query(ShareMapping).filter(
            ShareMapping.agent_hash == agent_hash
        ).delete(synchronize_session=False)
        results["share_mappings"] = deleted_shares
        if deleted_shares > 0:
            logger.info(f"Deleted {deleted_shares} share mappings for agent {agent_hash}")

        # 9. 聊天历史
        deleted_history = db.query(ChatHistory).filter(
            ChatHistory.agent_hash == agent_hash
        ).delete(synchronize_session=False)
        results["chat_history"] = deleted_history
        if deleted_history > 0:
            logger.info(f"Deleted {deleted_history} chat history records for agent {agent_hash}")

        return results

    def _cleanup_vfs_storage(
        self,
        agent_hash: str,
    ) -> Dict[str, Any]:
        """
        清理 VFS 存储数据（COS）

        Args:
            agent_hash: Agent hash

        Returns:
            清理结果
        """
        results = {
            "deleted_files": 0,
            "errors": []
        }

        if not self.storage:
            results["message"] = "StorageService not available, skipping VFS cleanup"
            return results

        # VFS 基础路径: feclaw/agents/{agent_hash}/
        vfs_prefix = f"{settings.TENCENT_COS_PREFIX}agents/{agent_hash}/"

        try:
            # 列出所有对象
            objects = self.storage.list_objects(vfs_prefix)
            
            if not objects:
                results["message"] = f"No VFS files found under {vfs_prefix}"
                return results

            # 删除所有对象
            deleted_count = 0
            for obj in objects:
                key = obj.get("Key")
                if key:
                    try:
                        self.storage.client.delete_object(
                            Bucket=settings.TENCENT_COS_BUCKET,
                            Key=key
                        )
                        deleted_count += 1
                        logger.debug(f"Deleted VFS file: {key}")
                    except Exception as e:
                        error_msg = f"Failed to delete {key}: {str(e)}"
                        logger.warning(error_msg)
                        results["errors"].append(error_msg)

            results["deleted_files"] = deleted_count
            results["prefix"] = vfs_prefix
            logger.info(f"Deleted {deleted_count} VFS files for agent {agent_hash}")

        except Exception as e:
            error_msg = f"Failed to list/delete VFS objects: {str(e)}"
            logger.error(error_msg)
            results["errors"].append(error_msg)

        return results

    def _cleanup_local_files(self, agent_hash: str) -> Dict[str, Any]:
        """
        清理 Agent 配置文件（从 AgentConfig 数据库表删除）

        Args:
            agent_hash: Agent hash

        Returns:
            清理结果
        """
        results = {
            "deleted_files": [],
            "removed_dir": False,
            "errors": []
        }

        db = SessionLocal()
        try:
            # 从 AgentConfig 表删除该 agent 的所有配置
            deleted_count = db.query(AgentConfigModel).filter(
                AgentConfigModel.agent_hash == agent_hash
            ).delete()
            db.commit()

            results["deleted_files"] = [f"agent_config:{agent_hash}"]
            results["removed_dir"] = True
            logger.info(f"Cleaned up {deleted_count} AgentConfig entries for agent {agent_hash}")
        except Exception as e:
            error_msg = f"Failed to clean AgentConfig for {agent_hash}: {str(e)}"
            logger.error(error_msg)
            results["errors"].append(error_msg)
            db.rollback()
        finally:
            db.close()

        return results


# 全局实例
agent_cleanup_service = AgentCleanupService()
