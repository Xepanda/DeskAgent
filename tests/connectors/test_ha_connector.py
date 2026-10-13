"""
Unit tests for Home Assistant connector.
"""

from __future__ import annotations

import unittest
from host.desk_agent.connectors.ha.ha_connector import HAConnector


class TestHAConnector(unittest.TestCase):
    def setUp(self) -> None:
        self.ha = HAConnector()

    def test_domain_allowlist_enforcement(self) -> None:
        # Valid domain: switch
        res = self.ha.execute("switch.heater", "turn_on")
        self.assertTrue(res["accepted"])
        self.assertEqual(res["entity_id"], "switch.heater")

        # Invalid domain: camera (camera is LAN viewed only, not HA controlled)
        with self.assertRaises(PermissionError) as ctx:
            self.ha.execute("camera.front_door", "take_snapshot")
        self.assertIn("outside allowed domain scope", str(ctx.exception))

    def test_state_confirmation_success(self) -> None:
        res = self.ha.execute(
            entity_id="light.reading_lamp",
            action="turn_on",
            wait_for_state={"expected_state": "on", "timeout_ms": 500},
        )
        self.assertTrue(res["accepted"])
        self.assertTrue(res["state_confirmed"])
        self.assertEqual(res["observed_state"], "on")

    def test_get_state(self) -> None:
        st = self.ha.get_state("light.desk")
        self.assertIn("entity_id", st)
        self.assertIn("state", st)
        self.assertIn("attributes", st)


if __name__ == "__main__":
    unittest.main()
