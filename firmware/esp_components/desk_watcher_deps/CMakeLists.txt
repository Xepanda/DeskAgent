# TuyaOpen phase 1 compiles DeskAgent into libtuyaapp. Only native IDF
# dependencies belong here; exposing all firmware/components duplicates code.
include("${CMAKE_CURRENT_LIST_DIR}/../../components/desk_watcher_deps/CMakeLists.txt")
if(CMAKE_BUILD_EARLY_EXPANSION)
    return()
endif()

# Apply a project-owned fix after IDF has registered tuyaos_adapter. The SDK
# checkout stays unchanged. No alternate KWS/VAD implementation is supplied.
set_property(GLOBAL PROPERTY DESK_WATCHER_AFE_OVERLAY
    "${CMAKE_CURRENT_LIST_DIR}/../../overlays/esp32/audio_afe.c")
function(_desk_watcher_patch_afe)
    if(NOT TARGET __idf_tuyaos_adapter)
        message(FATAL_ERROR "Watcher requires the real TuyaOS adapter")
    endif()
    get_target_property(_sources __idf_tuyaos_adapter SOURCES)
    set(_replacement)
    set(_found FALSE)
    get_property(_overlay GLOBAL PROPERTY DESK_WATCHER_AFE_OVERLAY)
    foreach(_source IN LISTS _sources)
        get_filename_component(_name "${_source}" NAME)
        if(_name STREQUAL "audio_afe.c")
            list(APPEND _replacement "${_overlay}")
            set(_found TRUE)
        elseif(_source MATCHES "/boards/ESP32/common/audio/")
            # These are alternative generic board TDD drivers. Watcher uses
            # its own dual-chip driver already compiled into libtuyaapp.
        else()
            list(APPEND _replacement "${_source}")
        endif()
    endforeach()
    if(NOT _found)
        message(FATAL_ERROR "Watcher audio requires CONFIG_ENABLE_AUDIO in TuyaOpen")
    endif()
    set_property(TARGET __idf_tuyaos_adapter PROPERTY SOURCES "${_replacement}")
endfunction()
cmake_language(DEFER DIRECTORY "${CMAKE_SOURCE_DIR}" CALL _desk_watcher_patch_afe)
