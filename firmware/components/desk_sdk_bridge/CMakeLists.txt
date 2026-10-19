# CMakeLists.txt for TuyaOpen / ESP-IDF component desk_sdk_bridge
idf_component_register(
    SRCS
        "src/desk_sdk_bridge.c"
    INCLUDE_DIRS
        "include"
    REQUIRES
        desk_hardware
        lwip
)
