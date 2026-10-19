# DeskAgent diagnostic commands registered in the SDK's TAL CLI.
idf_component_register(
    SRCS "src/desk_cli.c"
    INCLUDE_DIRS "include" "../../main/include"
    REQUIRES tal_cli driver esp_timer esp_wifi esp_netif desk_hardware desk_tclaw desk_sdk_bridge
)
