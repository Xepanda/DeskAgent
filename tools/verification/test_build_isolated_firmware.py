import importlib.util
import builtins
import contextlib
import io
from pathlib import Path
import subprocess
import sys
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

LEGACY_BRIDGE_PATH = MODULE_PATH.parents[1] / "build_watcher_tuyaopen.py"
LEGACY_SPEC = importlib.util.spec_from_file_location("build_watcher_tuyaopen", LEGACY_BRIDGE_PATH)
LEGACY_BRIDGE = importlib.util.module_from_spec(LEGACY_SPEC)
assert LEGACY_SPEC.loader is not None
LEGACY_SPEC.loader.exec_module(LEGACY_BRIDGE)


class IsolatedFirmwareBuildTests(unittest.TestCase):
    def test_powershell_entrypoint_delegates_without_reading_generated_sdk_state(self):
        script = (MODULE_PATH.parents[1] / "build_watcher_firmware.ps1").read_text(encoding="utf-8")
        self.assertNotRegex(script, r"(?i)sdkconfig(?:\.old|\.h)?")
        self.assertNotIn("ReadAllBytes", script)
        self.assertIn("build_isolated_firmware.py", script)

    def test_reviewed_defaults_are_staged_fresh_and_required_settings_are_checked(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            firmware = root / "firmware"
            project = root / "stage" / "project"
            firmware.mkdir()
            defaults = ('CONFIG_IDF_TARGET="esp32s3"\n'
                        "CONFIG_PARTITION_TABLE_CUSTOM=y\n"
                        'CONFIG_ESPTOOLPY_FLASHSIZE="32MB"\n')
            (firmware / "sdkconfig.defaults").write_text(defaults, encoding="utf-8")
            staged = MODULE.install_reviewed_sdkconfig_defaults(firmware, project)
            self.assertEqual(staged.read_text(encoding="utf-8"), defaults)
            with self.assertRaises(FileExistsError):
                MODULE.install_reviewed_sdkconfig_defaults(firmware, project)

    def test_generated_sdkconfig_sensitive_symbols_fail_before_hashing(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "sdkconfig"
            config.write_text('CONFIG_IDF_TARGET="esp32s3"\n'
                              "CONFIG_PARTITION_TABLE_CUSTOM=y\n"
                              'CONFIG_ESPTOOLPY_FLASHSIZE="32MB"\n'
                              'CONFIG_WIFI_PASSWORD="secret"\n', encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "sensitive-looking"):
                MODULE.assert_non_sensitive_sdkconfig(config)

    def test_generated_sdkconfig_must_resolve_approved_target_and_flash_defaults(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "sdkconfig"
            config.write_text('CONFIG_IDF_TARGET="esp32s3"\n', encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "missing required build settings"):
                MODULE.assert_non_sensitive_sdkconfig(config)

    def test_managed_components_must_match_tracked_lock_exactly(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            lock = root / "dependencies.lock"
            managed = root / "managed_components"
            managed.mkdir()
            (managed / "vendor__codec").mkdir()
            (managed / "vendor__unlocked").mkdir()
            lock.write_text("dependencies:\n  vendor/codec:\n    version: 1.2.3\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "not covered by the tracked lock"):
                MODULE.validate_managed_component_lock(lock, managed)

    def test_managed_components_are_only_accepted_when_lock_set_matches(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            lock = root / "dependencies.lock"
            managed = root / "managed_components"
            managed.mkdir()
            (managed / "vendor__codec").mkdir()
            lock.write_text("dependencies:\n  vendor/codec:\n    version: 1.2.3\n", encoding="utf-8")
            self.assertEqual(MODULE.validate_managed_component_lock(lock, managed), ["vendor__codec"])

    def test_stage_sdk_missing_manifest_blocks_before_any_sdk_path_access(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sdk = root / "sdk"
            project = sdk / "platform" / "ESP32" / "tuya_open_sdk"
            managed = project / "managed_components"
            (managed / "vendor__unlocked").mkdir(parents=True)
            (sdk / "src").mkdir(parents=True)
            (sdk / "boards").mkdir()
            (sdk / "tools").mkdir()
            (sdk / "CMakeLists.txt").write_text("cmake", encoding="utf-8")
            (sdk / "tos.py").write_text("cli", encoding="utf-8")
            (sdk / "platform" / "platform_config.yaml").write_text("platforms: []", encoding="utf-8")
            (sdk / "platform" / "ESP32" / "esp-idf" / "build").mkdir(parents=True)
            (sdk / "platform" / "ESP32" / ".espressif" / "python_env" / ".cache").mkdir(parents=True)
            (sdk / "platform" / "ESP32" / "esp-idf" / "build" / "generated.h").write_text("sentinel", encoding="utf-8")
            (sdk / "platform" / "ESP32" / ".espressif" / "python_env" / ".cache" / "credential").write_text("sentinel", encoding="utf-8")
            (sdk / "platform" / "ESP32" / ".prepare").write_text("esp32s3", encoding="utf-8")
            lock = root / "dependencies.lock"
            lock.write_text("dependencies:\n  vendor/codec:\n    version: 1.0.0\n", encoding="utf-8")
            forbidden_roots = [sdk]
            original_ops = self._forbidden_access_guards(forbidden_roots)
            original_copy2 = MODULE.shutil.copy2
            original_copytree = MODULE.shutil.copytree
            copies = []

            def guarded_copy2(source, destination, *args, **kwargs):
                copies.append(Path(source))
                return original_copy2(source, destination, *args, **kwargs)

            def guarded_copytree(source, destination, *args, **kwargs):
                copies.append(Path(source))
                return original_copytree(source, destination, *args, **kwargs)

            with original_ops, \
                    mock.patch.object(MODULE.shutil, "copy2", guarded_copy2), \
                    mock.patch.object(MODULE.shutil, "copytree", guarded_copytree):
                with self.assertRaisesRegex(RuntimeError, "BLOCKED: tracked SDK provenance manifest"):
                    MODULE.stage_sdk(sdk, root / "archive" / "sdk", dependency_lock=lock,
                                     repo_root=root)
            self.assertEqual(copies, [])

    def _forbidden_access_guards(self, roots):
        """Fail if any common pathlib/os file traversal operation touches roots."""
        def forbidden(value):
            try:
                path = Path(value)
            except (TypeError, ValueError):
                return False
            return any(path == root or root in path.parents for root in roots)

        patches = []
        original_builtin_open = builtins.open

        def guarded_builtin_open(file, *args, **kwargs):
            if forbidden(file):
                raise AssertionError(f"forbidden builtins.open access: {file}")
            return original_builtin_open(file, *args, **kwargs)

        patches.append(mock.patch.object(builtins, "open", guarded_builtin_open))
        for name in ("open", "stat", "lstat", "exists", "is_file", "is_dir", "resolve", "glob", "rglob", "iterdir"):
            original = getattr(Path, name)

            def guard(path, *args, __name=name, __original=original, **kwargs):
                if forbidden(path):
                    raise AssertionError(f"forbidden Path.{__name} access: {path}")
                return __original(path, *args, **kwargs)

            patches.append(mock.patch.object(Path, name, guard))

        for name in ("stat", "scandir"):
            original = getattr(MODULE.os, name)

            def guard(value, *args, __name=name, __original=original, **kwargs):
                if forbidden(value):
                    raise AssertionError(f"forbidden os.{__name} access: {value}")
                return __original(value, *args, **kwargs)

            patches.append(mock.patch.object(MODULE.os, name, guard))

        class GuardContext:
            def __enter__(self):
                for patcher in patches:
                    patcher.__enter__()
                return self

            def __exit__(self, *exc):
                for patcher in reversed(patches):
                    patcher.__exit__(*exc)
        return GuardContext()

    def test_run_build_missing_tracked_manifest_blocks_before_any_sdk_access(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            sdk = root / "shared-sdk"
            (repo / "firmware" / ".target_logging").mkdir(parents=True)
            forbidden = [sdk]
            (sdk / "platform" / "ESP32" / ".espressif" / "python_env").mkdir(parents=True)
            (sdk / ".venv" / "Lib" / "site-packages").mkdir(parents=True)
            original_run = MODULE.subprocess.run
            calls = []

            def tracked_lookup(command, **kwargs):
                calls.append(command)
                if command[:3] == ["git", "ls-files", "--error-unmatch"]:
                    return subprocess.CompletedProcess(command, 1, stdout=b"", stderr=b"missing")
                return original_run(command, **kwargs)

            with self._forbidden_access_guards(forbidden), \
                    mock.patch.object(MODULE.subprocess, "run", side_effect=tracked_lookup):
                with self.assertRaisesRegex(RuntimeError, "BLOCKED: tracked SDK provenance manifest"):
                    MODULE.run_build(repo, sdk, repo / "firmware" / ".target_logging" / "attempt")
            self.assertEqual(len(calls), 1)
            self.assertNotIn(str(sdk), str(calls))

    def test_main_checks_repo_manifest_before_resolving_sdk_argument(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            sdk = root / "shared-sdk"
            archive = repo / "firmware" / ".target_logging" / "attempt"
            (repo / "firmware" / ".target_logging").mkdir(parents=True)
            sdk.mkdir()
            commands = []

            def missing_manifest(command, **kwargs):
                commands.append(command)
                return subprocess.CompletedProcess(command, 1, stdout=b"", stderr=b"missing")

            stderr = io.StringIO()
            with self._forbidden_access_guards([sdk]), \
                    mock.patch.object(MODULE.subprocess, "run", side_effect=missing_manifest), \
                    mock.patch.object(sys, "argv", [str(MODULE_PATH), "--repo", str(repo),
                                                       "--sdk", str(sdk), "--archive", str(archive)]), \
                    contextlib.redirect_stderr(stderr):
                result = MODULE.main()
            self.assertEqual(result, 2)
            self.assertIn("tracked SDK provenance manifest", stderr.getvalue())
            self.assertEqual(len(commands), 1)
            self.assertNotIn(str(sdk), str(commands))

    def test_configure_partition_cli_rejects_missing_stage_token_before_touching_inputs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            archive = repo / "firmware" / ".target_logging" / "run-unique"
            sdkconfig = root / "shared" / "sdkconfig"
            csv_path = root / "shared" / "partitions.csv"
            sdkconfig.parent.mkdir()
            sdkconfig.write_text("SENTINEL_CONFIG", encoding="utf-8")
            csv_path.write_text("SENTINEL_CSV", encoding="utf-8")
            forbidden = [sdkconfig, csv_path]
            stderr = io.StringIO()
            with self._forbidden_access_guards(forbidden), \
                    mock.patch.object(sys, "argv", [
                        str(MODULE_PATH), "--configure-partition", str(sdkconfig), str(csv_path),
                        "--archive", str(archive), "--stage-manifest",
                        str(archive / "stage" / "runner-stage-token.json"), "--repo", str(repo),
                    ]), contextlib.redirect_stderr(stderr):
                result = MODULE.main()
            self.assertEqual(result, 2)
            self.assertIn("runner stage token", stderr.getvalue())
            self.assertEqual(sdkconfig.read_text(encoding="utf-8"), "SENTINEL_CONFIG")
            self.assertEqual(csv_path.read_text(encoding="utf-8"), "SENTINEL_CSV")

    def test_configure_partition_cli_rejects_paths_outside_authorized_stage(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            archive = repo / "firmware" / ".target_logging" / "run-unique"
            stage = archive / "stage"
            stage.mkdir(parents=True)
            token_path = MODULE.write_stage_authorization(archive, archive)
            shared = root / "shared"
            sdkconfig = shared / "sdkconfig"
            csv_path = shared / "partitions.csv"
            shared.mkdir()
            sdkconfig.write_text("SENTINEL_CONFIG", encoding="utf-8")
            csv_path.write_text("SENTINEL_CSV", encoding="utf-8")
            stderr = io.StringIO()
            with self._forbidden_access_guards([sdkconfig, csv_path]), \
                    mock.patch.object(sys, "argv", [
                        str(MODULE_PATH), "--configure-partition", str(sdkconfig), str(csv_path),
                        "--archive", str(archive), "--stage-manifest", str(token_path), "--repo", str(repo),
                    ]), contextlib.redirect_stderr(stderr):
                result = MODULE.main()
            self.assertEqual(result, 2)
            self.assertIn("outside the authorized archive stage", stderr.getvalue())

    def test_configure_partition_cli_accepts_only_runner_authorized_stage_files(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "repo"
            archive = repo / "firmware" / ".target_logging" / "run-unique"
            stage = archive / "stage"
            sdkconfig = stage / MODULE.STAGE_CONFIG_RELATIVE
            csv_path = stage / MODULE.STAGE_PARTITIONS_RELATIVE
            sdkconfig.parent.mkdir(parents=True)
            csv_path.parent.mkdir(parents=True)
            sdkconfig.write_text('CONFIG_PARTITION_TABLE_FILENAME="partitions.csv"\n', encoding="utf-8")
            csv_path.write_text("csv", encoding="utf-8")
            token_path = MODULE.write_stage_authorization(archive, archive)
            with mock.patch.object(sys, "argv", [
                str(MODULE_PATH), "--configure-partition", str(sdkconfig), str(csv_path),
                "--archive", str(archive), "--stage-manifest", str(token_path), "--repo", str(repo),
            ]):
                self.assertEqual(MODULE.main(), 0)
            self.assertIn(str(csv_path.resolve()).replace("\\", "/"),
                          sdkconfig.read_text(encoding="utf-8"))

    def test_bridge_direct_cli_refuses_before_sdk_filesystem_or_subprocess_access(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sdk = root / "shared-sdk"
            (sdk / ".venv" / "Lib" / "site-packages").mkdir(parents=True)
            (sdk / "platform" / "ESP32" / "tuya_open_sdk" / "build").mkdir(parents=True)
            with self._forbidden_access_guards([sdk]), \
                    mock.patch.object(sys, "argv", [str(BRIDGE_PATH), str(sdk), str(root / "app"),
                                                      str(root / "archive"), "--repo", str(root / "repo"),
                                                      "--source-sdk", str(sdk)]):
                with self.assertRaisesRegex(RuntimeError, "BLOCKED: direct bridge invocation is disabled"):
                    BRIDGE.main()

    def test_legacy_tuyaopen_cli_refuses_before_sdk_filesystem_access(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sdk = root / "shared-sdk"
            (sdk / "platform" / "ESP32" / "tuya_open_sdk" / "build").mkdir(parents=True)
            calls = []
            def manifest_check(command, **kwargs):
                calls.append(command)
                return subprocess.CompletedProcess(command, 1, stdout=b"", stderr=b"manifest missing")

            with self._forbidden_access_guards([sdk]), \
                    mock.patch.object(LEGACY_BRIDGE.subprocess, "run", side_effect=manifest_check):
                with mock.patch.object(LEGACY_BRIDGE.sys, "argv", [
                    str(LEGACY_BRIDGE_PATH), str(sdk), str(root / "app"), str(root / "archive")
                ]):
                    with self.assertRaisesRegex(RuntimeError, "BLOCKED: tracked SDK provenance manifest"):
                        LEGACY_BRIDGE.main()
            self.assertEqual(len(calls), 1)
            self.assertNotIn(str(sdk), str(calls))

    def test_legacy_firmware_snapshot_helper_refuses_before_sdk_or_generated_access(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            firmware = repo / "firmware"
            sdk = root / "sdk"
            firmware.mkdir(parents=True)
            (repo / "tools").mkdir()
            (firmware / "sdkconfig.defaults").write_text("tracked defaults", encoding="utf-8")
            (repo / "tools" / "build_watcher_firmware.ps1").write_text("entry", encoding="utf-8")
            (repo / "tools" / "build_watcher_tuyaopen.py").write_text("bridge", encoding="utf-8")
            (firmware / ".build" / "build").mkdir(parents=True)
            (firmware / ".build" / "build" / "build_param.json").write_text("DO_NOT_OPEN", encoding="utf-8")
            calls = []

            def tracked_only(command, **kwargs):
                calls.append(command)
                if str(sdk) in command:
                    raise AssertionError(f"forbidden subprocess access under SDK root: {command}")
                if "--error-unmatch" in command:
                    return subprocess.CompletedProcess(command, 1, stdout=b"", stderr=b"manifest absent")
                if "ls-files" in command:
                    return subprocess.CompletedProcess(
                        command, 0,
                        stdout=b"firmware/sdkconfig.defaults\0tools/build_watcher_firmware.ps1\0tools/build_watcher_tuyaopen.py\0",
                        stderr=b"",
                    )
                return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

            with self._forbidden_access_guards([sdk, firmware / ".build"]), \
                    mock.patch.object(LEGACY_BRIDGE.subprocess, "run", side_effect=tracked_only):
                with self.assertRaisesRegex(RuntimeError, "BLOCKED: tracked SDK provenance manifest"):
                    LEGACY_BRIDGE.capture_firmware_inputs(firmware, repo, sdk)
            self.assertEqual(len(calls), 1)

    def test_isolated_bridge_does_not_expose_direct_source_snapshot_helper(self):
        self.assertFalse(hasattr(BRIDGE, "source_snapshot"))


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

    def test_sdk_staging_fails_closed_before_shared_generated_inputs_are_touched(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sdk = root / "sdk"
            platform = sdk / "platform" / "ESP32"
            project = platform / "tuya_open_sdk"
            managed = project / "managed_components" / "vendor__pkg"
            managed.mkdir(parents=True)
            (managed / "component.h").write_text("fixture", encoding="utf-8")
            for relative in ("sdkconfig", "sdkconfig.old", "build/config/sdkconfig.h"):
                path = project / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("DO_NOT_READ", encoding="utf-8")
            esp_idf = platform / "esp-idf"
            espressif = platform / ".espressif"
            (esp_idf / "build").mkdir(parents=True)
            (espressif / "python_env" / ".cache").mkdir(parents=True)
            (esp_idf / "build" / "generated.h").write_text("DO_NOT_READ", encoding="utf-8")
            (espressif / "python_env" / ".cache" / "credential").write_text("DO_NOT_READ", encoding="utf-8")
            lock = root / "dependencies.lock"
            lock.write_text("dependencies:\n  vendor/pkg:\n    version: 1.0.0\n", encoding="utf-8")
            stage = root / "archive" / "sdk"
            forbidden = [sdk]
            copies = []
            original_copy2 = MODULE.shutil.copy2
            original_copytree = MODULE.shutil.copytree

            def guarded_copy2(source, destination, *args, **kwargs):
                copies.append(Path(source))
                return original_copy2(source, destination, *args, **kwargs)

            def guarded_copytree(source, destination, *args, **kwargs):
                copies.append(Path(source))
                return original_copytree(source, destination, *args, **kwargs)

            with self._forbidden_access_guards(forbidden), \
                    mock.patch.object(MODULE.shutil, "copy2", guarded_copy2), \
                    mock.patch.object(MODULE.shutil, "copytree", guarded_copytree), \
                    mock.patch.object(MODULE, "sha256_file", wraps=MODULE.sha256_file) as hash_file:
                with self.assertRaisesRegex(RuntimeError, "BLOCKED: tracked SDK provenance manifest"):
                    MODULE.stage_sdk(sdk, stage, dependency_lock=lock)
            self.assertEqual(copies, [])
            self.assertFalse(any(Path(call.args[0]) == sdk or sdk in Path(call.args[0]).parents
                                 for call in hash_file.call_args_list))
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
            repo = Path(temp) / "repo"
            archive = repo / "firmware" / ".target_logging" / "run"
            stage = archive / "stage"
            config = stage / MODULE.STAGE_CONFIG_RELATIVE
            csv = stage / MODULE.STAGE_PARTITIONS_RELATIVE
            config.parent.mkdir(parents=True)
            csv.parent.mkdir(parents=True)
            csv.write_text("csv", encoding="utf-8")
            config.write_text('CONFIG_PARTITION_TABLE_CUSTOM_FILENAME="partitions.csv"\n'
                              'CONFIG_PARTITION_TABLE_FILENAME="partitions.csv"\n', encoding="utf-8")
            token_path = MODULE.write_stage_authorization(archive, archive)
            authorization = MODULE.validate_partition_stage_token(config, csv, archive, token_path, repo)
            MODULE.configure_staged_partition_config(config, csv, authorization=authorization)
            resolved = csv.resolve().as_posix()
            result = config.read_text(encoding="utf-8")
            self.assertIn(f'CONFIG_PARTITION_TABLE_CUSTOM_FILENAME="{resolved}"', result)
            self.assertIn(f'CONFIG_PARTITION_TABLE_FILENAME="{resolved}"', result)
            self.assertNotIn('CONFIG_PARTITION_TABLE_FILENAME="partitions.csv"', result)

    def test_cli_arguments_require_stage_token_for_helper_and_archive_for_build(self):
        helper = MODULE.parse_arguments(["--configure-partition", "sdkconfig", "partitions.csv",
                                         "--archive", "firmware/.target_logging/run",
                                         "--stage-manifest", "firmware/.target_logging/run/stage/runner-stage-token.json"])
        self.assertEqual(helper.configure_partition, ["sdkconfig", "partitions.csv"])
        self.assertEqual(helper.archive, Path("firmware/.target_logging/run"))

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
