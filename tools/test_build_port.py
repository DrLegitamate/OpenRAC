"""tools/build_port.py's plans, without building anything:
python3 -m unittest discover -s tools"""

import contextlib
import hashlib
import http.client
import os
import io
import sys
import tempfile
import time
import zipfile
import unittest
from pathlib import Path
from unittest import mock

import build_port

CACHE = """# This is the CMakeCache file.
//Path to a program.
CMAKE_C_COMPILER:STRING=C:/LLVM/bin/clang.exe
CMAKE_CXX_COMPILER:STRING=C:/LLVM/bin/clang++.exe
CMAKE_MAKE_PROGRAM:FILEPATH=C:/Ninja/ninja.exe
OPENRAC_RAC1_PAL_SOURCE:PATH=C:/work/rac1-decomp
WITH_EQUALS:STRING=a=b
"""
BUILD = ["cmake", "--build", "--preset", "release", "--parallel", str(build_port.build_jobs()),
         "--target", "openrac-rac1-pal", "openrac-extractor"]

# main() lowers its own priority (background.lower_priority); not the test runner's.
_priority = mock.patch.object(build_port.background, "lower_priority", return_value=True)


def setUpModule():
    _priority.start()


def tearDownModule():
    _priority.stop()


class Reading(unittest.TestCase):
    def test_build_jobs_leave_threads_free_unless_told(self):
        with mock.patch.dict(os.environ, {}, clear=False), mock.patch("os.cpu_count", return_value=8):
            os.environ.pop("OPENRAC_BUILD_JOBS", None)
            self.assertEqual(build_port.build_jobs(), 6)
            for given, jobs in (("all", 8), ("3", 3), ("0", 6), ("many", 6)):
                os.environ["OPENRAC_BUILD_JOBS"] = given
                self.assertEqual(build_port.build_jobs(), jobs, given)

    def test_cmake_cache_entries(self):
        cache = build_port.parse_cache(CACHE)
        self.assertEqual(cache["CMAKE_C_COMPILER"], "C:/LLVM/bin/clang.exe")
        self.assertEqual(cache["OPENRAC_RAC1_PAL_SOURCE"], "C:/work/rac1-decomp")
        self.assertEqual(cache["WITH_EQUALS"], "a=b")
        self.assertEqual(len(cache), 5)

    def test_what_cmd_set_prints(self):
        env = build_port.parse_environment("Path=C:\\a;C:\\b\r\nINCLUDE=C:\\inc\r\n=C:=C:\\\r\n")
        self.assertEqual(env, {"PATH": "C:\\a;C:\\b", "INCLUDE": "C:\\inc"})

    def test_names_follow_the_games(self):
        self.assertEqual(build_port.source_variable("rac1-pal"), "OPENRAC_RAC1_PAL_SOURCE")
        self.assertEqual(build_port.targets(["rac1-pal"]), ["openrac-rac1-pal", "openrac-extractor"])
        self.assertIn("rac1-pal", build_port.games())
        self.assertEqual(build_port.default_source("rac1-pal"), build_port.ROOT / "games" / "rac1")

    def test_clang_is_looked_for_in_order(self):
        env = {"OPENRAC_CLANG": "X:/mine/clang.exe", "PROGRAMFILES": "X:/PF", "USERPROFILE": "X:/me", "PATH": ""}
        found = build_port.clang_candidates(env, Path("X:/VS"))
        self.assertEqual(found[:4], [
            Path("X:/mine/clang.exe"),
            Path("X:/PF/LLVM/bin/clang.exe"),
            Path("X:/me/scoop/apps/llvm/current/bin/clang.exe"),
            Path("X:/VS/VC/Tools/Llvm/x64/bin/clang.exe"),
        ])


class Clean(unittest.TestCase):
    def test_a_read_only_file_is_deleted_too(self):
        # git's objects are read-only, and Windows will not delete those: --clean stopped at a
        # dependency CMake had cloned into the build folder.
        with tempfile.TemporaryDirectory() as tmp:
            build = Path(tmp) / "release"
            pack = build / "_deps" / "cgltf-src" / ".git" / "objects" / "pack"
            pack.mkdir(parents=True)
            index = pack / "pack-0.idx"
            index.write_bytes(b"x")
            index.chmod(0o444)
            build_port.remove_tree(build)
            self.assertFalse(build.exists())


