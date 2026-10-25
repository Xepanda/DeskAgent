# CMakeLists.txt for TuyaOpen / ESP-IDF component desk_hardware
idf_component_register(
    SRCS
        "src/desk_hardware.c"
        "src/desk_arbiter.c"
        "src/desk_audio_resampler.c"
        "src/desk_audio_stream.c"
        "src/desk_watcher_board.c"
        "src/desk_watcher_audio.c"
        "src/desk_watcher_motion.c"
        "src/desk_watcher_camera.c"
        "src/desk_watcher_log_source.c"
        "src/desk_watcher_input.c"
        "src/desk_notification.c"
    INCLUDE_DIRS
        "include"
    REQUIRES
        driver
        esp_timer
        esp_lcd
        desk_watcher_deps
        esp_codec_dev
)

target_compile_definitions(${COMPONENT_LIB} PUBLIC DESK_BOARD_SENSECAP_WATCHER=1)
