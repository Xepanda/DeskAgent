# CMakeLists.txt for TuyaOpen / ESP-IDF component desk_tclaw
idf_component_register(
    SRCS
        "src/desk_tclaw_bridge.c"
    INCLUDE_DIRS
        "include"
    REQUIRES
        desk_hardware
)