class Commands(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.build = Path(self.tmp.name) / "release"
        self.build.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def plan(self, cache, source=None, compilers=None, configured=True):
        if configured:
            (self.build / "build.ninja").write_text("")
        return build_port.commands("cmake", "release", self.build, ["rac1-pal"], source, cache, compilers)

    def test_a_new_folder_is_configured_from_the_preset_with_the_compilers_found(self):
        steps = self.plan({}, compilers=("C:/LLVM/clang.exe", "C:/LLVM/clang++.exe", "C:/Ninja/ninja.exe"),
                          configured=False)
        self.assertEqual(steps, [
            ["cmake", "--preset", "release", "-DCMAKE_C_COMPILER=C:/LLVM/clang.exe",
             "-DCMAKE_CXX_COMPILER=C:/LLVM/clang++.exe", "-DCMAKE_MAKE_PROGRAM=C:/Ninja/ninja.exe"],
            BUILD,
        ])

    def test_a_new_folder_takes_the_presets_compilers_when_none_are_found(self):
        self.assertEqual(self.plan({}, configured=False), [["cmake", "--preset", "release"], BUILD])

    def test_a_configured_folder_is_only_built(self):
        self.assertEqual(self.plan(build_port.parse_cache(CACHE)), [BUILD])

    def test_the_same_source_again_is_only_built(self):
        cache = build_port.parse_cache(CACHE)
        self.assertEqual(self.plan(cache, source=Path(cache["OPENRAC_RAC1_PAL_SOURCE"])), [BUILD])

    def test_another_source_is_configured_without_the_preset(self):
        steps = self.plan(build_port.parse_cache(CACHE), source=Path("/elsewhere/rac1"))
        self.assertEqual(steps[0][:2], ["cmake", "-S"])
        self.assertNotIn("--preset", steps[0])
        self.assertEqual(steps[0][-1], "-DOPENRAC_RAC1_PAL_SOURCE=" + Path("/elsewhere/rac1").as_posix())
        self.assertEqual(steps[1], BUILD)

    def test_a_new_folder_runs_hostgen_under_this_python(self):
        steps = build_port.commands("cmake", "release", self.build, ["rac1-pal"], None, {}, None, "/usr/bin/python3")
        self.assertEqual(steps[0], ["cmake", "--preset", "release", "-DPython3_EXECUTABLE=/usr/bin/python3"])

    def test_python_is_named_without_the_windows_verbatim_prefix(self):
        self.assertEqual(build_port.plain_path("\\\\?\\D:\\claude code\\python\\python.exe"),
                         Path("D:\\claude code\\python\\python.exe"))
        self.assertEqual(build_port.plain_path("\\\\?\\UNC\\nas\\share\\python.exe"),
                         Path("\\\\nas\\share\\python.exe"))
        self.assertEqual(build_port.plain_path("/usr/bin/python3"), Path("/usr/bin/python3"))

    def test_a_folder_holding_a_verbatim_python_gets_the_plain_one(self):
        # What launcher 1.0.1 left behind: hostgen's `cmd /C` could not start this Python.
        (self.build / "build.ninja").write_text("")
        cache = build_port.parse_cache(CACHE)
        cache["Python3_EXECUTABLE"] = "//?/D:/claude code/python/python.exe"
        steps = build_port.commands("cmake", "release", self.build, ["rac1-pal"], None, cache, None,
                                    "D:/claude code/python/python.exe")
        self.assertEqual(steps, [["cmake", "-S", str(build_port.PORT), "-B", str(self.build),
                                  "-DPython3_EXECUTABLE=D:/claude code/python/python.exe"], BUILD])
        unconfigured = build_port.commands("cmake", "release", self.build / "new", ["rac1-pal"], None,
                                           {"Python3_EXECUTABLE": "//?/D:/python.exe"}, None, "D:/python.exe")
        self.assertIn("-DPython3_EXECUTABLE=D:/python.exe", unconfigured[0])

    def test_more_targets_are_built_with_the_game(self):
        (self.build / "build.ninja").write_text("")
        steps = build_port.commands("cmake", "release", self.build, ["rac1-pal"], None,
                                    build_port.parse_cache(CACHE), more=["runtime_test"])
        self.assertEqual(steps, [BUILD + ["runtime_test"]])

    def test_a_disc_is_kept_for_the_games_translation(self):
        disc = Path("/discs/rac1.iso")
        new = build_port.commands("cmake", "release", self.build, ["rac1-pal"], None, {}, disc=disc)
        self.assertEqual(new[0], ["cmake", "--preset", "release", "-DOPENRAC_RAC1_PAL_RETAIL_IMAGE=" + disc.as_posix()])
        (self.build / "build.ninja").write_text("")
        cache = build_port.parse_cache(CACHE)
        steps = build_port.commands("cmake", "release", self.build, ["rac1-pal"], None, cache, disc=disc)
        self.assertEqual(steps[0], ["cmake", "-S", str(build_port.PORT), "-B", str(self.build),
                                    "-DOPENRAC_RAC1_PAL_RETAIL_IMAGE=" + disc.as_posix()])
        cache["OPENRAC_RAC1_PAL_RETAIL_IMAGE"] = disc.as_posix()
        self.assertEqual(build_port.commands("cmake", "release", self.build, ["rac1-pal"], None, cache, disc=disc),
                         [BUILD])

    def test_an_unfinished_folder_keeps_its_own_compilers(self):
        steps = self.plan(build_port.parse_cache(CACHE), compilers=("other", "other++", "ninja2"), configured=False)
        self.assertEqual(steps[0][:3], ["cmake", "--preset", "release"])
        self.assertIn("-DCMAKE_C_COMPILER=C:/LLVM/bin/clang.exe", steps[0])
        self.assertNotIn("-DCMAKE_C_COMPILER=other", steps[0])

    def test_a_clean_build_keeps_the_folders_choices(self):
        cache = build_port.parse_cache(CACHE)
        cache["OPENRAC_RAC1_PAL_RETAIL_IMAGE"] = "/discs/rac1.iso"
        kept = build_port.kept_choices(cache)
        self.assertEqual(kept, {"OPENRAC_RAC1_PAL_SOURCE": "C:/work/rac1-decomp",
                                "OPENRAC_RAC1_PAL_RETAIL_IMAGE": "/discs/rac1.iso"})
        steps = build_port.commands("cmake", "release", self.build, ["rac1-pal"], None, {}, kept=kept)
        self.assertEqual(steps[0], ["cmake", "--preset", "release", "-DOPENRAC_RAC1_PAL_SOURCE=C:/work/rac1-decomp",
                                    "-DOPENRAC_RAC1_PAL_RETAIL_IMAGE=/discs/rac1.iso"])
        # A --source given with --clean wins over the kept one.
        steps = build_port.commands("cmake", "release", self.build, ["rac1-pal"], Path("/new/rac1"), {}, kept=kept)
        self.assertIn("-DOPENRAC_RAC1_PAL_SOURCE=" + Path("/new/rac1").as_posix(), steps[0])
        self.assertNotIn("-DOPENRAC_RAC1_PAL_SOURCE=C:/work/rac1-decomp", steps[0])


    def test_the_probe_baker_is_kept_and_configured_with_its_embree(self):
        cache = build_port.parse_cache(CACHE)
        cache["OPENRAC_PROBE_BAKER"] = "ON"
        self.assertEqual(build_port.kept_choices(cache)["OPENRAC_PROBE_BAKER"], "ON")
        defines = {"OPENRAC_PROBE_BAKER": "ON", "embree_DIR": "/b/embree/install/lib/cmake/embree-4.4.1"}
        steps = build_port.commands("cmake", "release", self.build, ["rac1-pal"], None, {}, defines=defines,
                                    more=[build_port.BAKER])
        self.assertIn("-DOPENRAC_PROBE_BAKER=ON", steps[0])
        self.assertIn("-Dembree_DIR=/b/embree/install/lib/cmake/embree-4.4.1", steps[0])
        self.assertEqual(steps[-1][-1], "openrac-enhanced-baker")

    def test_embree_is_pinned_and_built_static_without_tbb(self):
        self.assertEqual(len(build_port.EMBREE_SHA256), 64)
        self.assertIn(build_port.EMBREE_VERSION, build_port.EMBREE_URL)
        self.assertEqual(build_port.EMBREE_OPTIONS["EMBREE_STATIC_LIB"], "ON")
        self.assertEqual(build_port.EMBREE_OPTIONS["EMBREE_TASKING_SYSTEM"], "INTERNAL")

    def test_embree_is_given_the_x86_isas_on_x86_only(self):
        # On ARM a value for an x86-64 ISA still counts as one more ISA, and AppleClang's static
        # Embree takes only one: the macOS launcher's baker was left out that way.
        for machine in ("x86_64", "AMD64"):
            self.assertEqual(build_port.embree_options(machine)["EMBREE_ISA_AVX"], "ON")
        for machine in ("arm64", "aarch64", "ARM64"):
            options = build_port.embree_options(machine)
            self.assertFalse([key for key in options if key.startswith("EMBREE_ISA_")], machine)
            self.assertEqual(options["EMBREE_STATIC_LIB"], "ON")

    def test_embree_is_compiled_with_the_builds_cxxflags(self):
        # The launcher's Linux builds put -stdlib=libc++ in CXXFLAGS; Embree must not drop it, and
        # an Embree built with other flags is built again.
        self.assertEqual(build_port.EMBREE_OPTIONS["EMBREE_IGNORE_CMAKE_CXX_FLAGS"], "OFF")
        self.assertNotEqual(build_port.embree_stamp({"CXXFLAGS": "-stdlib=libc++"}), build_port.embree_stamp({}))


class ProbeBaker(unittest.TestCase):
    """--probe-baker: the baker with its Embree, except on an Intel Mac, which ships without it."""

    def test_only_an_intel_mac_ships_without_the_baker(self):
        self.assertFalse(build_port.ships_baker("Darwin", "x86_64"))
        self.assertTrue(build_port.ships_baker("Darwin", "arm64"))
        for system in ("Linux", "Windows"):
            for machine in ("x86_64", "AMD64", "aarch64"):
                self.assertTrue(build_port.ships_baker(system, machine), (system, machine))

    def test_this_computer_is_read_when_none_is_given(self):
        with mock.patch.object(build_port.platform, "system", return_value="Darwin"), \
                mock.patch.object(build_port.platform, "machine", return_value="x86_64"):
            self.assertFalse(build_port.ships_baker())
        with mock.patch.object(build_port.platform, "system", return_value="Darwin"), \
                mock.patch.object(build_port.platform, "machine", return_value="arm64"):
            self.assertTrue(build_port.ships_baker())

    def baker_on(self, system, machine, embree=None):
        """probe_baker on SYSTEM and MACHINE, with build_embree mocked: its result, what it said,
        and the mock."""
        out = io.StringIO()
        build = mock.Mock(return_value=Path("/b/embree/install/lib/cmake/embree-4.4.1"), side_effect=embree)
        with mock.patch.object(build_port.platform, "system", return_value=system), \
                mock.patch.object(build_port.platform, "machine", return_value=machine), \
                mock.patch.object(build_port, "build_embree", build), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            result = build_port.probe_baker("cmake", {}, None, {})
        return result, out.getvalue(), build

    def test_an_intel_mac_neither_downloads_nor_builds_embree_and_says_why(self):
        (defines, more), said, build = self.baker_on("Darwin", "x86_64")
        build.assert_not_called()
        self.assertEqual((defines, more), ({"OPENRAC_PROBE_BAKER": "OFF"}, []))
        self.assertEqual(said.strip(), build_port.NO_BAKER_HERE)

    def test_elsewhere_the_baker_is_built_with_its_embree(self):
        (defines, more), said, build = self.baker_on("Darwin", "arm64")
        build.assert_called_once()
        self.assertEqual(defines, {"OPENRAC_PROBE_BAKER": "ON", "embree_DIR": "/b/embree/install/lib/cmake/embree-4.4.1"})
        self.assertEqual(more, [build_port.BAKER])
        self.assertNotIn(build_port.NO_BAKER_HERE, said)

    def test_an_embree_that_fails_leaves_the_baker_out(self):
        (defines, more), _, _ = self.baker_on("Linux", "x86_64", embree=build_port.BuildError("no network"))
        self.assertEqual((defines, more), ({"OPENRAC_PROBE_BAKER": "OFF"}, []))

    def test_an_intel_mac_builds_the_rest_and_succeeds(self):
        # The whole command line, its tools mocked: the game and the extractor are configured and
        # built without the baker, and the build exits 0.
        run = mock.Mock(return_value=mock.Mock(returncode=0))
        build = mock.Mock()
        out = io.StringIO()
        with mock.patch.object(build_port.platform, "system", return_value="Darwin"), \
                mock.patch.object(build_port.platform, "machine", return_value="x86_64"), \
                mock.patch.object(build_port, "read_cache", return_value={}), \
                mock.patch.object(build_port, "prepare", return_value=({}, Path("cmake"), None)), \
                mock.patch.object(build_port, "build_embree", build), \
                mock.patch.object(build_port, "write_stamp"), \
                mock.patch.object(build_port.subprocess, "run", run), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            status = build_port.main(["rac1-pal", "--probe-baker"])
        self.assertEqual(status, 0)
        build.assert_not_called()
        self.assertIn(build_port.NO_BAKER_HERE, out.getvalue())
        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn("-DOPENRAC_PROBE_BAKER=OFF", commands[0])
        built = commands[-1]
        self.assertIn("openrac-rac1-pal", built)
        self.assertIn("openrac-extractor", built)
        self.assertNotIn(build_port.BAKER, built)


class Stamp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.build = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def read(self):
        import json
        return json.loads((self.build / "openrac-build.json").read_text(encoding="utf-8"))

    def test_a_build_records_its_version_commit_and_games(self):
        build_port.write_stamp(self.build, ["rac1-pal"])
        stamp = self.read()
        self.assertEqual(stamp["version"], build_port.checkout_version())
        self.assertEqual(stamp["commit"], build_port.checkout_commit())
        self.assertEqual(stamp["games"], ["rac1-pal"])
        # Another game built from the same checkout joins it.
        build_port.write_stamp(self.build, ["rac1-ntsc"])
        self.assertEqual(self.read()["games"], ["rac1-ntsc", "rac1-pal"])

    def test_a_ci_build_records_its_repository(self):
        with mock.patch.dict(build_port.os.environ, {"GITHUB_REPOSITORY": "someone/OpenRAC"}):
            build_port.write_stamp(self.build, ["rac2-ntsc"])
        self.assertEqual(self.read()["repository"], "someone/OpenRAC")
        with mock.patch.dict(build_port.os.environ, {"GITHUB_REPOSITORY": "not a repository"}):
            build_port.write_stamp(self.build, ["rac2-ntsc"])
        self.assertNotIn("repository", self.read())

    def test_a_build_from_another_checkout_starts_the_list_again(self):
        (self.build / "openrac-build.json").write_text('{"version": "0.0.1", "commit": null, "games": ["rac1-pal"]}')
        build_port.write_stamp(self.build, ["rac1-ntsc"])
        self.assertEqual(self.read()["games"], ["rac1-ntsc"])


class CommandLine(unittest.TestCase):
    def test_tracy_modes_override_cached_profiling_options(self):
        for flags, expected in (([], ("OFF", "OFF", "OFF")),
                                (["--tracy"], ("ON", "OFF", "OFF")),
                                (["--tracy-system"], ("ON", "ON", "OFF")),
                                (["--tracy-sampling"], ("ON", "ON", "ON"))):
            with self.subTest(flags=flags), \
                    mock.patch.object(build_port, "read_cache", return_value={"OPENRAC_TRACY": "ON"}), \
                    mock.patch.object(build_port, "prepare", return_value=({}, Path("cmake"), None)), \
                    mock.patch.object(build_port, "write_stamp"), \
                    mock.patch.object(build_port.subprocess, "run", return_value=mock.Mock(returncode=0)) as run, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(build_port.main(["rac1-pal", *flags]), 0)
                configure = run.call_args_list[0].args[0]
                resolved = {"OPENRAC_TRACY": "ON"}
                resolved.update(part[2:].split("=", 1) for part in configure if part.startswith("-D"))
                for key, value in zip(("OPENRAC_TRACY", "OPENRAC_TRACY_SYSTEM", "OPENRAC_TRACY_SAMPLING"), expected):
                    self.assertEqual(resolved[key], value)

    def refused(self, argv):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            build_port.main(argv)

    def test_an_unknown_game_is_refused(self):
        self.refused(["rac9-pal"])

    def test_a_source_for_two_games_is_refused(self):
        self.refused(["rac1-pal", "rac1-ntsc", "--source", "."])

    def test_a_disc_for_two_games_or_none_is_refused(self):
        self.refused(["rac1-pal", "rac1-ntsc", "--disc", __file__])
        self.refused(["rac1-pal", "--disc", "/no/such/disc.iso"])


# The real pins, before a test replaces them with a fake archive's.
PINNED_NINJA = dict(build_port.NINJA_RELEASES)
FAKE_NINJA = b"\x7fELF not really ninja"


def ninja_zip(program: bytes = FAKE_NINJA) -> bytes:
    """A release archive as Ninja's: one file, ninja."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zipped:
        zipped.writestr("ninja", program)
    return out.getvalue()


class Response(io.BytesIO):
    """What urlopen returns, as a context manager; FAIL_AFTER bytes in, the connection drops."""

    def __init__(self, data: bytes, fail_after: int | None = None):
        super().__init__(data)
        self.fail_after = fail_after

    def read(self, size=-1):
        if self.fail_after is not None and self.tell() >= self.fail_after:
            raise http.client.IncompleteRead(b"", 1)
        if self.fail_after is not None and size < 0:
            size = self.fail_after - self.tell()
        return super().read(size)


class ProvideNinja(unittest.TestCase):
    """Ninja for a Linux computer without one (SteamOS): never the network for real."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cache = self.root / "cache"
        self.archive = ninja_zip()
        self.releases = {"x86_64": ("ninja-linux.zip", hashlib.sha256(self.archive).hexdigest(),
                                    hashlib.sha256(FAKE_NINJA).hexdigest()),
                         "aarch64": ("ninja-linux-aarch64.zip", "0" * 64, "0" * 64)}
        for patch in (mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.cache)}),
                      mock.patch.object(build_port, "NINJA_RELEASES", self.releases),
                      mock.patch.object(build_port.sys, "platform", "linux"),
                      mock.patch.object(build_port, "ninja_runs", return_value=None),
                      contextlib.redirect_stdout(io.StringIO())):
            patch.__enter__()
            self.addCleanup(patch.__exit__, None, None, None)
        self.folder = self.cache / "openrac" / "toolchains" / "ninja" / build_port.NINJA_VERSION / "x86_64"

    def tearDown(self):
        self.tmp.cleanup()

    def download(self, *responses):
        return mock.patch.object(build_port.urllib.request, "urlopen", side_effect=list(responses))

    def leftovers(self):
        return list(self.folder.glob(".download-*")) if self.folder.exists() else []

    def test_the_pinned_release_is_official_and_checked(self):
        self.assertEqual(build_port.NINJA_URL.format(version="1.13.2", archive="ninja-linux.zip"),
                         "https://github.com/ninja-build/ninja/releases/download/v1.13.2/ninja-linux.zip")
        self.assertEqual(set(PINNED_NINJA), {"x86_64", "aarch64"})
        for machine, (archive, archive_sha, program_sha) in PINNED_NINJA.items():
            self.assertTrue(archive.startswith("ninja-linux"), machine)
            self.assertRegex(archive_sha, "^[0-9a-f]{64}$")
            self.assertRegex(program_sha, "^[0-9a-f]{64}$")

    def test_a_missing_ninja_is_downloaded_into_the_users_cache(self):
        with self.download(Response(self.archive)) as urlopen:
            program = build_port.provide_ninja("x86_64")
        self.assertEqual(program, self.folder / "ninja")
        self.assertEqual(program.read_bytes(), FAKE_NINJA)
        self.assertTrue(os.access(program, os.X_OK))
        self.assertIn("/v1.13.2/ninja-linux.zip", urlopen.call_args.args[0])
        self.assertEqual(self.leftovers(), [])

    def test_a_whole_cached_ninja_is_used_again_without_the_network(self):
        with self.download(Response(self.archive)):
            build_port.provide_ninja("x86_64")
        (self.folder / "ninja").chmod(0o644)  # a copy that lost its executable bit is given it back
        with self.download() as urlopen:
            program = build_port.provide_ninja("x86_64")
        urlopen.assert_not_called()
        self.assertTrue(os.access(program, os.X_OK))

    def test_a_corrupted_cached_ninja_is_downloaded_again(self):
        self.folder.mkdir(parents=True)
        (self.folder / "ninja").write_bytes(b"half a ninja")
        with self.download(Response(self.archive)) as urlopen:
            program = build_port.provide_ninja("x86_64")
        urlopen.assert_called_once()
        self.assertEqual(program.read_bytes(), FAKE_NINJA)

    def test_an_archive_with_another_checksum_is_not_used(self):
        with self.download(Response(ninja_zip(b"something else"))), self.assertRaises(build_port.BuildError) as e:
            build_port.provide_ninja("x86_64")
        self.assertIn("SHA-256", str(e.exception))
        self.assertIn("internet connection", str(e.exception))
        self.assertFalse((self.folder / "ninja").exists())
        self.assertEqual(self.leftovers(), [])

    def test_a_program_with_another_checksum_is_not_used(self):
        other = ninja_zip(b"something else")
        self.releases["x86_64"] = ("ninja-linux.zip", hashlib.sha256(other).hexdigest(), "0" * 64)
        with self.download(Response(other)), self.assertRaises(build_port.BuildError):
            build_port.provide_ninja("x86_64")
        self.assertFalse((self.folder / "ninja").exists())

    def test_an_interrupted_download_leaves_nothing_and_says_so(self):
        for response in (Response(self.archive, fail_after=100),
                         ConnectionResetError("connection reset"),
                         build_port.urllib.error.URLError("no network")):
            with self.subTest(response=response), self.download(response), \
                    self.assertRaises(build_port.BuildError) as e:
                build_port.provide_ninja("x86_64")
            self.assertIn("could not be set up", str(e.exception))
            self.assertFalse((self.folder / "ninja").exists())
            self.assertEqual(self.leftovers(), [])

    def test_a_download_a_stopped_build_left_is_cleared_later(self):
        stale = self.folder / ".download-old"
        fresh = self.folder / ".download-new"
        for folder in (stale, fresh):
            folder.mkdir(parents=True)
            (folder / "ninja-linux.zip").write_bytes(b"half")
        old = time.time() - build_port.NINJA_STALE_SECONDS - 10
        os.utime(stale, (old, old))
        with self.download(Response(self.archive)):
            build_port.provide_ninja("x86_64")
        self.assertFalse(stale.exists())
        self.assertTrue(fresh.exists())  # perhaps another build's, still downloading

    def test_another_processor_is_refused_without_the_network(self):
        with self.download() as urlopen, self.assertRaises(build_port.BuildError) as e:
            build_port.provide_ninja("riscv64")
        urlopen.assert_not_called()
        self.assertIn("riscv64", str(e.exception))

    def test_the_processors_other_names_are_understood(self):
        with self.download(Response(self.archive)):
            self.assertEqual(build_port.provide_ninja("AMD64").parent.name, "x86_64")
        with self.download(Response(self.archive)) as urlopen, self.assertRaises(build_port.BuildError):
            build_port.provide_ninja("arm64")  # aarch64's download, whose pin the fake does not match
        urlopen.assert_called_once()
        self.assertIn("ninja-linux-aarch64.zip", urlopen.call_args.args[0])

    def test_macos_and_windows_are_told_to_install_it_as_before(self):
        for system in ("darwin", "win32"):
            with self.subTest(system=system), mock.patch.object(build_port.sys, "platform", system), \
                    self.download() as urlopen, self.assertRaises(build_port.BuildError) as e:
                build_port.provide_ninja("x86_64")
            urlopen.assert_not_called()
            self.assertEqual(str(e.exception), build_port.NO_NINJA)

    def test_the_read_only_appimage_is_never_written(self):
        appimage = self.root / ".mount_OpenRAC" / "usr" / "lib" / "OpenRAC Launcher" / "openrac"
        (appimage / "python" / "bin").mkdir(parents=True)
        (appimage / "python" / "bin" / "python3").write_bytes(b"")
        before = sorted(p.relative_to(appimage) for p in appimage.rglob("*"))
        appimage.chmod(0o555)
        try:
            with mock.patch.object(build_port.sys, "executable", str(appimage / "python" / "bin" / "python3")), \
                    mock.patch.object(build_port, "ROOT", appimage), self.download(Response(self.archive)):
                program = build_port.provide_ninja("x86_64")
        finally:
            appimage.chmod(0o755)
        self.assertEqual(sorted(p.relative_to(appimage) for p in appimage.rglob("*")), before)
        self.assertTrue(program.is_relative_to(self.cache))

    def test_a_cache_that_cannot_be_written_says_what_to_do(self):
        blocked = self.root / "not-a-folder"
        blocked.write_text("")
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(blocked)}), self.download() as urlopen, \
                self.assertRaises(build_port.BuildError) as e:
            build_port.provide_ninja("x86_64")
        urlopen.assert_not_called()
        self.assertIn(str(blocked), str(e.exception))
        self.assertIn("ninja program on PATH", str(e.exception))

    def test_without_a_home_folder_it_says_what_to_do(self):
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": ""}), \
                mock.patch.object(build_port.Path, "home", side_effect=RuntimeError("no home")), \
                self.assertRaises(build_port.BuildError) as e:
            build_port.provide_ninja("x86_64")
        self.assertIn("XDG_CACHE_HOME", str(e.exception))

    def test_a_relative_xdg_cache_home_is_ignored(self):
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": "relative"}), \
                mock.patch.object(build_port.Path, "home", return_value=self.root / "home"):
            self.assertEqual(build_port.ninja_folder("x86_64"),
                             self.root / "home" / ".cache" / "openrac" / "toolchains" / "ninja"
                             / build_port.NINJA_VERSION / "x86_64")

    def test_a_cached_ninja_that_does_not_run_is_reported(self):
        self.folder.mkdir(parents=True)
        (self.folder / "ninja").write_bytes(FAKE_NINJA)
        with mock.patch.object(build_port, "ninja_runs", return_value="Exec format error"), \
                self.assertRaises(build_port.BuildError) as e:
            build_port.provide_ninja("x86_64")
        self.assertIn("Exec format error", str(e.exception))


