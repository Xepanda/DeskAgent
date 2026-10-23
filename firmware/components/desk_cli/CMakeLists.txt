# DeskAgent diagnostic commands registered in the SDK's TAL CLI.
idf_component_register(
    SRCS "src/desk_cli.c" "src/desk_pairing_window.c"
    INCLUDE_DIRS "include" "../../main/include"
    REQUIRES tal_cli driver esp_timer esp_wifi esp_netif esp_app_format desk_hardware desk_tclaw desk_sdk_bridge
)
