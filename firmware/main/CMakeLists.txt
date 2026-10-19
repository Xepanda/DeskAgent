# CMakeLists.txt for ESP-IDF main component
idf_component_register(
    SRCS "src/app_main.c" "src/provisioning_state.c" "src/tclaw_runtime_adapter.c" "src/watcher_storage.c"
    INCLUDE_DIRS "include"
    REQUIRES desk_hardware desk_tclaw desk_sdk_bridge desk_cli desk_watcher_deps tal_cli esp_netif esp_event esp_wifi nvs_flash
)
