import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).with_name("build_isolated_firmware.py")
SPEC = importlib.util.spec_from_file_location("build_isolated_firmware", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)

BRIDGE_PATH = Path(__file__).with_name("run_isolated_tuyaopen_bridge.py")
BRIDGE_SPEC = importlib.util.spec_from_file_location("run_isolated_tuyaopen_bridge", BRIDGE_PATH)
BRIDGE = importlib.util.module_from_spec(BRIDGE_SPEC)
assert BRIDGE_SPEC.loader is not None
BRIDGE_SPEC.loader.exec_module(BRIDGE)


class IsolatedFirmwareBuildTests(unittest.TestCase):
    def test_mounted_bridge_wrapper_preserves_archive_paths_lexically(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            wrapper = root / "mounted_bridge.py"
            bridge = root / "fake_bridge.py"
            bridge.write_text(
                "import json, os, sys\n"
                "from pathlib import Path\n"
                "def main():\n"
                "    values = sys.argv[1:4]\n"
                "    print(json.dumps({\n"
                "        'resolve': [str(Path(value).resolve()) for value in values],\n"
                "        'realpath': [os.path.realpath(value) for value in values],\n"
                "        'repo': str(Path(sys.argv[5]).resolve()),\n"
                "    }))\n"
                "    return 0\n",
                encoding="utf-8",
            )
            wrapper.write_text(MODULE.mounted_bridge_wrapper_source(), encoding="utf-8")
            command = MODULE.mounted_bridge_command(
                Path(__import__("sys").executable),
                wrapper,
                bridge,
                Path("Y:\\"),
                root / "repo",
                root / "source-sdk",
            )

            result = subprocess.run(command, check=True, capture_output=True, text=True)
            observed = __import__("json").loads(result.stdout)
            expected = ["Y:\\stage\\sdk", "Y:\\stage\\app", "Y:\\"]
            self.assertEqual(observed["resolve"], expected)
            self.assertEqual(observed["realpath"], expected)
            self.assertEqual(observed["repo"], str((root / "repo").resolve()))

    def test_archive_subst_mount_passes_short_paths_and_unmounts_after_build_error(self):
        mappings = {}
        calls = []

        def fake_run(command, **kwargs):
            calls.append(command)
            if command == ["subst"]:
                output = "".join(f"{drive}:\\: => {target}\n" for drive, target in mappings.items())
                return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")
            drive = command[1][0]
            if command[2] == "/D":
                mappings.pop(drive, None)
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
            mappings[drive] = command[2]
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        archive = Path(r"D:\workspace\firmware\.target_logging\a7r4")
        with self.assertRaisesRegex(RuntimeError, "synthetic build failure"):
            with MODULE.temporary_archive_mount(
                archive,
                runner=fake_run,
                drive_available=lambda _letter: True,
                candidates=("Y",),
            ) as mount:
                paths = MODULE.archive_stage_paths(mount)
                self.assertEqual(str(mount), "Y:\\")
                self.assertEqual(str(paths["stage"]), "Y:\\stage")
                self.assertEqual(str(paths["sdk"]), "Y:\\stage\\sdk")
                self.assertEqual(str(paths["app"]), "Y:\\stage\\app")
                self.assertTrue(all(str(path).startswith("Y:\\") for path in paths.values()))
                raise RuntimeError("synthetic build failure")

        self.assertNotIn("Y", mappings)
        self.assertIn(["subst", "Y:", "/D"], calls)

    def test_archive_subst_mount_skips_conflicting_drive_letter(self):
        mappings = {}
        calls = []

        def fake_run(command, **kwargs):
            calls.append(command)
            if command == ["subst"]:
                output = "".join(f"{drive}:\\: => {target}\n" for drive, target in mappings.items())
                return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")
            drive = command[1][0]
            if command[2] == "/D":
                mappings.pop(drive, None)
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
            mappings[drive] = command[2]
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with MODULE.temporary_archive_mount(
            Path(r"D:\workspace\firmware\.target_logging\a7r4"),
            runner=fake_run,
            drive_available=lambda letter: letter != "Z",
            candidates=("Z", "Y"),
        ) as mount:
            self.assertEqual(str(mount), "Y:\\")
        self.assertFalse(any(call[0] == "subst" and len(call) > 1 and call[1] == "Z:" for call in calls))
        self.assertNotIn("Y", mappings)

    def test_archive_subst_mount_cleans_partial_mount_when_subst_reports_failure(self):
        mappings = {}
        calls = []

        def fake_run(command, **kwargs):
            calls.append(command)
            if command == ["subst"]:
                output = "".join(f"{drive}:\\: => {target}\n" for drive, target in mappings.items())
                return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")
            drive = command[1][0]
            if command[2] == "/D":
                mappings.pop(drive, None)
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
            mappings[drive] = command[2]
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="mount failed")

        with self.assertRaisesRegex(RuntimeError, "Could not create temporary subst drive"):
            with MODULE.temporary_archive_mount(
                Path(r"D:\workspace\firmware\.target_logging\a7r4"),
                runner=fake_run,
                drive_available=lambda _letter: True,
                candidates=("Y",),
            ):
                self.fail("failed subst must not yield a mount")
        self.assertNotIn("Y", mappings)
        self.assertIn(["subst", "Y:", "/D"], calls)

    def test_archive_path_must_be_under_target_logging(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_logging = root / "firmware" / ".target_logging"
            archive = target_logging / "run-1"
            outside = root / "firmware" / "build"
            self.assertTrue(MODULE.is_within(archive, target_logging))
            self.assertFalse(MODULE.is_within(outside, target_logging))
            self.assertTrue(MODULE.is_within(target_logging, target_logging))
            self.assertEqual(archive, target_logging / "run-1")

    def test_firmware_staging_excludes_existing_build_and_archive_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "firmware"
            (source / "main").mkdir(parents=True)
            (source / ".build" / "bin").mkdir(parents=True)
            (source / "build").mkdir()
            (source / "dist").mkdir()
            (source / ".target_logging" / "prior-run").mkdir(parents=True)
            (source / "main" / "app.c").write_text("source", encoding="utf-8")
            (source / ".build" / "bin" / "old.bin").write_bytes(b"old")
            (source / "build" / "CMakeCache.txt").write_text("cache", encoding="utf-8")
            (source / "dist" / "old.bin").write_bytes(b"dist")
            (source / ".target_logging" / "prior-run" / "log.txt").write_text("log", encoding="utf-8")

            destination = root / "archive" / "stage" / "app"
            MODULE._copy_firmware(source, destination)

            self.assertEqual((destination / "main" / "app.c").read_text(encoding="utf-8"), "source")
            for excluded in (".build", "build", "dist", ".target_logging"):
                self.assertFalse((destination / excluded).exists())

    def test_sdk_staging_keeps_project_build_and_managed_components_local(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sdk = root / "sdk"
            platform = sdk / "platform" / "ESP32"
            project = platform / "tuya_open_sdk"
            (sdk / "tools").mkdir(parents=True)
            source_header = sdk / "src" / "common" / "include" / "tuya_iot_config.h"
            source_header.parent.mkdir(parents=True)
            source_header.write_text("/* staged header */\n", encoding="utf-8")
            (sdk / "boards").mkdir()
            (platform / ".espressif").mkdir(parents=True)
            (platform / "esp-idf").mkdir()
            (platform / ".espressif" / "idf-env.json").write_text(
                '{"idfInstalled": {}}\n', encoding="utf-8"
            )
            version_header = (platform / "esp-idf" / "components" / "esp_common" / "include" / "esp_idf_version.h")
            version_header.parent.mkdir(parents=True)
            version_header.write_text(
                "#define ESP_IDF_VERSION_MAJOR 5\n#define ESP_IDF_VERSION_MINOR 4\n",
                encoding="utf-8",
            )
            (project / "managed_components" / "vendor").mkdir(parents=True)
            (project / "build").mkdir()
            (sdk / "CMakeLists.txt").write_text("cmake", encoding="utf-8")
            (sdk / "tos.py").write_text("cli", encoding="utf-8")
            (sdk / "platform" / "platform_config.yaml").write_text("platforms: []", encoding="utf-8")
            (platform / ".prepare").write_text("esp32s3", encoding="utf-8")
            (platform / "build_setup.py").write_text("setup", encoding="utf-8")
            (project / "sdkconfig").write_text("config", encoding="utf-8")
            (project / "managed_components" / "vendor" / "CMakeLists.txt").write_text("dep", encoding="utf-8")
            (project / "build" / "old.bin").write_bytes(b"old")

            staged = root / "archive" / "sdk"
            MODULE.stage_sdk(sdk, staged)

            self.assertTrue((staged / "platform" / "ESP32" / "tuya_open_sdk" / "managed_components" / "vendor" / "CMakeLists.txt").is_file())
            self.assertFalse((staged / "platform" / "ESP32" / "tuya_open_sdk" / "build").exists())
            self.assertEqual((staged / "platform" / "ESP32" / ".prepare").read_text(encoding="utf-8"), "esp32s3")
            self.assertEqual((staged / "platform" / "ESP32" / "tuya_open_sdk" / "sdkconfig").read_text(encoding="utf-8"), "config")
            self.assertTrue((staged / "src").is_dir())
            self.assertTrue((staged / "tools").is_dir())
            staged_header = staged / "src" / "common" / "include" / "tuya_iot_config.h"
            self.assertFalse(MODULE.os.path.samefile(staged / "src", sdk / "src"))
            self.assertEqual(staged_header.read_text(encoding="utf-8"), source_header.read_text(encoding="utf-8"))
            self.assertTrue(MODULE.is_within(staged_header.resolve(), root / "archive"))

    def test_sdk_staging_copies_all_runtime_inputs_and_records_directory_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sdk = root / "sdk"
            platform = sdk / "platform" / "ESP32"
            project = platform / "tuya_open_sdk"
            staged_dirs = ("boards", "tools", "platform/ESP32/esp-idf", "platform/ESP32/.espressif")
            for relative in staged_dirs:
                source_dir = sdk / Path(relative)
                source_dir.mkdir(parents=True, exist_ok=True)
                (source_dir / "input.txt").write_text(f"{relative}\n", encoding="utf-8")
            source_idf_env = sdk / "platform" / "ESP32" / ".espressif" / "idf-env.json"
            source_idf_env.write_text(MODULE.json.dumps({
                "idfInstalled": {
                    "existing-idf-v5.4": {
                        "version": "5.4", "path": "existing-idf", "features": ["core"], "targets": ["esp32"]
                    }
                }
            }), encoding="utf-8")
            source_idf_env_bytes = source_idf_env.read_bytes()
            idf_version_header = sdk / "platform" / "ESP32" / "esp-idf" / "components" / "esp_common" / "include" / "esp_idf_version.h"
            idf_version_header.parent.mkdir(parents=True)
            idf_version_header.write_text(
                "#define ESP_IDF_VERSION_MAJOR 5\n#define ESP_IDF_VERSION_MINOR 4\n", encoding="utf-8"
            )
            (sdk / "src" / "common").mkdir(parents=True)
            (sdk / "src" / "common" / "header.h").write_text("header\n", encoding="utf-8")
            (sdk / "CMakeLists.txt").write_text("cmake", encoding="utf-8")
            (sdk / "tos.py").write_text("cli", encoding="utf-8")
            (sdk / "platform" / "platform_config.yaml").write_text("platforms: []", encoding="utf-8")
            (platform / ".prepare").write_text("esp32s3", encoding="utf-8")
            (platform / "build_setup.py").write_text("setup", encoding="utf-8")
            (project / "managed_components" / "vendor").mkdir(parents=True)
            (project / "managed_components" / "vendor" / "dep.h").write_text("dep", encoding="utf-8")
            (project / "sdkconfig").write_text("config", encoding="utf-8")

            staged = root / "archive" / "stage" / "sdk"
            MODULE.stage_sdk(sdk, staged)

            for relative in staged_dirs:
                source_dir = sdk / Path(relative)
                staged_dir = staged / Path(relative)
                self.assertTrue(staged_dir.is_dir(), relative)
                self.assertFalse(MODULE.os.path.samefile(source_dir, staged_dir), relative)
                self.assertFalse(staged_dir.is_symlink(), relative)
                entries = [staged_dir, *staged_dir.rglob("*")]
                self.assertFalse(any(
                    entry.is_symlink()
                    or (getattr(entry, "is_junction", None) is not None and entry.is_junction())
                    or bool(getattr(entry.lstat(), "st_file_attributes", 0) & 0x400)
                    for entry in entries
                ), relative)
                self.assertEqual((staged_dir / "input.txt").read_text(encoding="utf-8"),
                                 (source_dir / "input.txt").read_text(encoding="utf-8"))
            self.assertTrue((staged / "platform" / "ESP32" / "tuya_open_sdk" / "managed_components" / "vendor" / "dep.h").is_file())

            manifest_path = staged.parent / "staged-sdk-inputs.json"
            self.assertTrue(manifest_path.is_file())
            manifest = __import__("json").loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(set(manifest["directories"]), set(staged_dirs))
            self.assertTrue(all(item["samefile_source"] is False for item in manifest["directories"].values()))
            self.assertTrue(all(item["reparse_points"] == 0 for item in manifest["directories"].values()))
            self.assertTrue(manifest["aggregate_sha256"])

            staged_idf = staged / "platform" / "ESP32" / "esp-idf"
            staged_env_path = staged / "platform" / "ESP32" / ".espressif" / "idf-env.json"
            self.assertEqual(source_idf_env.read_bytes(), source_idf_env_bytes)
            staged_env = MODULE.json.loads(staged_env_path.read_text(encoding="utf-8"))
            staged_repo = str(staged_idf.resolve())
            record = staged_env["idfInstalled"][f"{staged_repo}-v5.4"]
            self.assertEqual(record, {
                "version": "5.4",
                "path": staged_repo,
                "features": ["core"],
                "targets": ["esp32s3"],
            })
            self.assertEqual(MODULE.assert_staged_idf_target(staged_idf, staged / "platform" / "ESP32" / ".espressif", "esp32s3"), record)

    def test_idf_registry_matches_canonical_active_repo_id_through_subst_alias(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            idf_root = root / "stage" / "sdk" / "platform" / "ESP32" / "esp-idf"
            espressif_root = root / "stage" / "sdk" / "platform" / "ESP32" / ".espressif"
            version_header = idf_root / "components" / "esp_common" / "include" / "esp_idf_version.h"
            version_header.parent.mkdir(parents=True)
            version_header.write_text(
                "#define ESP_IDF_VERSION_MAJOR 5\n#define ESP_IDF_VERSION_MINOR 4\n",
                encoding="utf-8",
            )
            espressif_root.mkdir(parents=True)
            env_path = espressif_root / "idf-env.json"
            env_path.write_text('{"idfInstalled": {}}\n', encoding="utf-8")

            alias_root = r"Y:\stage\sdk\platform\ESP32\esp-idf"
            canonical_root = r"D:\ePanda\DeskAgent\firmware\.target_logging\a7r5\stage\sdk\platform\ESP32\esp-idf"
            realpath = MODULE.os.path.realpath

            def fake_realpath(path):
                value = str(path)
                if value.lower() == alias_root.lower() or value.lower() == str(idf_root).lower():
                    return canonical_root
                return realpath(path)

            with mock.patch.object(MODULE.os.path, "realpath", side_effect=fake_realpath):
                self.assertEqual(MODULE.canonical_idf_registry_path(Path(alias_root)), Path(canonical_root))
                record = MODULE.register_staged_idf_target(idf_root, espressif_root, "esp32s3")
                validated = MODULE.assert_staged_idf_target(idf_root, espressif_root, "esp32s3")

            registry = MODULE.json.loads(env_path.read_text(encoding="utf-8"))["idfInstalled"]
            expected_active_repo_id = f"{canonical_root}-v5.4"
            self.assertIn(expected_active_repo_id, registry)
            self.assertEqual(registry[expected_active_repo_id]["path"], canonical_root)
            self.assertEqual(registry[expected_active_repo_id]["targets"], ["esp32s3"])
            self.assertEqual(record, registry[expected_active_repo_id])
            self.assertEqual(validated, record)

    def test_partition_config_reasserts_absolute_staged_csv(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "sdkconfig"
            csv = root / "app" / "partitions.csv"
            csv.parent.mkdir()
            csv.write_text("csv", encoding="utf-8")
            config.write_text('CONFIG_PARTITION_TABLE_CUSTOM_FILENAME="partitions.csv"\n'
                              'CONFIG_PARTITION_TABLE_FILENAME="partitions.csv"\n', encoding="utf-8")
            MODULE.configure_staged_partition_config(config, csv)
            resolved = csv.resolve().as_posix()
            result = config.read_text(encoding="utf-8")
            self.assertIn(f'CONFIG_PARTITION_TABLE_CUSTOM_FILENAME="{resolved}"', result)
            self.assertIn(f'CONFIG_PARTITION_TABLE_FILENAME="{resolved}"', result)
            self.assertNotIn('CONFIG_PARTITION_TABLE_FILENAME="partitions.csv"', result)

    def test_cli_arguments_allow_helper_without_archive_but_require_it_for_build(self):
        helper = MODULE.parse_arguments(["--configure-partition", "sdkconfig", "partitions.csv"])
        self.assertEqual(helper.configure_partition, ["sdkconfig", "partitions.csv"])
        self.assertIsNone(helper.archive)

        build = MODULE.parse_arguments(["--archive", "firmware/.target_logging/run"])
        self.assertEqual(build.archive, Path("firmware/.target_logging/run"))
        self.assertIsNone(build.configure_partition)

        with self.assertRaises(SystemExit) as error:
            MODULE.parse_arguments([])
        self.assertEqual(error.exception.code, 2)

    def test_sdk_gitpython_is_located_without_changing_process_environment(self):
        with tempfile.TemporaryDirectory() as temp:
            sdk = Path(temp) / "sdk"
            site = sdk / ".venv" / "Lib" / "site-packages"
            for package in ("git", "gitdb"):
                package_dir = site / package
                package_dir.mkdir(parents=True)
                (package_dir / "__init__.py").write_text("", encoding="utf-8")
            before = dict(MODULE.os.environ)
            self.assertEqual(MODULE.gitpython_site_packages(sdk), site.resolve())
            self.assertEqual(dict(MODULE.os.environ), before)

    def test_bridge_imports_sdk_gitpython_in_its_own_sys_path(self):
        with tempfile.TemporaryDirectory() as temp:
            sdk = Path(temp) / "sdk"
            site = sdk / ".venv" / "Lib" / "site-packages"
            for package in ("git", "gitdb"):
                package_dir = site / package
                package_dir.mkdir(parents=True)
                (package_dir / "__init__.py").write_text("", encoding="utf-8")

            previous = {name: BRIDGE.sys.modules.pop(name, None) for name in ("git", "gitdb")}
            try:
                resolved = BRIDGE.load_sdk_gitpython(sdk)
                self.assertEqual(resolved, site.resolve())
                self.assertEqual(str(resolved), BRIDGE.sys.path[0])
                self.assertTrue(Path(BRIDGE.sys.modules["git"].__file__).is_relative_to(resolved))
                self.assertTrue(Path(BRIDGE.sys.modules["gitdb"].__file__).is_relative_to(resolved))
            finally:
                BRIDGE.sys.modules.pop("git", None)
                BRIDGE.sys.modules.pop("gitdb", None)
                BRIDGE.sys.path.remove(str(site.resolve()))
                for name, module in previous.items():
                    if module is not None:
                        BRIDGE.sys.modules[name] = module

    def test_source_snapshot_returns_provenance_dict(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "repo"
            sdk = Path(temp) / "sdk"
            (repo / "firmware" / ".build" / "build").mkdir(parents=True)
            (repo / "tools").mkdir()
            (repo / "firmware" / ".build" / "build" / "build_param.json").write_text("{}", encoding="utf-8")
            (repo / "tools" / "build_watcher_firmware.ps1").write_text("build", encoding="utf-8")
            (repo / "tools" / "build_watcher_tuyaopen.py").write_text("bridge", encoding="utf-8")
            outputs = [
                b"", b"tracked diff", "source-head\n", "sdk-head\n", "",
            ]

            def completed(stdout):
                return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout)

            with mock.patch.object(BRIDGE.subprocess, "run", side_effect=[completed(value) for value in outputs]):
                snapshot = BRIDGE.source_snapshot(repo, sdk, Path(temp) / "staged-firmware")

            self.assertIsInstance(snapshot, dict)
            self.assertEqual(snapshot["source_commit"], "source-head")
            self.assertEqual(snapshot["diff_bytes"], b"tracked diff")
            self.assertTrue(snapshot["aggregate_sha256"])
            self.assertEqual(len(snapshot["inputs"]), 3)
            self.assertEqual(snapshot["external_sdk_state"]["head"], "sdk-head")

    def test_idf_export_uses_staging_espressif_paths_for_both_build_phases(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sdk = root / "sdk"
            staged_sdk = root / "archive" / "stage" / "sdk"
            source_tools = sdk / "platform" / "ESP32" / ".espressif"
            staged_tools = staged_sdk / "platform" / "ESP32" / ".espressif"
            source_scripts = source_tools / "python_env" / "idf5.4_py3.12_env" / "Scripts"
            staged_scripts = staged_tools / "python_env" / "idf5.4_py3.12_env" / "Scripts"
            source_scripts.mkdir(parents=True)
            staged_scripts.mkdir(parents=True)
            source_python = source_scripts / "python.exe"
            staged_python = staged_scripts / "python.exe"
            source_python.write_bytes(b"python")
            staged_python.write_bytes(b"python")

            tools_root, scripts = MODULE.staged_idf_tool_paths(sdk, staged_sdk, source_python, source_scripts)
            prefix = MODULE.idf_export_prefix(tools_root, scripts, staged_sdk / "platform" / "ESP32" / "esp-idf" / "export.bat")

            self.assertNotIn("PYTHONPATH", prefix)
            self.assertIn(f'IDF_TOOLS_PATH={staged_tools}', prefix)
            self.assertIn(str(staged_scripts), prefix)
            self.assertNotIn(str(source_tools), prefix)
            self.assertIn(f'call "{staged_sdk / "platform" / "ESP32" / "esp-idf" / "export.bat"}"', prefix)

    def test_sdk_gitpython_preflight_rejects_missing_packages(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(RuntimeError, "GitPython and GitDB are not available"):
                MODULE.gitpython_site_packages(Path(temp) / "sdk")


if __name__ == "__main__":
    unittest.main()
