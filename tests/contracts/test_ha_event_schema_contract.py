"""Schema and fixture checks for the approved HA state-change contract."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

import jsonschema


REPO_ROOT = Path(__file__).resolve().parents[2]


class HAEventSchemaContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.envelope_schema = json.loads(
            (REPO_ROOT / "contracts/schemas/envelope_event.json").read_text(encoding="utf-8")
        )
        domain_schema = json.loads(
            (REPO_ROOT / "contracts/schemas/domain_ha.json").read_text(encoding="utf-8")
        )
        cls.payload_schema = domain_schema["definitions"]["ha_state_changed_payload"]

    def _load_fixture(self, name: str) -> dict:
        return json.loads(
            (REPO_ROOT / "contracts/fixtures/events" / name).read_text(encoding="utf-8")
        )

    def _assert_valid_event(self, event: dict) -> None:
        jsonschema.validate(event, self.envelope_schema)
        self.assertEqual(event["type"], "ha.state_changed")
        jsonschema.validate(event["payload"], self.payload_schema)

    def test_state_only_change_fixture_is_valid(self) -> None:
        event = self._load_fixture("ha_state_only_changed.json")
        self._assert_valid_event(event)
        self.assertEqual(event["payload"]["old_state"], "off")
        self.assertEqual(event["payload"]["new_state"], "on")
        self.assertNotIn("attributes", event["payload"])

    def test_attributes_only_change_fixture_is_valid(self) -> None:
        event = self._load_fixture("ha_state_changed.json")
        self._assert_valid_event(event)
        self.assertEqual(event["payload"]["old_state"], event["payload"]["new_state"])
        self.assertIsInstance(event["payload"]["attributes"], dict)

    def test_new_entity_fixture_is_valid(self) -> None:
        event = self._load_fixture("ha_entity_added.json")
        self._assert_valid_event(event)
        self.assertIsNone(event["payload"]["old_state"])
        self.assertIsInstance(event["payload"]["new_state"], str)
        self.assertIsInstance(event["payload"]["attributes"], dict)

    def test_entity_removal_fixture_is_valid(self) -> None:
        event = self._load_fixture("ha_entity_removed.json")
        self._assert_valid_event(event)
        self.assertIsNone(event["payload"]["new_state"])
        self.assertNotIn("attributes", event["payload"])

    def test_attributes_must_be_an_object(self) -> None:
        event = self._load_fixture("ha_state_changed.json")
        for invalid_attributes in (["brightness", 200], "brightness=200", None, 200):
            with self.subTest(attributes=invalid_attributes):
                event["payload"]["attributes"] = invalid_attributes
                with self.assertRaises(jsonschema.ValidationError):
                    jsonschema.validate(event["payload"], self.payload_schema)

    def test_removal_must_omit_attributes(self) -> None:
        event = self._load_fixture("ha_entity_removed.json")
        event["payload"]["attributes"] = {}
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(event["payload"], self.payload_schema)


if __name__ == "__main__":
    unittest.main()
