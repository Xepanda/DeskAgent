"""Worker startup and diagnostics must not dereference a freed task handle."""
import unittest
from pathlib import Path

class TestBackgroundWorkerLifecycle(unittest.TestCase):
    def test_worker_uses_published_run_state_and_self_stack_snapshot(self):
        source = (Path(__file__).resolve().parents[2] / "firmware/components/desk_sdk_bridge/src/desk_sdk_bridge.c").read_text(encoding="utf-8")
        worker = source[source.index("static TaskHandle_t s_worker_task_handle"):source.index("#else\n#include <pthread.h>", source.index("static TaskHandle_t s_worker_task_handle"))]
        self.assertNotIn("while (s_worker_task_handle != NULL)", worker)
        self.assertNotIn("uxTaskGetStackHighWaterMark(s_worker_task_handle)", worker)
        self.assertIn("uxTaskGetStackHighWaterMark(NULL)", worker)
        self.assertIn("atomic_compare_exchange_strong", worker)
        self.assertIn("atomic_load(&s_worker_stack_hwm)", worker)
