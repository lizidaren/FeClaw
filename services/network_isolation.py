"""
Network isolation management for FeClaw sandbox.

Provides NetworkIsolationManager to check whether the shared sandbox network
namespace is available and provide the setuid helper path for bwrap.

Architecture:
- All root-privileged setup is handled by systemd oneshot (feclaw-netns.service)
- Runtime entry to netns is via a setuid C helper (feclaw-netns-helper)
- The helper opens the netns fd, enters it, drops privileges, then execs bwrap
- This module never runs privileged operations — only checks existence
"""

import os
import logging
import subprocess

logger = logging.getLogger(__name__)

NETNS_NAME = "feclaw-sandbox"
HELPER_PATH = "/usr/local/libexec/feclaw/helper"
CLEANUP_SCRIPT = "scripts/cleanup_sandbox_netns.sh"


class NetworkIsolationManager:
    """Manages sandbox network namespace access.

    Does NOT create netns or iptables rules (handled by systemd oneshot).
    Runtime netns entry uses a setuid C helper binary.

    Security properties:
    - helper is the ONLY setuid binary in the feclaw chain
    - helper's root window is ~0.012ms (setns -> setuid -> prctl)
    - After dropping privileges, bwrap runs as the backend user (lch)
    - NO sudo required at runtime
    - NO sudoers configuration to maintain
    """

    @classmethod
    def check(cls) -> bool:
        """Check if the sandbox netns exists.

        Returns True if feclaw-sandbox netns is available.
        """
        try:
            result = subprocess.run(
                ["ip", "netns", "list"],
                capture_output=True, text=True, timeout=5,
            )
            available = NETNS_NAME in result.stdout
            if available:
                logger.info("Sandbox netns '%s' is available", NETNS_NAME)
            else:
                logger.warning("Sandbox netns '%s' not found", NETNS_NAME)
            return available
        except Exception as e:
            logger.warning("Failed to check netns availability: %s", e)
            return False

    @classmethod
    def get_netns_prefix(cls) -> list:
        """Return the setuid helper path to enter the sandbox netns.

        Returns ["/usr/local/libexec/feclaw/helper"] — prepend this
        before bwrap in the command line:

            helper bwrap --dev /dev --proc /proc ...

        The helper:
        1. Opens feclaw-sandbox netns fd (setuid root → EUID=root)
        2. Calls setns(CLONE_NEWNET) — enters shared netns
        3. NO_NEW_PRIVS + seccomp BPF (7 dangerous syscalls blocked)
        4. Drops privileges: setgroups(0) + setgid + setuid (EUID=lch)
        5. Execs bwrap as user lch with seccomp active

        Fallback: returns empty list if helper doesn't exist
        (sandbox runs without network isolation)
        """
        if os.path.exists(HELPER_PATH) and os.access(HELPER_PATH, os.X_OK):
            return [HELPER_PATH]
        logger.warning("Netns helper not found at %s, falling back", HELPER_PATH)
        return []

    @classmethod
    def cleanup(cls) -> None:
        """Run the cleanup shell script (requires root via sudo)."""
        script_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            CLEANUP_SCRIPT,
        )
        try:
            subprocess.run(
                ["sudo", "-n", "bash", script_path],
                capture_output=True, text=True, timeout=30,
            )
            logger.info("Sandbox netns cleanup completed")
        except Exception as e:
            logger.warning("Sandbox netns cleanup failed: %s", e)
