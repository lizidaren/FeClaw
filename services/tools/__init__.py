"""
Agent 工具服务 - tools 包

从 agent_tools_service.py 拆分出的模块化工具服务。
所有工具以 Mixin 方式组合到 AgentToolsService 中。
外部导入方式不变：from services.agent_tools_service import AgentToolsService
"""

from services.tools.base import AgentToolsServiceBase, Step, DEFAULT_CONFIG
from services.tools.file_ops import FileOpsMixin
from services.tools.ai_tools import AIToolsMixin
from services.tools.web_tools import WebToolsMixin
from services.tools.session_tools import SessionToolsMixin
from services.tools.bash_tools import BashToolsMixin
from services.tools.cron_tools import CronToolsMixin
from services.tools.share_tools import ShareToolsMixin
from services.tools.knowledge_tools import KnowledgeToolsMixin
from services.tools.route_tools import RouteToolsMixin
from services.tools.moments_tools import MomentsToolsMixin
from services.tools.fehub_tools import FeHubToolsMixin
from services.tools.tts_tools import TtsToolsMixin
from services.tools.universal_parser import ParseFileMixin
from services.tools.pptx_tools import PptxToolsMixin


class AgentToolsService(
    TtsToolsMixin,
    PptxToolsMixin,
    ParseFileMixin,
    FeHubToolsMixin,
    MomentsToolsMixin,
    FileOpsMixin,
    AIToolsMixin,
    WebToolsMixin,
    SessionToolsMixin,
    BashToolsMixin,
    CronToolsMixin,
    ShareToolsMixin,
    KnowledgeToolsMixin,
    RouteToolsMixin,
):
    """
    Agent 工具服务

    由多个 Mixin 组合而成，每个 Mixin 负责一类工具。
    导出与原 AgentToolsService 完全相同的接口。
    """
    pass


__all__ = ["AgentToolsService", "Step", "DEFAULT_CONFIG"]
