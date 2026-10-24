cmake_minimum_required(VERSION 3.16)

if(DEFINED EXAMPLE_LIB)
    # TuyaOpen CLI (tos.py) mode: register EXAMPLE_LIB target
    add_library(${EXAMPLE_LIB})

    file(GLOB MAIN_SRCS "${CMAKE_CURRENT_LIST_DIR}/main/src/*.c")
    file(GLOB_RECURSE COMP_SRCS "${CMAKE_CURRENT_LIST_DIR}/components/*/src/*.c")

    target_sources(${EXAMPLE_LIB}
        PRIVATE
            ${MAIN_SRCS}
            ${COMP_SRCS}
    )

    # Compile the pinned TClaw framework sources into the real TuyaOpen app
    # target. The upstream tuya_app_main.c is intentionally omitted; firmware
    # main owns the single app entry and A1 wires the native startup sequence.
    set(TCLAW_SOURCE_ROOT "${CMAKE_CURRENT_LIST_DIR}/third_party/tclaw")
    file(GLOB TCLAW_APP_SRCS CONFIGURE_DEPENDS
        "${TCLAW_SOURCE_ROOT}/src/*.c"
        "${TCLAW_SOURCE_ROOT}/agent/*.c"
        "${TCLAW_SOURCE_ROOT}/tools/*.c"
        "${TCLAW_SOURCE_ROOT}/cron_service/*.c"
        "${TCLAW_SOURCE_ROOT}/heartbeat/*.c"
        "${TCLAW_SOURCE_ROOT}/memory/*.c"
        "${TCLAW_SOURCE_ROOT}/gateway/*.c"
        "${TCLAW_SOURCE_ROOT}/skills/*.c"
    )
    list(FILTER TCLAW_APP_SRCS EXCLUDE REGEX "/src/tuya_app_main\\.c$")
    target_sources(${EXAMPLE_LIB} PRIVATE ${TCLAW_APP_SRCS})
    target_include_directories(${EXAMPLE_LIB} BEFORE PRIVATE
        "${TCLAW_SOURCE_ROOT}"
        "${TCLAW_SOURCE_ROOT}/include"
        "${TCLAW_SOURCE_ROOT}/agent"
        "${TCLAW_SOURCE_ROOT}/tools"
        "${TCLAW_SOURCE_ROOT}/cron_service"
        "${TCLAW_SOURCE_ROOT}/heartbeat"
        "${TCLAW_SOURCE_ROOT}/memory"
        "${TCLAW_SOURCE_ROOT}/gateway"
        "${TCLAW_SOURCE_ROOT}/skills"
        "${TCLAW_SOURCE_ROOT}/overlay/ai_components/ai_agent/include"
        "${TCLAW_SOURCE_ROOT}/overlay/ai_components/ai_mcp/include"
    )

    # Preserve TClaw's upstream IM and Lua build wiring without copying its
    # second app entry. Runtime initialization remains controlled by A1.
    add_subdirectory("${TCLAW_SOURCE_ROOT}/IM" "${CMAKE_CURRENT_BINARY_DIR}/tclaw_IM")
    add_subdirectory("${TCLAW_SOURCE_ROOT}/components/lua" "${CMAKE_CURRENT_BINARY_DIR}/tclaw_lua")

    # The upstream TClaw overlays are required production sources. Fail the
    # configure if this TuyaOpen SDK lacks the expected AI targets or source
    # translation units; do not silently build a stub or unpatched fallback.
    function(_desk_tclaw_replace_source target rel_path)
        if(NOT TARGET ${target})
            message(FATAL_ERROR "Pinned TClaw requires TuyaOpen target '${target}'")
        endif()
        set(_overlay_file "${TCLAW_SOURCE_ROOT}/overlay/ai_components/${rel_path}")
        if(NOT EXISTS "${_overlay_file}")
            message(FATAL_ERROR "Pinned TClaw overlay source is missing: ${_overlay_file}")
        endif()
        get_target_property(_old_sources ${target} SOURCES)
        get_filename_component(_replace_name "${rel_path}" NAME)
        set(_new_sources)
        set(_found_source FALSE)
        foreach(_src ${_old_sources})
            get_filename_component(_src_name "${_src}" NAME)
            if(_src_name STREQUAL "${_replace_name}")
                set(_found_source TRUE)
            else()
                list(APPEND _new_sources "${_src}")
            endif()
        endforeach()
        if(NOT _found_source)
            message(FATAL_ERROR "Pinned TClaw expected ${_replace_name} in target '${target}'")
        endif()
        list(APPEND _new_sources "${_overlay_file}")
        set_target_properties(${target} PROPERTIES SOURCES "${_new_sources}")
    endfunction()

    _desk_tclaw_replace_source(ai_mcp "ai_mcp/src/ai_mcp_server.c")
    _desk_tclaw_replace_source(ai_agent "ai_agent/src/ai_agent.c")
    if(NOT TARGET ai_audio)
        message(FATAL_ERROR "Watcher profile requires TuyaOpen target 'ai_audio'")
    endif()
    foreach(_tclaw_overlay_target ai_mcp ai_agent)
        target_include_directories(${_tclaw_overlay_target} BEFORE PUBLIC
            "${TCLAW_SOURCE_ROOT}/overlay/ai_components/${_tclaw_overlay_target}/include"
        )
    endforeach()
    target_compile_definitions(ai_agent PUBLIC AUDIO_CODEC_NAME=\"watcher_audio\")
    target_compile_definitions(ai_audio PUBLIC AUDIO_CODEC_NAME=\"watcher_audio\")

    if(NOT DEFINED IDF_PATH)
        if(DEFINED ENV{IDF_PATH})
            set(IDF_PATH $ENV{IDF_PATH})
        elseif(DEFINED TOP_SOURCE_DIR)
            set(IDF_PATH "${TOP_SOURCE_DIR}/platform/ESP32/esp-idf")
        endif()
    endif()

    target_compile_definitions(${EXAMPLE_LIB}
        PRIVATE
            ESP_PLATFORM
            app_main=tuya_app_main
            DESK_BOARD_SENSECAP_WATCHER=1
            AUDIO_CODEC_NAME=\"watcher_audio\"
    )

    target_include_directories(${EXAMPLE_LIB}
        PUBLIC
            ${CMAKE_CURRENT_LIST_DIR}/main/include
            ${CMAKE_CURRENT_LIST_DIR}/components/desk_hardware/include
            ${CMAKE_CURRENT_LIST_DIR}/components/desk_tclaw/include
            ${CMAKE_CURRENT_LIST_DIR}/components/desk_sdk_bridge/include
            ${CMAKE_CURRENT_LIST_DIR}/components/desk_cli/include
            ${TOP_SOURCE_DIR}/src/tal_cli/include
            ${CMAKE_CURRENT_LIST_DIR}/third_party/esp_lcd_spd2010/include
            ${IDF_PATH}/components/esp_lcd/include
            ${IDF_PATH}/components/esp_common/include
            ${IDF_PATH}/components/esp_hw_support/include
            ${IDF_PATH}/components/esp_system/include
            ${IDF_PATH}/components/esp_app_format/include
            ${IDF_PATH}/components/pthread/include
            ${IDF_PATH}/components/esp_rom/include
            ${IDF_PATH}/components/esp_rom/include/esp32s3
            ${IDF_PATH}/components/esp_rom/esp32s3/include
            ${IDF_PATH}/components/soc/include
            ${IDF_PATH}/components/soc/esp32s3/include
            ${IDF_PATH}/components/soc/esp32s3/register
            ${IDF_PATH}/components/hal/include
            ${IDF_PATH}/components/hal/esp32s3/include
            ${IDF_PATH}/components/esp_hw_support/port/esp32s3
            ${IDF_PATH}/components/freertos/config/include
            ${IDF_PATH}/components/freertos/config/include/freertos
            ${IDF_PATH}/components/freertos/config/xtensa/include
            ${IDF_PATH}/components/freertos/FreeRTOS-Kernel/include
            ${IDF_PATH}/components/freertos/FreeRTOS-Kernel/include/freertos
            ${IDF_PATH}/components/freertos/FreeRTOS-Kernel/portable/xtensa/include
            ${IDF_PATH}/components/freertos/FreeRTOS-Kernel/portable/xtensa/include/freertos
            ${IDF_PATH}/components/freertos/esp_additions/include
            ${IDF_PATH}/components/freertos/esp_additions/include/freertos
            ${IDF_PATH}/components/esp_driver_uart/include
            ${IDF_PATH}/components/esp_driver_gpio/include
            ${IDF_PATH}/components/driver/i2c/include
            ${IDF_PATH}/components/esp_driver_i2c/include
            ${IDF_PATH}/components/esp_driver_ledc/include
            ${IDF_PATH}/components/esp_driver_spi/include
            ${IDF_PATH}/components/esp_driver_i2s/include
            ${IDF_PATH}/../tuya_open_sdk/managed_components/espressif__esp_codec_dev/include
            ${IDF_PATH}/../tuya_open_sdk/managed_components/espressif__esp_codec_dev/interface
            ${IDF_PATH}/../tuya_open_sdk/managed_components/espressif__esp_codec_dev/device/include
            ${IDF_PATH}/../tuya_open_sdk/managed_components/espressif__esp-sr/include/esp32s3
            ${IDF_PATH}/components/driver/deprecated
            ${IDF_PATH}/components/esp_timer/include
            ${IDF_PATH}/components/esp_event/include
            ${IDF_PATH}/components/esp_netif/include
            ${IDF_PATH}/components/esp_wifi/include
            ${IDF_PATH}/components/esp_partition/include
            ${IDF_PATH}/components/spi_flash/include
            ${IDF_PATH}/components/nvs_flash/include
            ${IDF_PATH}/components/log/include
            ${IDF_PATH}/components/heap/include
            ${IDF_PATH}/components/lwip/include
            ${IDF_PATH}/components/lwip/port/include
            ${IDF_PATH}/components/lwip/port/freertos/include
            ${IDF_PATH}/components/lwip/lwip/src/include
            ${IDF_PATH}/components/lwip/port/esp32xx/include
            ${IDF_PATH}/components/lwip/port/esp32xx/include/sys
            ${IDF_PATH}/components/vfs/include
            ${IDF_PATH}/components/xtensa/include
            ${IDF_PATH}/components/xtensa/esp32s3/include
            ${IDF_PATH}/components/newlib/platform_include
            ${IDF_PATH}/../tuya_open_sdk/build/config
    )

    # Trim PLATFORM_NEED_HDIR to essential paths and space-separate to prevent Windows 32KB CreateProcess command line overflow in Phase 2 ESP-IDF build
    set(PLATFORM_NEED_HDIR_LIST
        "${TOP_BINARY_DIR}/include"
        "${TOP_SOURCE_DIR}/src/common/include"
        "${TOP_SOURCE_DIR}/src/common/utilities"
        "${TOP_SOURCE_DIR}/src/tal_system/include"
        "${TOP_SOURCE_DIR}/src/tal_network/include"
        "${TOP_SOURCE_DIR}/src/tal_wifi/include"
        "${TOP_SOURCE_DIR}/src/tal_driver/uart"
        "${TOP_SOURCE_DIR}/src/tal_cli/include"
        "${TOP_SOURCE_DIR}/src/tal_bluetooth/include"
        "${TOP_SOURCE_DIR}/src/tal_kv/include"
        "${TOP_SOURCE_DIR}/src/tal_security/include"
    )
    string(REPLACE ";" " " PLATFORM_NEED_HDIR "${PLATFORM_NEED_HDIR_LIST}")
elseif(DEFINED ENV{IDF_PATH})
    include($ENV{IDF_PATH}/tools/cmake/project.cmake)
    idf_build_set_property(COMPILE_OPTIONS "-Wno-error=format" APPEND)
    project(desk_agent_firmware)
    include_directories(${CMAKE_CURRENT_LIST_DIR}/main/include)
    add_compile_options(-Wno-error=format)
else()
    # Standalone CMake configuration for host unit testing & syntax verification
    project(desk_agent_firmware C CXX)
    set(CMAKE_C_STANDARD 11)
    set(CMAKE_CXX_STANDARD 17)

    include_directories(
        ${CMAKE_CURRENT_SOURCE_DIR}/components/desk_hardware/include
        ${CMAKE_CURRENT_SOURCE_DIR}/components/desk_tclaw/include
        ${CMAKE_CURRENT_SOURCE_DIR}/components/desk_sdk_bridge/include
        ${CMAKE_CURRENT_SOURCE_DIR}/main/include
    )

    add_library(desk_hardware STATIC
        ${CMAKE_CURRENT_SOURCE_DIR}/components/desk_hardware/src/desk_hardware.c
        ${CMAKE_CURRENT_SOURCE_DIR}/components/desk_hardware/src/desk_arbiter.c
    )

    add_library(desk_tclaw STATIC
        ${CMAKE_CURRENT_SOURCE_DIR}/components/desk_tclaw/src/desk_tclaw_bridge.c
    )

    find_package(Threads REQUIRED)

    add_library(desk_sdk_bridge STATIC
        ${CMAKE_CURRENT_SOURCE_DIR}/components/desk_sdk_bridge/src/desk_sdk_bridge.c
    )
    target_link_libraries(desk_sdk_bridge PRIVATE Threads::Threads)

    add_executable(desk_agent_firmware_test
        ${CMAKE_CURRENT_SOURCE_DIR}/main/src/app_main.c
        ${CMAKE_CURRENT_SOURCE_DIR}/main/src/provisioning_state.c
        ${CMAKE_CURRENT_SOURCE_DIR}/main/src/tclaw_runtime_adapter.c
        ${CMAKE_CURRENT_SOURCE_DIR}/components/desk_cli/src/desk_cli.c
    )
    target_link_libraries(desk_agent_firmware_test PRIVATE desk_hardware desk_tclaw desk_sdk_bridge Threads::Threads)
endif()
