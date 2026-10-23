"""Fixed WatcheRobot Managed Application entrypoint for Desk Agent Host.

The SDK Runtime launches this file after injecting its managed-session
environment. The entrypoint delegates to the existing Host main path, which
detects that environment and enters ApplicationContext.from_environment().
"""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> None:
    install_root = Path(__file__).resolve().parents[1]
    if str(install_root) not in sys.path:
        sys.path.insert(0, str(install_root))

    from host.desk_agent.main import main as run_host

    config_path = "/etc/desk-agent/config.json"
    sys.argv = ["desk-agent-managed-app", "--config", config_path]
    run_host()


if __name__ == "__main__":
    main()
