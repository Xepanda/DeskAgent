# TClaw pinned source provenance

- Upstream: https://github.com/tuya/TClaw
- Pinned commit: `88bd37378f1d106fcc1175e61b368222cc8fabda`
- Upstream repository license: MIT; the original `LICENSE` file is retained.
- Upstream TuyaOpen submodule: path `TuyaOpen`, gitlink `e82840dc9b629cb77fa922fb04e3f820dc2e78b4`.
- The TuyaOpen submodule contents are intentionally not vendored. This project uses the separately locked local TuyaOpen SDK checkout; its compatibility with the pinned application is evaluated by the project build.
- The selected source/build files originate at the pinned TClaw commit. Project patch: include/tuya_app_config.h makes local secrets optional and protects defaults with macro guards; no real credentials are embedded. `src/tuya_app_main.c`, board/profile configuration files, the secrets example, repository instructions, and non-build documentation are excluded. The project app entry remains owned by the firmware integration layer.
- Source file list SHA-256: `b7fc36384435f141f04e7cd254fbdd0e0cf3a4a5ba0290ee842ef08b46a32e64`.

This provenance does not claim a successful compile, network session, or device runtime test.
