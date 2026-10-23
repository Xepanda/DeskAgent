"""Regression coverage for cached app-task stack high-water sampling."""

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
APP_MAIN_C = ROOT / "firmware/main/src/app_main.c"
CLI_C = ROOT / "firmware/components/desk_cli/src/desk_cli.c"
HWM_CACHE_H = ROOT / "firmware/main/include/desk_app_stack_hwm_cache.h"


class TestDeskAppTaskStackHwm(unittest.TestCase):
    def test_getter_reads_cache_and_only_worker_samples_its_own_live_stack(self):
        source = APP_MAIN_C.read_text(encoding="utf-8")
        getter = source.split("uint32_t desk_app_task_stack_hwm(void)", 1)[1].split("\n}", 1)[0]
        worker = source.split("static void desk_app_task(void *pvParameters)", 1)[1].split("\n}", 1)[0]
        loop = source.split("while (1)", 1)[1].split("\n    }", 1)[0]
        create = source.split("int app_main(void)", 1)[1].split("#else", 1)[0]
        cli = CLI_C.read_text(encoding="utf-8")

        self.assertIn("desk_app_stack_hwm_cache_read", getter)
        self.assertNotIn("uxTaskGetStackHighWaterMark", getter)
        self.assertNotIn("s_app_task_handle", source)
        self.assertIn("uxTaskGetStackHighWaterMark(NULL)", source)
        self.assertIn("desk_app_stack_hwm_cache_publish", source)
        self.assertIn("desk_app_task_stack_hwm_publish_self", loop)
        self.assertIn("desk_app_task_stack_hwm_publish_self", worker)
        self.assertIn("NULL, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT", create)
        self.assertIn("cached self-sample", cli)
        self.assertIn("UNKNOWN (no cached self-sample", cli)

    def test_c_harness_getter_never_queries_tcb_after_worker_exit(self):
        compiler = shutil.which("gcc")
        if compiler is None:
            self.skipTest("native gcc is unavailable; C harness is ready for host CI")

        harness = r'''#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "desk_app_stack_hwm_cache.h"

static desk_app_stack_hwm_cache_t cache = DESK_APP_STACK_HWM_CACHE_INITIALIZER;
static bool app_task_alive = true;
static unsigned tcb_queries;

static uint32_t sample_current_task(void *handle) {
    assert(handle == NULL); /* production must query only the calling task */
    assert(app_task_alive);
    ++tcb_queries;
    return 256U;
}

static void publish_app_task_sample(void) {
    desk_app_stack_hwm_cache_publish(&cache, sample_current_task(NULL) * sizeof(uint32_t));
}

static uint32_t getter(void) {
    uint32_t bytes = 0;
    (void)desk_app_stack_hwm_cache_read(&cache, &bytes);
    return bytes;
}

int main(void) {
    uint32_t bytes = 123;
    assert(!desk_app_stack_hwm_cache_read(&cache, &bytes));
    assert(bytes == 0); /* no task yet or synchronous creation fallback */
    publish_app_task_sample();
    assert(getter() == 1024U);
    assert(tcb_queries == 1U);
    app_task_alive = false; /* task subsequently calls vTaskDelete(NULL) */
    assert(getter() == 1024U); /* the cache remains readable after TCB deletion */
    assert(tcb_queries == 1U); /* getter did not query a stale TaskHandle_t */
    return 0;
}
'''
        with tempfile.TemporaryDirectory(prefix="desk-app-hwm-") as temp_dir:
            temp = Path(temp_dir)
            stub = temp / "freertos"
            stub.mkdir()
            (stub / "FreeRTOS.h").write_text(
                "#ifndef FREERTOS_H\n#define FREERTOS_H\n"
                "#include <assert.h>\ntypedef struct { int held; } portMUX_TYPE;\n"
                "#define portMUX_INITIALIZER_UNLOCKED {0}\n"
                "static inline void portENTER_CRITICAL(portMUX_TYPE *m) { assert(!m->held); m->held=1; }\n"
                "static inline void portEXIT_CRITICAL(portMUX_TYPE *m) { assert(m->held); m->held=0; }\n"
                "#endif\n",
                encoding="utf-8",
            )
            source = temp / "app_hwm_cache_test.c"
            executable = temp / "app_hwm_cache_test"
            source.write_text(harness, encoding="utf-8")
            built = subprocess.run(
                [compiler, "-std=c11", "-Wall", "-Wextra", "-Werror",
                 f"-I{temp}", f"-I{HWM_CACHE_H.parent}", str(source), "-o", str(executable)],
                capture_output=True, text=True,
            )
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
            run = subprocess.run([str(executable)], capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)


if __name__ == "__main__":
    unittest.main()
