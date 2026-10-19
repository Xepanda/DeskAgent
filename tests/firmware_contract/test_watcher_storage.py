import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class TestWatcherStorage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.folder = Path(cls.temp.name)
        (cls.folder / 'esp_partition.h').write_text('''#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
typedef int esp_err_t;
typedef unsigned char esp_partition_type_t;
typedef unsigned char esp_partition_subtype_t;
typedef struct { unsigned size; } esp_flash_t;
extern esp_flash_t *esp_flash_default_chip;
typedef struct { esp_flash_t *flash_chip; unsigned address,size; char label[17]; } esp_partition_t;
typedef struct iterator *esp_partition_iterator_t;
#define ESP_OK 0
#define ESP_ERR_INVALID_STATE 1
#define ESP_ERR_INVALID_SIZE 2
#define ESP_ERR_NO_MEM 3
#define ESP_PARTITION_TYPE_ANY 255
#define ESP_PARTITION_TYPE_DATA 1
#define ESP_PARTITION_SUBTYPE_ANY 255
const esp_partition_t *esp_partition_find_first(esp_partition_type_t,esp_partition_subtype_t,const char*);
esp_partition_iterator_t esp_partition_find(esp_partition_type_t,esp_partition_subtype_t,const char*);
const esp_partition_t *esp_partition_get(esp_partition_iterator_t);
esp_partition_iterator_t esp_partition_next(esp_partition_iterator_t);
void esp_partition_iterator_release(esp_partition_iterator_t);
esp_err_t esp_partition_register_external(esp_flash_t*,size_t,size_t,const char*,esp_partition_type_t,esp_partition_subtype_t,const esp_partition_t**);
esp_err_t esp_partition_deregister_external(const esp_partition_t*);
esp_err_t esp_partition_read(const esp_partition_t*,size_t,void*,size_t);
esp_err_t esp_partition_write(const esp_partition_t*,size_t,const void*,size_t);
esp_err_t esp_flash_get_size(esp_flash_t*,uint32_t*);
''')
        (cls.folder / 'esp_flash.h').write_text('#include "esp_partition.h"\n')
        (cls.folder / 'esp_log.h').write_text('#define ESP_LOGI(...) ((void)0)\n#define ESP_LOGE(...) ((void)0)\n')
        cls.exe = cls.folder / 'storage_test'
        result = subprocess.run([
            'gcc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-DESP_PLATFORM',
            f'-I{cls.folder}', f'-I{ROOT / "firmware/main/include"}',
            str(ROOT / 'firmware/main/src/watcher_storage.c'),
            str(ROOT / 'tests/firmware_contract/watcher_storage_test.c'), '-o', str(cls.exe)
        ], capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(result.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def run_case(self, name):
        result = subprocess.run([str(self.exe), name], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_erased_tail_and_reboot_preserve_registered_data(self):
        self.run_case('success')

    def test_unknown_tail_data_is_not_formatted(self):
        self.run_case('unknown')

    def test_partial_metadata_is_not_repaired(self):
        self.run_case('metadata')

    def test_partition_overlap_is_rejected_without_write(self):
        self.run_case('overlap')

    def test_wrong_flash_capacity_is_rejected(self):
        self.run_case('capacity')

    def test_wrong_factory_layout_is_rejected(self):
        self.run_case('layout')

    def test_read_error_deregisters_virtual_partitions(self):
        self.run_case('read_error')

    def test_virtual_store_on_another_flash_chip_is_rejected(self):
        self.run_case('foreign')

    def test_unmarked_existing_virtual_store_is_not_trusted(self):
        self.run_case('unmarked')


if __name__ == '__main__':
    unittest.main()
