"""Worker startup and diagnostics must not dereference a freed task handle."""
import unittest
import shutil
import subprocess
import tempfile
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
        self.assertIn('"desk_poll_worker", 24576', worker)

    def test_esp_writer_uses_scoped_psram_stack_configuration(self):
        source = (Path(__file__).resolve().parents[2] / "firmware/components/desk_sdk_bridge/src/desk_sdk_websocket.c").read_text(encoding="utf-8")
        start = source.index("static int ws_writer_start(void)")
        writer = source[start:source.index("static int ws_enqueue_frame_with_camera_ack", start)]
        self.assertIn("MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT", writer)
        configure = writer.index("esp_pthread_set_cfg(&writer_cfg)")
        create = writer.index("pthread_create(")
        restore = writer.index("esp_pthread_set_cfg(&previous)")
        self.assertLess(configure, create)
        self.assertLess(create, restore)
        self.assertIn("create_rc != 0", writer)

    @unittest.skipUnless(shutil.which("gcc"), "native gcc required")
    def test_preemptive_create_runs_worker_before_handle_is_published(self):
        source = (Path(__file__).resolve().parents[2] / "firmware/components/desk_sdk_bridge/src/desk_sdk_bridge.c").read_text(encoding="utf-8")
        start = source.index("static TaskHandle_t s_worker_task_handle")
        worker = source[start:source.index("#else\n#include <pthread.h>", start)]
        harness = r"""
#include <stdatomic.h>
#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#include <assert.h>
typedef void *TaskHandle_t;
typedef int BaseType_t;
#define pdPASS 1
#define MALLOC_CAP_SPIRAM 1
#define MALLOC_CAP_8BIT 2
#define pdMS_TO_TICKS(n) (n)
static int polls, samples, deletes, fail_create;
void desk_firmware_stop_background_task(void);
static void desk_firmware_poll(int timeout) { (void)timeout; ++polls; desk_firmware_stop_background_task(); }
static void vTaskDelay(int ticks) { (void)ticks; }
static void vTaskDeleteWithCaps(void *task) { assert(task == NULL); ++deletes; }
static unsigned uxTaskGetStackHighWaterMark(TaskHandle_t task) { assert(task == NULL); ++samples; return 1024; }
static BaseType_t xTaskCreateWithCaps(void (*fn)(void *), const char *name, int stack,
                                    void *arg, int priority, TaskHandle_t *handle, int caps) {
    (void)name; (void)stack; (void)priority; (void)caps;
    if (fail_create) return 0;
    fn(arg); /* Scheduler preemption before output handle publication. */
    *handle = (void *)0xdead;
    return pdPASS;
}
""" + worker + r"""
int main(void) {
    assert(desk_firmware_start_background_task() == 0);
    assert(polls == 1 && samples == 1 && deletes == 1);
    assert(!desk_firmware_is_background_task_running());
    assert(desk_firmware_background_task_stack_hwm() == 0);
    assert(samples == 1); /* No external diagnostic scan of the stale handle. */
    fail_create = 1;
    assert(desk_firmware_start_background_task() == -1);
    assert(!desk_firmware_is_background_task_running());
    fail_create = 0;
    assert(desk_firmware_start_background_task() == 0);
    assert(polls == 2 && deletes == 2);
    return 0;
}
"""
        with tempfile.TemporaryDirectory() as directory:
            src = Path(directory) / "worker.c"
            exe = Path(directory) / "worker"
            src.write_text(harness, encoding="utf-8")
            subprocess.run(["gcc", "-std=c11", "-Wall", "-Wextra", "-Werror", str(src), "-o", str(exe)], check=True, capture_output=True)
            subprocess.run([str(exe)], check=True, capture_output=True)