class NinjaOnPath(unittest.TestCase):
    """Which Ninja a build on Linux or macOS runs with, and that every command it starts sees it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.system = self.root / "usr-bin"
        self.system.mkdir()
        self.provided = self.root / "cache" / "ninja"
        self.provided.mkdir(parents=True)
        (self.provided / "ninja").write_bytes(b"")

    def tearDown(self):
        self.tmp.cleanup()

    def found(self, ninja_on_path: bool):
        def find(name, *paths):
            if name == "ninja":
                return self.system / "ninja" if ninja_on_path and str(self.system) in paths else None
            return Path(f"/usr/bin/{name}")
        return mock.patch.object(build_port, "find_program", side_effect=find)

    def prepare(self, cache, ninja_on_path):
        with mock.patch.object(build_port, "WINDOWS", False), \
                mock.patch.dict(os.environ, {"PATH": str(self.system)}), self.found(ninja_on_path), \
                mock.patch.object(build_port, "find_cmake", return_value=Path("/usr/bin/cmake")), \
                mock.patch.object(build_port, "provide_ninja", return_value=self.provided / "ninja") as provide:
            env, _, compilers = build_port.prepare(cache)
        return env, compilers, provide

    def test_the_systems_ninja_is_used_without_a_download(self):
        env, compilers, provide = self.prepare({}, ninja_on_path=True)
        provide.assert_not_called()
        self.assertEqual(env["PATH"], str(self.system))
        self.assertIsNone(compilers)

    def test_a_new_folder_without_ninja_on_path_gets_the_provided_one_first_on_path(self):
        env, _, provide = self.prepare({}, ninja_on_path=False)
        provide.assert_called_once()
        self.assertEqual(env["PATH"].split(os.pathsep), [str(self.provided), str(self.system)])

    def test_a_folder_configured_with_the_provided_ninja_finds_it_without_a_download(self):
        cache = {"CMAKE_MAKE_PROGRAM": (self.provided / "ninja").as_posix()}
        env, _, provide = self.prepare(cache, ninja_on_path=False)
        provide.assert_not_called()
        self.assertEqual(env["PATH"].split(os.pathsep)[0], str(self.provided))

    def test_a_folder_whose_ninja_is_gone_gets_one_again(self):
        env, _, provide = self.prepare({"CMAKE_MAKE_PROGRAM": "/tmp/.mount_old/ninja"}, ninja_on_path=False)
        provide.assert_called_once()
        self.assertEqual(env["PATH"].split(os.pathsep)[0], str(self.provided))

    def test_every_command_runs_with_the_provided_ninja_on_path(self):
        env = {"PATH": os.pathsep.join([str(self.provided), "/usr/bin"])}
        with mock.patch.object(build_port, "WINDOWS", False), \
                mock.patch.object(build_port, "read_cache", return_value={}), \
                mock.patch.object(build_port, "prepare", return_value=(env, Path("cmake"), None)), \
                mock.patch.object(build_port, "write_stamp"), \
                mock.patch.object(build_port.subprocess, "run", return_value=mock.Mock(returncode=0)) as run, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(build_port.main(["rac1-pal"]), 0)
        self.assertEqual(len(run.call_args_list), 2)  # configure, build
        for call in run.call_args_list:
            self.assertEqual(call.kwargs["env"]["PATH"].split(os.pathsep)[0], str(self.provided))

    def test_a_failure_to_provide_ninja_stops_the_build_with_its_reason(self):
        err = io.StringIO()
        with mock.patch.object(build_port, "read_cache", return_value={}), \
                mock.patch.object(build_port, "prepare", side_effect=build_port.BuildError("no network: why")), \
                mock.patch.object(build_port.subprocess, "run") as run, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            self.assertEqual(build_port.main(["rac1-pal"]), 1)
        run.assert_not_called()
        self.assertIn("error: no network: why", err.getvalue())


class GonePrograms(unittest.TestCase):
    """A folder configured from an earlier AppImage mount names programs that are gone."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ninja = self.root / "ninja"
        self.ninja.write_bytes(b"")
        self.python = self.root / "python3"
        self.python.write_bytes(b"")
        self.build = self.root / "build" / "release"
        self.build.mkdir(parents=True)
        (self.build / "build.ninja").write_text("")

    def tearDown(self):
        self.tmp.cleanup()

    def test_programs_still_there_are_kept(self):
        cache = {"CMAKE_MAKE_PROGRAM": self.ninja.as_posix(), "Python3_EXECUTABLE": self.python.as_posix()}
        self.assertEqual(build_port.gone_programs(cache, {"PATH": str(self.root)}, "/new/python3"), {})

    def test_gone_programs_are_replaced(self):
        cache = {"CMAKE_MAKE_PROGRAM": "/tmp/.mount_old/ninja", "Python3_EXECUTABLE": "/tmp/.mount_old/python3"}
        with mock.patch.object(build_port, "find_program", return_value=self.ninja):
            gone = build_port.gone_programs(cache, {"PATH": str(self.root)}, "/tmp/.mount_new/python3")
        self.assertEqual(gone, {"CMAKE_MAKE_PROGRAM": self.ninja.as_posix(),
                                "Python3_EXECUTABLE": "/tmp/.mount_new/python3"})

    def run_main(self, cache, windows=False):
        env = {"PATH": str(self.root)}
        with mock.patch.object(build_port, "WINDOWS", windows), \
                mock.patch.object(build_port, "PORT", self.root), \
                mock.patch.object(build_port, "ROOT", self.root), \
                mock.patch.object(build_port, "read_cache", return_value=cache), \
                mock.patch.object(build_port, "prepare", return_value=(env, Path("cmake"), None)), \
                mock.patch.object(build_port, "find_program", return_value=self.ninja), \
                mock.patch.object(build_port, "write_stamp"), \
                mock.patch.object(build_port.sys, "executable", "/tmp/.mount_new/python3"), \
                mock.patch.object(build_port.subprocess, "run", return_value=mock.Mock(returncode=0)) as run, \
                contextlib.redirect_stdout(io.StringIO()):
            with mock.patch.object(build_port, "games", return_value=["rac1-pal"]), \
                    mock.patch.object(build_port, "default_source", return_value=self.root):
                self.assertEqual(build_port.main(["rac1-pal"]), 0)
        return [call.args[0] for call in run.call_args_list]

    def test_a_configured_folder_is_configured_again_with_the_new_programs(self):
        cache = {"CMAKE_MAKE_PROGRAM": "/tmp/.mount_old/ninja", "Python3_EXECUTABLE": "/tmp/.mount_old/python3",
                 "OPENRAC_TRACY": "OFF", "OPENRAC_TRACY_SYSTEM": "OFF", "OPENRAC_TRACY_SAMPLING": "OFF"}
        configure = self.run_main(cache)[0]
        self.assertEqual(configure[:5], ["cmake", "-S", str(self.root), "-B", str(self.root / "build" / "release")])
        self.assertIn(f"-DCMAKE_MAKE_PROGRAM={self.ninja.as_posix()}", configure)
        self.assertIn("-DPython3_EXECUTABLE=/tmp/.mount_new/python3", configure)

    def test_windows_keeps_its_folders_programs_as_before(self):
        cache = {"CMAKE_MAKE_PROGRAM": "C:/gone/ninja.exe", "OPENRAC_TRACY": "OFF",
                 "OPENRAC_TRACY_SYSTEM": "OFF", "OPENRAC_TRACY_SAMPLING": "OFF"}
        commands = self.run_main(cache, windows=True)
        self.assertEqual(len(commands), 1)  # only built, as before
        self.assertNotIn("-DCMAKE_MAKE_PROGRAM", " ".join(commands[0]))

    def test_a_new_folder_names_the_new_python_once(self):
        cache = {"Python3_EXECUTABLE": "/tmp/.mount_old/python3"}
        (self.build / "build.ninja").unlink()
        configure = self.run_main(cache)[0]
        self.assertEqual(configure[:3], ["cmake", "--preset", "release"])
        self.assertEqual([p for p in configure if p.startswith("-DPython3_EXECUTABLE=")],
                         ["-DPython3_EXECUTABLE=/tmp/.mount_new/python3"])


class WindowsNinja(unittest.TestCase):
    def test_windows_still_asks_for_ninja_and_downloads_nothing(self):
        with mock.patch.object(build_port, "WINDOWS", True), \
                mock.patch.object(build_port, "find_visual_studio", return_value=Path("X:/VS")), \
                mock.patch.object(build_port, "msvc_environment", return_value={"PATH": ""}), \
                mock.patch.object(build_port, "find_clang",
                                  return_value=(Path("X:/clang.exe"), Path("X:/clang++.exe"))), \
                mock.patch.object(build_port, "find_program", return_value=None), \
                mock.patch.object(build_port, "provide_ninja") as provide, \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaises(build_port.BuildError) as e:
            build_port.prepare({})
        provide.assert_not_called()
        self.assertEqual(str(e.exception), build_port.NO_NINJA)


if __name__ == "__main__":
    unittest.main()
