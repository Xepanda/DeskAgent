cmake_minimum_required(VERSION 3.16)

# Detect if compiling within TuyaOpen / ESP-IDF build system or standalone
if(DEFINED CMAKE_TOOLCHAIN_FILE OR DEFINED ENV{IDF_PATH})
    include($ENV{IDF_PATH}/tools/cmake/project.cmake)
    project(desk_agent_firmware)
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
    )
    target_link_libraries(desk_agent_firmware_test PRIVATE desk_hardware desk_tclaw desk_sdk_bridge Threads::Threads)
endif()
