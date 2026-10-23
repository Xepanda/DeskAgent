from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT / "deploy" / "edge" / "managed-app"


class EdgeManagedAppBundleTests(unittest.TestCase):
    def test_manifest_pins_sdk_and_does_not_claim_linux_support(self) -> None:
        manifest = json.loads((BUNDLE / "app.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["requires_watcherobot"], ">=0.1.11,<0.2")
        self.assertNotIn("supported_host_platforms", manifest)
        self.assertIn("websockets>=14,<16", manifest["dependencies"])

        release = json.loads((BUNDLE / "release-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(release["sdk"]["git_commit"], "8bc13967a06ef073459be47e930ff43ef37ee8fb")
        self.assertIsNone(release["sdk"]["wheel_sha256"])
        self.assertEqual(release["target"]["official_sdk_support"], "not_declared")

    def test_entrypoint_calls_existing_host_with_managed_config(self) -> None:
        entrypoint = BUNDLE / "app.py"
        namespace: dict[str, object] = {"__file__": str(entrypoint), "__name__": "edge_managed_app_test"}
        exec(compile(entrypoint.read_text(encoding="utf-8"), str(entrypoint), "exec"), namespace)
        calls: list[bool] = []

        def run_host() -> None:
            calls.append(True)
            self.assertEqual(
                sys.argv,
                ["desk-agent-managed-app", "--config", "/etc/desk-agent/config.json"],
            )

        with patch.dict(sys.modules, {"host.desk_agent.main": type(sys)("host.desk_agent.main")}):
            sys.modules["host.desk_agent.main"].main = run_host
            namespace["main"]()

        self.assertEqual(calls, [True])


if __name__ == "__main__":
    unittest.main()
