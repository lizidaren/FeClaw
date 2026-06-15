"""
VFS builtin commands package.
"""
from .ls import LsCommand
from .cd import CdCommand
from .pwd import PwdCommand
from .cat import CatCommand
from .echo import EchoCommand
from .grep import GrepCommand
from .find import FindCommand
from .touch import TouchCommand
from .mkdir import MkdirCommand
from .rm import RmCommand
from .rmdir import RmdirCommand
from .cp import CpCommand
from .mv import MvCommand
from .wc import WcCommand
from .head import HeadCommand
from .tail import TailCommand
from .sort import SortCommand
from .uniq import UniqCommand
from .stat import StatCommand
from .date import DateCommand
from .exit import ExitCommand
from .help import HelpCommand
from .cut import CutCommand
from .diff import DiffCommand
from .awk import AwkCommand
from .sed import SedCommand

# Registry function to register all builtin commands
from ..registry import register_command


def register_all_commands():
    """Register all builtin commands with the global registry."""
    register_command("ls", LsCommand)
    register_command("cd", CdCommand)
    register_command("pwd", PwdCommand)
    register_command("cat", CatCommand)
    register_command("echo", EchoCommand)
    register_command("grep", GrepCommand)
    register_command("find", FindCommand)
    register_command("touch", TouchCommand)
    register_command("mkdir", MkdirCommand)
    register_command("rm", RmCommand)
    register_command("rmdir", RmdirCommand)
    register_command("cp", CpCommand)
    register_command("mv", MvCommand)
    register_command("wc", WcCommand)
    register_command("head", HeadCommand)
    register_command("tail", TailCommand)
    register_command("sort", SortCommand)
    register_command("uniq", UniqCommand)
    register_command("stat", StatCommand)
    register_command("date", DateCommand)
    register_command("exit", ExitCommand)
    register_command("help", HelpCommand)
    register_command("cut", CutCommand)
    register_command("diff", DiffCommand)
    register_command("awk", AwkCommand)
    register_command("sed", SedCommand)
