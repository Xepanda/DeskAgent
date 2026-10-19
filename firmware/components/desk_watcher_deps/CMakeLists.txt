# Project-local IDF wrapper: keep the vendored upstream source out of the
# phase-1 components/*/src/*.c glob; compile it once as an IDF component.
idf_component_register(
    SRCS "${CMAKE_CURRENT_LIST_DIR}/../../third_party/esp_lcd_spd2010/esp_lcd_spd2010.c"
    INCLUDE_DIRS "${CMAKE_CURRENT_LIST_DIR}/../../third_party/esp_lcd_spd2010/include"
    PRIV_REQUIRES driver
    REQUIRES esp_lcd
    WHOLE_ARCHIVE
)
target_compile_definitions(${COMPONENT_LIB} PRIVATE
    ESP_LCD_SPD2010_VER_MAJOR=1 ESP_LCD_SPD2010_VER_MINOR=0 ESP_LCD_SPD2010_VER_PATCH=1)
