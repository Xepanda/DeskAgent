# Watcher dependency registration

This IDF component is discovered through the existing `firmware/esp_components`
Junction, which points to `firmware/components`. It registers the official,
vendored SPD2010 implementation under `firmware/third_party/esp_lcd_spd2010`.

The wrapper has no `src/*.c` files. The source is therefore compiled only as
the IDF component `desk_watcher_deps`, not by the phase-1 `COMP_SRCS` glob in
the firmware app CMake. The component exports the `esp_lcd_spd2010.h` include
directory and depends on IDF `esp_lcd` / `driver`.

This registration contains no board adapter, pin configuration, power
sequencing, touch support, or LVGL dependency. Those remain the responsibility
of the Watcher hardware adapter package.
