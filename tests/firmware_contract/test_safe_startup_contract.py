"""
Contract tests for Desk Agent R1 Safe Startup & Boot Integration.
Verifies that:
1. app_main() initializes hardware, TClaw, and SDK bridge safely.
2. Initial TClaw state is TCLAW_STATE_IDLE (never pre-emptively LISTENING).
3. Tuya Cloud state is TUYA_CLOUD_DISCONNECTED (no fake live connection claimed on boot).
4. No unsolicited motion MCU packets are transmitted during boot.
5. All hardware arbiters start in DESK_OWNER_NONE (clean lock slate).
6. Display expression initializes to 'idle' / 'Ready'.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import tempfile
import unittest

# TClaw states
TCLAW_STATE_IDLE = 0
TCLAW_STATE_LISTENING = 1
TCLAW_STATE_SPEAKING = 3

# Tuya Cloud states
TUYA_CLOUD_DISCONNECTED = 0
TUYA_CLOUD_CONNECTING = 1
TUYA_CLOUD_CONNECTED = 2

# Arbiter resource IDs
DESK_RES_AUDIO_PLAYBACK = 0
DESK_RES_AUDIO_RECORD = 1
DESK_RES_CAMERA_SENSOR = 2
DESK_RES_MOTION_GIMBAL = 3
DESK_RES_DISPLAY_UI = 4

DESK_OWNER_NONE = 0
DESK_OWNER_SYSTEM_ALERT = 5

# Hardware Error codes
DESK_HW_OK = 0
DESK_HW_ERR_INVALID_ARG = -1
DESK_HW_ERR_UNAVAILABLE = -2
DESK_HW_ERR_BUSY = -5

MCU_TX_CB = ctypes.CFUNCTYPE(None, ctypes.c_char_p, ctypes.c_size_t, ctypes.c_void_p)


class DeskDisplaySelfTest(ctypes.Structure):
    _fields_ = [
        ("initialized", ctypes.c_bool),
        ("strict_mode", ctypes.c_bool),
        ("current_preset", ctypes.c_char * 32),
        ("current_status", ctypes.c_char * 64),
        ("arbiter_owner", ctypes.c_int),
        ("last_error_code", ctypes.c_int),
    ]


class TestSafeStartupContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.so_path = os.path.join(cls.temp_dir.name, "libdesk_startup_test.so")
        repo_root = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", ".."))

        # Compile full firmware stack including app_main
        cmd = [
            "gcc",
            "-shared",
            "-fPIC",
            "-O2",
            "-Wall",
            "-Dmain=standalone_test_main",
            f"-I{repo_root}/firmware/components/desk_hardware/include",
            f"-I{repo_root}/firmware/components/desk_tclaw/include",
            f"-I{repo_root}/firmware/components/desk_sdk_bridge/include",
            f"-I{repo_root}/firmware/components/desk_cli/include",
            f"-I{repo_root}/firmware/main/include",
            f"{repo_root}/firmware/components/desk_hardware/src/desk_arbiter.c",
            f"{repo_root}/firmware/components/desk_hardware/src/desk_hardware.c",
            f"{repo_root}/firmware/components/desk_tclaw/src/desk_tclaw_bridge.c",
            f"{repo_root}/firmware/components/desk_sdk_bridge/src/desk_sdk_bridge.c",
            f"{repo_root}/firmware/components/desk_sdk_bridge/src/desk_sdk_websocket.c",
            f"{repo_root}/firmware/components/desk_cli/src/desk_cli.c",
            f"{repo_root}/firmware/main/src/provisioning_state.c",
            f"{repo_root}/firmware/main/src/tclaw_runtime_adapter.c",
            f"{repo_root}/firmware/main/src/app_main.c",
            "-o",
            cls.so_path,
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"Failed to compile desk_startup shared library: {res.stderr}")

        cls.lib = ctypes.CDLL(cls.so_path)

        # Function signatures
        cls.lib.app_main.restype = ctypes.c_int
        cls.lib.app_main.argtypes = []

        cls.lib.desk_tclaw_get_state.restype = ctypes.c_int
        cls.lib.desk_tclaw_get_state.argtypes = []

        cls.lib.desk_tclaw_tuya_session_get_connection_state.restype = ctypes.c_int
        cls.lib.desk_tclaw_tuya_session_get_connection_state.argtypes = []

        cls.lib.desk_tclaw_tuya_get_session_id.restype = ctypes.c_char_p
        cls.lib.desk_tclaw_tuya_get_session_id.argtypes = []

        cls.lib.desk_arbiter_get_owner.restype = ctypes.c_int
        cls.lib.desk_arbiter_get_owner.argtypes = [ctypes.c_int]

        cls.lib.desk_hardware_display_get_expression.restype = ctypes.c_int
        cls.lib.desk_hardware_display_get_expression.argtypes = [
            ctypes.c_char_p,
            ctypes.c_size_t,
            ctypes.c_char_p,
            ctypes.c_size_t,
        ]

        cls.lib.desk_hardware_set_screen_expression.restype = ctypes.c_int
        cls.lib.desk_hardware_set_screen_expression.argtypes = [ctypes.c_char_p, ctypes.c_char_p]

        cls.lib.desk_hardware_display_set_expression.restype = ctypes.c_int
        cls.lib.desk_hardware_display_set_expression.argtypes = [ctypes.c_char_p, ctypes.c_char_p]

        cls.lib.desk_hardware_display_self_test.restype = ctypes.c_int
        cls.lib.desk_hardware_display_self_test.argtypes = [ctypes.POINTER(DeskDisplaySelfTest)]

        cls.lib.desk_hardware_screen_self_test.restype = ctypes.c_int
        cls.lib.desk_hardware_screen_self_test.argtypes = [ctypes.POINTER(DeskDisplaySelfTest)]

        cls.lib.desk_hardware_set_strict_mode.restype = None
        cls.lib.desk_hardware_set_strict_mode.argtypes = [ctypes.c_bool]

        cls.lib.desk_hardware_is_strict_mode.restype = ctypes.c_bool
        cls.lib.desk_hardware_is_strict_mode.argtypes = []

        cls.lib.desk_hardware_set_display_initialized.restype = None
        cls.lib.desk_hardware_set_display_initialized.argtypes = [ctypes.c_bool]

        cls.lib.desk_hardware_is_display_initialized.restype = ctypes.c_bool
        cls.lib.desk_hardware_is_display_initialized.argtypes = []

        cls.lib.desk_hardware_motion_move_to.restype = ctypes.c_int
        cls.lib.desk_hardware_motion_move_to.argtypes = [ctypes.c_float, ctypes.c_float, ctypes.c_uint32]

        cls.lib.desk_arbiter_acquire.restype = ctypes.c_int
        cls.lib.desk_arbiter_acquire.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint32]

        cls.lib.desk_arbiter_release.restype = None
        cls.lib.desk_arbiter_release.argtypes = [ctypes.c_int, ctypes.c_int]

        cls.lib.desk_hardware_register_mcu_tx_cb.restype = ctypes.c_int
        cls.lib.desk_hardware_register_mcu_tx_cb.argtypes = [MCU_TX_CB, ctypes.c_void_p]

        cls.lib.desk_firmware_stop_background_task.restype = None
        cls.lib.desk_firmware_stop_background_task.argtypes = []

        cls.lib.desk_sdk_socket_server_stop.restype = ctypes.c_int
        cls.lib.desk_sdk_socket_server_stop.argtypes = []

        cls.lib.desk_sdk_pairing_server_stop.restype = ctypes.c_int
        cls.lib.desk_sdk_pairing_server_stop.argtypes = []

        cls.lib.desk_hardware_init.restype = ctypes.c_int
        cls.lib.desk_hardware_init.argtypes = []

        cls.lib.desk_tclaw_reset.restype = None
        cls.lib.desk_tclaw_reset.argtypes = []

        cls.lib.desk_sdk_bridge_reset.restype = None
        cls.lib.desk_sdk_bridge_reset.argtypes = []

        # Tuya OpenSDK integration signatures
        cls.lib.desk_tuya_simulate_event.restype = ctypes.c_int
        cls.lib.desk_tuya_simulate_event.argtypes = [ctypes.c_int]

        cls.lib.desk_tuya_get_pid.restype = ctypes.c_char_p
        cls.lib.desk_tuya_get_pid.argtypes = []

        cls.lib.desk_tuya_get_uuid.restype = ctypes.c_char_p
        cls.lib.desk_tuya_get_uuid.argtypes = []

        cls.lib.desk_tuya_get_authkey.restype = ctypes.c_char_p
        cls.lib.desk_tuya_get_authkey.argtypes = []

        cls.lib.desk_tclaw_on_tuya_mqtt_connected.restype = ctypes.c_int
        cls.lib.desk_tclaw_on_tuya_mqtt_connected.argtypes = []

        cls.lib.desk_tclaw_on_tuya_mqtt_disconnected.restype = ctypes.c_int
        cls.lib.desk_tclaw_on_tuya_mqtt_disconnected.argtypes = []

        cls.lib.desk_tclaw_on_tuya_netcfg_started.restype = ctypes.c_int
        cls.lib.desk_tclaw_on_tuya_netcfg_started.argtypes = []

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp_dir.cleanup()

    def tearDown(self) -> None:
        self.lib.desk_firmware_stop_background_task()
        self.lib.desk_sdk_socket_server_stop()
        self.lib.desk_sdk_pairing_server_stop()
        self.lib.desk_tclaw_reset()
        self.lib.desk_sdk_bridge_reset()

    def test_boot_clean_state_machine(self) -> None:
        """Verifies app_main() boots cleanly into IDLE with Tuya disconnected."""
        rc = self.lib.app_main()
        self.assertEqual(rc, 0, "app_main() must return 0 on successful boot")

        # TClaw state must be IDLE
        tclaw_state = self.lib.desk_tclaw_get_state()
        self.assertEqual(tclaw_state, TCLAW_STATE_IDLE, "TClaw state must be IDLE at startup")

        # Tuya cloud must NOT claim connected or streaming
        tuya_state = self.lib.desk_tclaw_tuya_session_get_connection_state()
        self.assertEqual(
            tuya_state,
            TUYA_CLOUD_DISCONNECTED,
            "Tuya cloud session must be DISCONNECTED at boot without credentials",
        )

        # No fake session ID
        sess_id = self.lib.desk_tclaw_tuya_get_session_id()
        self.assertEqual(sess_id, b"", "Session ID must be empty at boot")

    def test_boot_resource_arbiters_unacquired(self) -> None:
        """Verifies all resource arbiters start in DESK_OWNER_NONE."""
        rc = self.lib.app_main()
        self.assertEqual(rc, 0)

        resources = [
            ("AUDIO_PLAYBACK", DESK_RES_AUDIO_PLAYBACK),
            ("AUDIO_RECORD", DESK_RES_AUDIO_RECORD),
            ("CAMERA_SENSOR", DESK_RES_CAMERA_SENSOR),
            ("MOTION_GIMBAL", DESK_RES_MOTION_GIMBAL),
            ("DISPLAY_UI", DESK_RES_DISPLAY_UI),
        ]
        for name, res_id in resources:
            owner = self.lib.desk_arbiter_get_owner(res_id)
            self.assertEqual(owner, DESK_OWNER_NONE, f"Resource {name} must be unacquired at boot")

    def test_boot_expression_preset_is_idle(self) -> None:
        """Verifies display expression preset is 'idle' and text is 'Ready'."""
        rc = self.lib.app_main()
        self.assertEqual(rc, 0)

        preset_buf = ctypes.create_string_buffer(64)
        text_buf = ctypes.create_string_buffer(256)
        rc_disp = self.lib.desk_hardware_display_get_expression(preset_buf, 64, text_buf, 256)
        self.assertEqual(rc_disp, 0)
        self.assertEqual(preset_buf.value.decode("utf-8"), "idle")
        self.assertIn("Ready", text_buf.value.decode("utf-8"))

    def test_boot_emits_no_unsolicited_motion_packets(self) -> None:
        """Verifies boot sequence transmits zero unsolicited MCU motion packets."""
        transmitted_packets: list[bytes] = []

        @MCU_TX_CB
        def on_mcu_tx(data, length, user_data):
            raw = ctypes.string_at(data, length)
            transmitted_packets.append(raw)

        # Pre-register callback before app_main runs
        self.lib.desk_hardware_init()
        self.lib.desk_hardware_register_mcu_tx_cb(on_mcu_tx, None)

        rc = self.lib.app_main()
        self.assertEqual(rc, 0)

        # Boot must emit 0 motion packets
        self.assertEqual(
            len(transmitted_packets),
            0,
            f"Boot must NOT send unsolicited motion packets to MCU (got {len(transmitted_packets)})",
        )

    def test_display_self_test_interface(self) -> None:
        """Verifies display subsystem self-test inspection report and strict mode gating."""
        rc = self.lib.app_main()
        self.assertEqual(rc, 0)

        test_report = DeskDisplaySelfTest()
        rc_diag = self.lib.desk_hardware_display_self_test(ctypes.byref(test_report))
        self.assertEqual(rc_diag, DESK_HW_OK)
        self.assertEqual(test_report.current_preset.decode("utf-8"), "idle")
        self.assertIn("Ready", test_report.current_status.decode("utf-8"))
        self.assertEqual(test_report.arbiter_owner, DESK_OWNER_NONE)
        self.assertEqual(test_report.last_error_code, DESK_HW_OK)

        # Alias interface desk_hardware_screen_self_test
        test_report_alias = DeskDisplaySelfTest()
        rc_alias = self.lib.desk_hardware_screen_self_test(ctypes.byref(test_report_alias))
        self.assertEqual(rc_alias, DESK_HW_OK)
        self.assertEqual(test_report_alias.current_preset.decode("utf-8"), "idle")

        # Strict mode: uninitialized display fails self-test with DESK_HW_ERR_UNAVAILABLE
        self.lib.desk_hardware_set_strict_mode(True)
        self.lib.desk_hardware_set_display_initialized(False)

        strict_report = DeskDisplaySelfTest()
        rc_strict = self.lib.desk_hardware_display_self_test(ctypes.byref(strict_report))
        self.assertEqual(rc_strict, DESK_HW_ERR_UNAVAILABLE)
        self.assertEqual(strict_report.last_error_code, DESK_HW_ERR_UNAVAILABLE)
        self.assertFalse(strict_report.initialized)

        # Re-enabling initialization restores PASS
        self.lib.desk_hardware_set_display_initialized(True)
        rc_restored = self.lib.desk_hardware_display_self_test(ctypes.byref(strict_report))
        self.assertEqual(rc_restored, DESK_HW_OK)
        self.assertTrue(strict_report.initialized)

        self.lib.desk_hardware_set_strict_mode(False)

    def test_set_screen_expression_interface(self) -> None:
        """Verifies desk_hardware_set_screen_expression() updates display and enforces arbitration."""
        rc = self.lib.app_main()
        self.assertEqual(rc, 0)

        # 1. Update expression via new R2 interface
        rc_set = self.lib.desk_hardware_set_screen_expression(b"listening", b"User Query Active")
        self.assertEqual(rc_set, DESK_HW_OK)

        # 2. Verify display readback
        preset_buf = ctypes.create_string_buffer(64)
        text_buf = ctypes.create_string_buffer(256)
        rc_disp = self.lib.desk_hardware_display_get_expression(preset_buf, 64, text_buf, 256)
        self.assertEqual(rc_disp, 0)
        self.assertEqual(preset_buf.value.decode("utf-8"), "listening")
        self.assertEqual(text_buf.value.decode("utf-8"), "User Query Active")

        # 3. Verify self-test diagnostic reflects new expression
        diag = DeskDisplaySelfTest()
        self.lib.desk_hardware_display_self_test(ctypes.byref(diag))
        self.assertEqual(diag.current_preset.decode("utf-8"), "listening")
        self.assertEqual(diag.current_status.decode("utf-8"), "User Query Active")

        # 4. Input validation (null/empty preset rejected)
        rc_null = self.lib.desk_hardware_set_screen_expression(None, b"test")
        self.assertEqual(rc_null, DESK_HW_ERR_INVALID_ARG)
        rc_empty = self.lib.desk_hardware_set_screen_expression(b"", b"test")
        self.assertEqual(rc_empty, DESK_HW_ERR_INVALID_ARG)

        # 5. Arbiter must be clean after call completes (no leaked lock)
        self.assertEqual(self.lib.desk_arbiter_get_owner(DESK_RES_DISPLAY_UI), DESK_OWNER_NONE)

    def test_no_bypass_hardware_motion(self) -> None:
        """Verifies that un-arbitrated / locked hardware cannot execute motion actions."""
        rc = self.lib.app_main()
        self.assertEqual(rc, 0)

        # Acquire lock under system alert
        acq_rc = self.lib.desk_arbiter_acquire(DESK_RES_MOTION_GIMBAL, DESK_OWNER_SYSTEM_ALERT, 0)
        self.assertEqual(acq_rc, 0)

        # Direct move attempt must be rejected (cannot bypass arbiter)
        move_rc = self.lib.desk_hardware_motion_move_to(ctypes.c_float(90.0), ctypes.c_float(120.0), 100)
        self.assertEqual(move_rc, -1, "Motion must be rejected when gimbal is held by another owner")

        self.lib.desk_arbiter_release(DESK_RES_MOTION_GIMBAL, DESK_OWNER_SYSTEM_ALERT)
        self.assertEqual(self.lib.desk_arbiter_get_owner(DESK_RES_MOTION_GIMBAL), DESK_OWNER_NONE)

    def test_tuya_credentials_and_pid_initialization(self) -> None:
        """Verifies Tuya PID, UUID and AuthKey match tuya_config.h."""
        rc = self.lib.app_main()
        self.assertEqual(rc, 0)

        pid = self.lib.desk_tuya_get_pid().decode("utf-8")
        self.assertEqual(pid, "rhbbg6sazsavynoj", "PID must match Tuya Platform PID")

        uuid = self.lib.desk_tuya_get_uuid().decode("utf-8")
        self.assertEqual(uuid, "host-test-uuid", "host tests must use a non-production placeholder")

        authkey = self.lib.desk_tuya_get_authkey().decode("utf-8")
        self.assertEqual(authkey, "host-test-authkey", "host tests must use a non-production placeholder")

    def test_tuya_event_transitions_and_screen_expressions(self) -> None:
        """Verifies screen expression states:
        - Boot: 'idle' / 'Ready - Waiting Net'
        - BIND_START: 'thinking' / 'BLE Netcfg'
        - MQTT_CONNECTED: 'happy' / 'Online'
        - MQTT_DISCONNECT: 'idle' / 'Cloud Offline'
        """
        rc = self.lib.app_main()
        self.assertEqual(rc, 0)

        preset_buf = ctypes.create_string_buffer(64)
        text_buf = ctypes.create_string_buffer(256)

        # 1. Initial boot state: idle / Waiting Net
        self.assertEqual(self.lib.desk_tclaw_tuya_session_get_connection_state(), TUYA_CLOUD_DISCONNECTED)
        self.lib.desk_hardware_display_get_expression(preset_buf, 64, text_buf, 256)
        self.assertEqual(preset_buf.value.decode("utf-8"), "idle")
        self.assertIn("Waiting Net", text_buf.value.decode("utf-8"))

        # 2. TUYA_EVENT_BIND_START (BLE netcfg started) -> thinking / BLE Netcfg
        TUYA_EVENT_BIND_START = 1
        rc_evt = self.lib.desk_tuya_simulate_event(TUYA_EVENT_BIND_START)
        self.assertEqual(rc_evt, 0)
        self.lib.desk_hardware_display_get_expression(preset_buf, 64, text_buf, 256)
        self.assertEqual(preset_buf.value.decode("utf-8"), "thinking")
        self.assertEqual(text_buf.value.decode("utf-8"), "BLE Netcfg")

        # 3. TUYA_EVENT_MQTT_CONNECTED -> happy / Online & state TUYA_CLOUD_CONNECTED
        TUYA_EVENT_MQTT_CONNECTED = 4
        rc_evt = self.lib.desk_tuya_simulate_event(TUYA_EVENT_MQTT_CONNECTED)
        self.assertEqual(rc_evt, 0)
        self.assertEqual(self.lib.desk_tclaw_tuya_session_get_connection_state(), TUYA_CLOUD_CONNECTED)
        self.lib.desk_hardware_display_get_expression(preset_buf, 64, text_buf, 256)
        self.assertEqual(preset_buf.value.decode("utf-8"), "happy")
        self.assertEqual(text_buf.value.decode("utf-8"), "Online")

        # Arbiter must remain clean (chime audio tone released)
        self.assertEqual(self.lib.desk_arbiter_get_owner(DESK_RES_AUDIO_PLAYBACK), DESK_OWNER_NONE)

        # 4. TUYA_EVENT_MQTT_DISCONNECT -> idle / Cloud Offline & state TUYA_CLOUD_DISCONNECTED
        TUYA_EVENT_MQTT_DISCONNECT = 5
        rc_evt = self.lib.desk_tuya_simulate_event(TUYA_EVENT_MQTT_DISCONNECT)
        self.assertEqual(rc_evt, 0)
        self.assertEqual(self.lib.desk_tclaw_tuya_session_get_connection_state(), TUYA_CLOUD_DISCONNECTED)
        self.lib.desk_hardware_display_get_expression(preset_buf, 64, text_buf, 256)
        self.assertEqual(preset_buf.value.decode("utf-8"), "idle")
        self.assertEqual(text_buf.value.decode("utf-8"), "Cloud Offline")

    def test_tuya_direct_bridge_hooks(self) -> None:
        """Verifies desk_tclaw_on_tuya_mqtt_connected, desk_tclaw_on_tuya_mqtt_disconnected,
        and desk_tclaw_on_tuya_netcfg_started hook directly into TClaw."""
        self.lib.desk_tclaw_init()

        preset_buf = ctypes.create_string_buffer(64)
        text_buf = ctypes.create_string_buffer(256)

        # 1. netcfg hook
        rc_net = self.lib.desk_tclaw_on_tuya_netcfg_started()
        self.assertEqual(rc_net, 0)
        self.lib.desk_hardware_display_get_expression(preset_buf, 64, text_buf, 256)
        self.assertEqual(preset_buf.value.decode("utf-8"), "thinking")
        self.assertEqual(text_buf.value.decode("utf-8"), "BLE Netcfg")

        # 2. connected hook
        rc_conn = self.lib.desk_tclaw_on_tuya_mqtt_connected()
        self.assertEqual(rc_conn, 0)
        self.assertEqual(self.lib.desk_tclaw_tuya_session_get_connection_state(), TUYA_CLOUD_CONNECTED)
        self.lib.desk_hardware_display_get_expression(preset_buf, 64, text_buf, 256)
        self.assertEqual(preset_buf.value.decode("utf-8"), "happy")
        self.assertEqual(text_buf.value.decode("utf-8"), "Online")

        # 3. disconnected hook
        rc_disc = self.lib.desk_tclaw_on_tuya_mqtt_disconnected()
        self.assertEqual(rc_disc, 0)
        self.assertEqual(self.lib.desk_tclaw_tuya_session_get_connection_state(), TUYA_CLOUD_DISCONNECTED)
        self.lib.desk_hardware_display_get_expression(preset_buf, 64, text_buf, 256)
        self.assertEqual(preset_buf.value.decode("utf-8"), "idle")
        self.assertEqual(text_buf.value.decode("utf-8"), "Cloud Offline")


if __name__ == "__main__":
    unittest.main()
