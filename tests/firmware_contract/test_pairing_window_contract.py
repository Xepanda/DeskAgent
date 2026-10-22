"""A1 pairing window transaction and lifecycle contracts."""

from pathlib import Path
import ctypes
import re
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "firmware/components/desk_cli/src/desk_cli.c"
CORE_C = ROOT / "firmware/components/desk_cli/src/desk_pairing_window.c"
CORE_H = ROOT / "firmware/components/desk_cli/include/desk_pairing_window.h"
TCLAW = ROOT / "firmware/components/desk_tclaw/src/desk_tclaw_bridge.c"
TTL_US = 300_000_000


class PairingWindow(ctypes.Structure):
    _fields_ = [("expiry_deadline_us", ctypes.c_int64), ("active", ctypes.c_bool)]


RngCallback = ctypes.CFUNCTYPE(ctypes.c_uint32, ctypes.c_void_p)
CodeCallback = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p)
VoidCallback = ctypes.CFUNCTYPE(None, ctypes.c_void_p)
ClockCallback = ctypes.CFUNCTYPE(ctypes.c_int64, ctypes.c_void_p)
TimerCallback = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p)


class PairingOps(ctypes.Structure):
    _fields_ = [
        ("random_u32", RngCallback),
        ("set_pairing_code", CodeCallback),
        ("show_pairing_code", CodeCallback),
        ("clear_display", VoidCallback),
        ("monotonic_time_us", ClockCallback),
        ("start_expiry_timer", TimerCallback),
        ("stop_expiry_timer", VoidCallback),
        ("lock", VoidCallback),
        ("unlock", VoidCallback),
        ("context", ctypes.c_void_p),
    ]


class TestPairingWindowContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = shutil.which("gcc")
        if not compiler:
            raise unittest.SkipTest("native gcc is unavailable; pairing C harness is ready for host CI")
        cls.temp = tempfile.TemporaryDirectory(prefix="desk-pairing-window-")
        cls.library_path = Path(cls.temp.name) / "pairing_window.so"
        subprocess.run(
            [compiler, "-std=c11", "-Wall", "-Wextra", "-Werror", "-shared", "-fPIC",
             f"-I{CORE_H.parent}", str(CORE_C), "-o", str(cls.library_path)],
            check=True,
            capture_output=True,
            text=True,
        )
        cls.lib = ctypes.CDLL(str(cls.library_path))
        cls.lib.desk_pairing_window_init.argtypes = [ctypes.POINTER(PairingWindow)]
        cls.lib.desk_pairing_window_open.argtypes = [ctypes.POINTER(PairingWindow), ctypes.POINTER(PairingOps)]
        cls.lib.desk_pairing_window_open.restype = ctypes.c_int
        cls.lib.desk_pairing_window_close.argtypes = [ctypes.POINTER(PairingWindow), ctypes.POINTER(PairingOps)]
        cls.lib.desk_pairing_window_expire.argtypes = [ctypes.POINTER(PairingWindow), ctypes.POINTER(PairingOps)]

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "temp"):
            cls.temp.cleanup()

    def make_harness(self, random_values=(1,), *, setter_rc=0, display_rc=0, timer_rc=0, now_us=1234):
        state = {"random": list(random_values), "events": [], "now": now_us,
                 "setter_rc": setter_rc, "display_rc": display_rc, "timer_rc": timer_rc}

        @RngCallback
        def rng(_ctx):
            if state["random"]:
                value = state["random"].pop(0)
                state["last_random"] = value
                return value
            return state.get("last_random", 0)

        @CodeCallback
        def setter(code, _ctx):
            state["events"].append(("set", None if code is None else code.decode()))
            return state["setter_rc"] if code else 0

        @CodeCallback
        def display(code, _ctx):
            state["events"].append(("display", code.decode()))
            return state["display_rc"]

        @VoidCallback
        def clear(_ctx):
            state["events"].append(("clear_display", None))

        @ClockCallback
        def clock(_ctx):
            state["events"].append(("clock", state["now"]))
            return state["now"]

        @TimerCallback
        def start_timer(duration, _ctx):
            state["events"].append(("start_timer", duration))
            return state["timer_rc"]

        @VoidCallback
        def stop_timer(_ctx):
            state["events"].append(("stop_timer", None))

        @VoidCallback
        def lock(_ctx):
            state["events"].append(("lock", None))

        @VoidCallback
        def unlock(_ctx):
            state["events"].append(("unlock", None))

        callbacks = (rng, setter, display, clear, clock, start_timer, stop_timer, lock, unlock)
        ops = PairingOps(*callbacks, None)
        window = PairingWindow()
        self.lib.desk_pairing_window_init(ctypes.byref(window))
        return window, ops, state, callbacks

    def test_cli_is_the_explicit_operator_entry_and_never_prints_code(self):
        source = CLI.read_text(encoding="utf-8")
        self.assertIn('{"pairing", "Explicit SDK pairing window: pairing <open|close>", cmd_pairing}', source)
        command = source.rsplit("static void cmd_pairing(int argc, char *argv[])", 1)[1].split(
            "static void cmd_wifi", 1
        )[0]
        self.assertIn('strcmp(argv[1], "open")', command)
        self.assertIn('strcmp(argv[1], "close")', command)
        self.assertIn('strcmp(argv[1], "cancel")', command)
        self.assertIn("pairing_window_open()", command)
        self.assertNotRegex(CORE_C.read_text(encoding="utf-8"), r"(?<!sn)printf\s*\(")
        for line in command.splitlines():
            if "printf(" in line:
                self.assertNotIn("s_pairing_window", line)
                self.assertNotIn("generated_code", line)
                self.assertNotIn("s_pairing_code", line)

    def test_rejection_sampling_preserves_leading_zero_and_order_is_set_timer_display(self):
        source = CORE_C.read_text(encoding="utf-8")
        self.assertIn("volatile unsigned char *cursor", source)
        self.assertIn("(UINT32_MAX / DESK_PAIRING_CODE_SPACE) * DESK_PAIRING_CODE_SPACE", source)
        self.assertIn('"%06lu"', source)
        open_body = source.split("int desk_pairing_window_open(", 1)[1].split("void desk_pairing_window_close", 1)[0]
        self.assertLess(open_body.index("ops->set_pairing_code(generated_code"),
                        open_body.index("ops->start_expiry_timer"))
        self.assertLess(open_body.index("ops->start_expiry_timer"), open_body.index("ops->show_pairing_code"))
        self.assertIn("DESK_PAIRING_WINDOW_TTL_US (300ULL * 1000ULL * 1000ULL)", CORE_H.read_text(encoding="utf-8"))

        window, ops, state, _callbacks = self.make_harness(random_values=(7,))
        self.assertEqual(self.lib.desk_pairing_window_open(ctypes.byref(window), ctypes.byref(ops)), 0)
        self.assertTrue(window.active)
        self.assertEqual([item for item in state["events"] if item[0] in ("set", "start_timer", "display")],
                         [("set", None), ("set", "000007"), ("start_timer", TTL_US), ("display", "000007")])
        self.assertEqual(window.expiry_deadline_us, state["now"] + TTL_US)

    def test_rejected_random_samples_are_bounded_and_leave_code_unconfigured(self):
        # UINT32_MAX is outside the accepted range; a broken RNG cannot hang the CLI forever.
        window, ops, state, _callbacks = self.make_harness(random_values=(0xFFFFFFFF,))
        self.assertEqual(self.lib.desk_pairing_window_open(ctypes.byref(window), ctypes.byref(ops)), -1)
        self.assertFalse(window.active)
        self.assertFalse(any(event == "display" for event, _ in state["events"]))
        self.assertFalse(any(event == "set" and value for event, value in state["events"]))

        # The rejected tail sample is discarded, and the next in-range value is used.
        window, ops, state, _callbacks = self.make_harness(random_values=(0xFFFFFFFF, 1_000_005))
        self.assertEqual(self.lib.desk_pairing_window_open(ctypes.byref(window), ctypes.byref(ops)), 0)
        self.assertIn(("display", "000005"), state["events"])

    def test_reopening_rotates_the_code_and_invalid_monotonic_time_fails_closed(self):
        window, ops, state, _callbacks = self.make_harness(random_values=(7, 8))
        self.assertEqual(self.lib.desk_pairing_window_open(ctypes.byref(window), ctypes.byref(ops)), 0)
        self.assertIn(("display", "000007"), state["events"])
        self.assertEqual(self.lib.desk_pairing_window_open(ctypes.byref(window), ctypes.byref(ops)), 0)
        self.assertIn(("display", "000008"), state["events"])
        self.assertEqual(state["events"].count(("set", "000007")), 1)
        self.assertEqual(state["events"].count(("set", "000008")), 1)

        window, ops, state, _callbacks = self.make_harness(random_values=(9,), now_us=-1)
        self.assertEqual(self.lib.desk_pairing_window_open(ctypes.byref(window), ctypes.byref(ops)), -1)
        self.assertFalse(window.active)
        self.assertFalse(any(event == "display" for event, _ in state["events"]))

    def test_setter_timer_and_display_failures_roll_back_code_and_ui(self):
        for failure in ("setter_rc", "timer_rc", "display_rc"):
            with self.subTest(failure=failure):
                window, ops, state, _callbacks = self.make_harness(random_values=(123,), **{failure: -1})
                self.assertEqual(self.lib.desk_pairing_window_open(ctypes.byref(window), ctypes.byref(ops)), -1)
                self.assertFalse(window.active)
                self.assertEqual(window.expiry_deadline_us, 0)
                self.assertIn(("set", None), state["events"])
                self.assertIn(("clear_display", None), state["events"])
                if failure == "setter_rc":
                    self.assertFalse(any(event == "display" for event, _ in state["events"]))

    def test_expiry_uses_monotonic_deadline_and_close_clears_everything(self):
        window, ops, state, _callbacks = self.make_harness(random_values=(42,))
        self.assertEqual(self.lib.desk_pairing_window_open(ctypes.byref(window), ctypes.byref(ops)), 0)
        clears_before_deadline = state["events"].count(("set", None))
        state["now"] += TTL_US - 1
        self.lib.desk_pairing_window_expire(ctypes.byref(window), ctypes.byref(ops))
        self.assertTrue(window.active)
        self.assertEqual(state["events"].count(("set", None)), clears_before_deadline)
        state["now"] += 1
        self.lib.desk_pairing_window_expire(ctypes.byref(window), ctypes.byref(ops))
        self.assertFalse(window.active)
        self.assertIn(("set", None), state["events"])
        self.assertIn(("clear_display", None), state["events"])

        window, ops, state, _callbacks = self.make_harness(random_values=(99,))
        self.assertEqual(self.lib.desk_pairing_window_open(ctypes.byref(window), ctypes.byref(ops)), 0)
        self.lib.desk_pairing_window_close(ctypes.byref(window), ctypes.byref(ops))
        self.assertFalse(window.active)
        self.assertIn(("set", None), state["events"])

    def test_pairing_overlay_lifecycle_seams_and_voice_input_mapping_are_preserved(self):
        source = TCLAW.read_text(encoding="utf-8")
        header = (ROOT / "firmware/components/desk_tclaw/include/desk_tclaw_bridge.h").read_text(encoding="utf-8")
        for name in ("desk_tclaw_pairing_ui_on_pairing_consumed", "desk_tclaw_pairing_ui_on_sdk_session_reset"):
            self.assertIn(f"void {name}(void);", header)
            body = source.split(f"void {name}(void)", 1)[1].split("}", 1)[0]
            self.assertIn("desk_tclaw_pairing_ui_clear();", body)
        render = source.split("static int watcher_ui_render", 1)[1].split(
            "static int desk_tclaw_update_screen", 1
        )[0]
        self.assertIn('snprintf(status, sizeof(status), "PAIR %s"', render)
        input_cb = source.split("static void watcher_ui_input_event", 1)[1].split(
            "static int desk_tclaw_update_ui_screen", 1
        )[0]
        self.assertIn("AT_KEY_TALK", input_cb)
        self.assertIn("AT_LONG_KEY_TALK", input_cb)


if __name__ == "__main__":
    unittest.main()
