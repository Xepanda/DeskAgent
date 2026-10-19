#include "watcher_storage.h"
#ifdef ESP_PLATFORM
#include "esp_flash.h"
#include "esp_partition.h"
#include "esp_log.h"
#if defined(__has_include)
#if __has_include("esp_memory_utils.h")
#include "esp_memory_utils.h"
#define HAVE_ESP_MEMORY_UTILS 1
#endif
#endif
#include <string.h>

#define META_OFFSET 0x1fbf000u
#define STORE_OFFSET 0x1fc0000u
#define STORE_SIZE 0x40000u
#define META_SIZE 0x1000u

typedef struct {
    char magic[16];
    uint32_t offset, size, version, check;
} storage_marker_t;

static const storage_marker_t expected = {
    .magic="DESK_TUYA_V1", .offset=STORE_OFFSET, .size=STORE_SIZE, .version=1,
    .check=~(STORE_OFFSET ^ STORE_SIZE ^ 1u),
};

static bool erased(const void *data, size_t length)
{
    const unsigned char *bytes=data;
    for (size_t i=0; i<length; ++i) if (bytes[i] != 0xff) return false;
    return true;
}

static bool expected_partition(const char *name, uint32_t address, uint32_t size)
{
    const esp_partition_t *p=esp_partition_find_first(ESP_PARTITION_TYPE_ANY, ESP_PARTITION_SUBTYPE_ANY, name);
    return p && p->flash_chip == esp_flash_default_chip && p->address == address && p->size == size;
}

int desk_watcher_storage_init(void)
{
#if defined(HAVE_ESP_MEMORY_UTILS)
    storage_marker_t stack_probe;
    if (!esp_ptr_internal(&stack_probe)) {
        ESP_LOGE("watcher_storage", "caller stack must reside in internal SRAM for cache-disabled flash ops");
        return ESP_ERR_INVALID_STATE;
    }
#endif
    const esp_partition_t *existing=esp_partition_find_first(ESP_PARTITION_TYPE_DATA, ESP_PARTITION_SUBTYPE_ANY, "tuya");
    if (existing) {
        const esp_partition_t *meta=esp_partition_find_first(ESP_PARTITION_TYPE_DATA, ESP_PARTITION_SUBTYPE_ANY, "deskmeta");
        storage_marker_t saved;
        if (existing->flash_chip != esp_flash_default_chip || existing->address != STORE_OFFSET ||
            existing->size != STORE_SIZE || !meta || meta->flash_chip != esp_flash_default_chip ||
            meta->address != META_OFFSET || meta->size != META_SIZE) return ESP_ERR_INVALID_STATE;
        esp_err_t read_rc=esp_partition_read(meta, 0, &saved, sizeof(saved));
        return read_rc == ESP_OK && memcmp(&saved, &expected, sizeof(saved)) == 0 ? ESP_OK : ESP_ERR_INVALID_STATE;
    }
    uint32_t flash_size=0;
    if (esp_flash_get_size(esp_flash_default_chip, &flash_size) != ESP_OK || flash_size != 0x2000000u ||
        !expected_partition("ota_0", 0x20000, 0x600000) ||
        !expected_partition("agentcfg", 0x12000, 0xe000) ||
        !expected_partition("storage", 0xd20000, 0x400000)) return ESP_ERR_INVALID_STATE;

    /* Any overlapping registered partition blocks the reservation, including
     * future factory layouts. Never infer that an unrecognized region is free. */
    esp_partition_iterator_t it=esp_partition_find(ESP_PARTITION_TYPE_ANY, ESP_PARTITION_SUBTYPE_ANY, NULL);
    while (it) {
        const esp_partition_t *p=esp_partition_get(it);
        if (p->flash_chip == esp_flash_default_chip &&
            (uint64_t)p->address < (uint64_t)STORE_OFFSET + STORE_SIZE &&
            (uint64_t)p->address + p->size > META_OFFSET) {
            esp_partition_iterator_release(it);
            return ESP_ERR_INVALID_STATE;
        }
        it=esp_partition_next(it);
    }

    const esp_partition_t *metadata=NULL, *storage=NULL;
    esp_err_t rc=esp_partition_register_external(esp_flash_default_chip, META_OFFSET, META_SIZE,
        "deskmeta", ESP_PARTITION_TYPE_DATA, ESP_PARTITION_SUBTYPE_ANY, &metadata);
    if (rc != ESP_OK) return rc;
    rc=esp_partition_register_external(esp_flash_default_chip, STORE_OFFSET, STORE_SIZE,
        "tuya", ESP_PARTITION_TYPE_DATA, ESP_PARTITION_SUBTYPE_ANY, &storage);
    if (rc != ESP_OK) { esp_partition_deregister_external(metadata); return rc; }

    storage_marker_t marker;
    rc=esp_partition_read(metadata, 0, &marker, sizeof(marker));
    if (rc != ESP_OK) goto fail;
    if (memcmp(&marker, &expected, sizeof(marker)) == 0) {
        ESP_LOGI("watcher_storage", "existing project Tuya store registered (factory table preserved)");
        return ESP_OK;
    }
    /* A new store is allowed only if EVERY byte in both reserved ranges is
     * erased. Unknown data or a partial marker is never formatted or repaired. */
    unsigned char chunk[256];
    for (uint32_t offset=0; offset<META_SIZE+STORE_SIZE; offset+=sizeof(chunk)) {
        const esp_partition_t *p=offset < META_SIZE ? metadata : storage;
        uint32_t relative=offset < META_SIZE ? offset : offset-META_SIZE;
        rc=esp_partition_read(p, relative, chunk, sizeof(chunk));
        if (rc != ESP_OK) goto fail;
        if (!erased(chunk, sizeof(chunk))) { rc=ESP_ERR_INVALID_STATE; goto fail; }
    }
    storage_marker_t marker_to_write = expected;
    rc=esp_partition_write(metadata, 0, &marker_to_write, sizeof(marker_to_write));
    if (rc != ESP_OK) goto fail;
    ESP_LOGI("watcher_storage", "new Tuya store reserved in verified erased 32MB flash tail");
    return ESP_OK;
fail:
    esp_partition_deregister_external(storage);
    esp_partition_deregister_external(metadata);
    ESP_LOGE("watcher_storage", "refusing unknown or unavailable persistent store rc=%d", rc);
    return rc;
}
#else
int desk_watcher_storage_init(void) { return 0; }
#endif
