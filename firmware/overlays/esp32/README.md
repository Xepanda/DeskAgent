# ESP32 SDK overlay

`audio_afe.c` is based on TuyaOpen ESP32 platform SDK source, SDK commit
`cdc064e78daeeae598157470244e351cd37cbbed`, path
`platform/ESP32/tuya_open_sdk/tuyaos_adapter/src/audio/audio_afe.c`.
ESP32 platform submodule commit: `8cd2a15a90c56ffc263efb04713445fc0d5c615b`.
Original SHA256: `4d7cb527a881b1aec6c8beaba4e6a255984cf787dfc1f0e5d858d9288bd1e6c1`.

Project changes stop initialization when models/configuration are unavailable,
avoid repeated partial initialization, correct configuration cleanup, and make
stop safe before initialization. Native IDF registration replaces this one
translation unit; it does not edit the installed SDK or fake KWS/VAD results.
It also selects WebRTC noise suppression when no neural NS model is present,
and checks buffer/task creation before reporting AFE initialization success.
