# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 the OpenRAC contributors
"""Checks which compiler and linker tools/asm_part.py builds a part with."""
import contextlib
import io
import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import asm_part  # noqa: E402

VARIABLES = ("OPENRAC_PART_ZIG", "OPENRAC_NATIVE_LIBRARY_DIR", "OPENRAC_PART_CLANG", "OPENRAC_PART_LLD")


class Tools(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.env = {k: v for k, v in os.environ.items() if k not in VARIABLES}

    def test_the_apks_zig_is_clang_and_lld(self):
        (self.dir / asm_part.ZIG_LIBRARY).write_bytes(b"")
        with mock.patch.dict(os.environ, {**self.env, "OPENRAC_NATIVE_LIBRARY_DIR": str(self.dir)}, clear=True):
            clang, lld = asm_part.tools()
        zig = str(self.dir / asm_part.ZIG_LIBRARY)
        self.assertEqual((clang, lld), ([zig, "clang"], [zig, "ld.lld"]))

    def test_a_named_zig_comes_first(self):
        (self.dir / asm_part.ZIG_LIBRARY).write_bytes(b"")
        with mock.patch.dict(os.environ, {**self.env, "OPENRAC_PART_ZIG": "/host/zig",
                                          "OPENRAC_NATIVE_LIBRARY_DIR": str(self.dir)}, clear=True):
            self.assertEqual(asm_part.tools()[1], ["/host/zig", "ld.lld"])

    def test_else_a_clang_and_an_lld(self):
        with mock.patch.dict(os.environ, {**self.env, "OPENRAC_PART_CLANG": "/x/clang",
                                          "OPENRAC_PART_LLD": "/x/lld"}, clear=True):
            self.assertEqual(asm_part.tools(), (["/x/clang"], ["/x/lld", "-flavor", "gnu"]))

    def test_an_installers_zig_comes_after_a_named_one(self):
        zig = self.dir / "zig.exe"
        zig.write_bytes(b"")
        with mock.patch.object(asm_part, "ZIG_PROGRAM", zig):
            with mock.patch.dict(os.environ, self.env, clear=True):
                self.assertEqual(asm_part.tools(), ([str(zig), "clang"], [str(zig), "ld.lld"]))
            with mock.patch.dict(os.environ, {**self.env, "OPENRAC_PART_ZIG": "/host/zig"}, clear=True):
                self.assertEqual(asm_part.tools()[0], ["/host/zig", "clang"])


def coff(symbols: list[tuple[str, int, int]]) -> bytes:
    """A COFF object's header, symbols (name, value, section; all external) and string table."""
    table, strings = b"", b""
    for name, value, section in symbols:
        if len(name) <= 8:
            raw = name.encode().ljust(8, b"\0")
        else:
            raw = bytes(4) + struct.pack("<I", 4 + len(strings))
            strings += name.encode() + b"\0"
        table += raw + struct.pack("<IhHBB", value, section, 0, 2, 0)
    return struct.pack("<HHIIIHH", 0x8664, 0, 0, 20, len(symbols), 0, 0) + table + struct.pack(
        "<I", 4 + len(strings)) + strings


class Exports(unittest.TestCase):
    def test_what_objects_use_and_do_not_define(self):
        with tempfile.TemporaryDirectory() as tmp:
            one, two = Path(tmp) / "one.o", Path(tmp) / "two.o"
            one.write_bytes(coff([("func_00115200", 0, 0), ("__imp_openrac_guest_base", 0, 0), ("mine", 16, 1)]))
            two.write_bytes(coff([("mine", 0, 0), ("short", 0, 0), ("defined_here", 0, 2)]))
            self.assertEqual(asm_part.external_symbols([str(one), str(two)]),
                             ["__imp_openrac_guest_base", "func_00115200", "short"])

    def test_functions_are_exported_by_name_and_data_as_data(self):
        self.assertEqual(asm_part.exports_text(["__imp_openrac_vu0", "func_00115200"]),
                         "EXPORTS\n  func_00115200\n  openrac_vu0 DATA\n")

    def test_a_part_on_windows_is_a_dll(self):
        self.assertEqual(asm_part.part_name("rac2-ntsc"), "libopenrac-rac2-ntsc-part.so")
        self.assertEqual(asm_part.part_name("rac2-ntsc", True), "libopenrac-rac2-ntsc-part.dll")

    def test_a_windows_part_links_nothing_but_the_program(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"OPENRAC_PART_ZIG": "/z/zig"}), \
                mock.patch.object(asm_part.subprocess, "run") as run:
            kit, out = Path(tmp) / "kit", Path(tmp) / "part.dll.tmp"
            asm_part.link_windows("rac2-ntsc", ["a.o"], out, kit)
        dlltool, link = [call.args[0] for call in run.call_args_list]
        self.assertEqual(dlltool[:6], ["/z/zig", "dlltool", "-m", "i386:x86-64", "-D", "openrac-rac2-ntsc.exe"])
        self.assertEqual(link[:2], ["/z/zig", "lld-link"])
        for flag in ("/dll", "/noentry", "/nodefaultlib"):
            self.assertIn(flag, link)


class Linux(unittest.TestCase):
    """A Linux launcher's part (SteamOS: no compiler, no headers): Zig's Clang, lld and headers."""

    META = {"format": asm_part.KIT_FORMAT, "game": "rac2-ntsc", "target": "linux", "triple": "x86_64-linux-gnu",
            "glibc_minor": asm_part.LINUX_GLIBC_MINOR, "includes": [["0", "user"], ["5", "system"]]}

    def test_each_processor_has_its_triple_and_zigs_header_folders(self):
        self.assertEqual(asm_part.linux_target("x86_64"), ("x86_64-linux-gnu", [
            "include", "libc/include/x86_64-linux-gnu", "libc/include/generic-glibc",
            "libc/include/x86-linux-any", "libc/include/any-linux-any"]))
        self.assertEqual(asm_part.linux_target("AMD64")[0], "x86_64-linux-gnu")
        triple, folders = asm_part.linux_target("aarch64")
        self.assertEqual(triple, "aarch64-linux-gnu")
        self.assertIn("libc/include/aarch64-linux-any", folders)
        with self.assertRaises(SystemExit):
            asm_part.linux_target("riscv64")

    def test_the_installers_zig_is_named_for_its_system(self):
        self.assertEqual(asm_part.ZIG_PROGRAM.name, "zig.exe" if os.name == "nt" else "zig")
        self.assertEqual(asm_part.ZIG_PROGRAM.parent, asm_part.ROOT / "tools" / "part-zig")

    def test_a_linux_part_is_compiled_with_the_kits_headers_only(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"OPENRAC_PART_ZIG": "/z/zig"}), \
                mock.patch.object(asm_part.subprocess, "run") as run, \
                contextlib.redirect_stdout(io.StringIO()):
            kit = Path(tmp)
            asm_part.compile_all([kit / "a.c"], kit, self.META, None, 1)
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[:3], ["/z/zig", "clang", "--driver-mode=gcc"])
        for flag in ("--target=x86_64-linux-gnu", "-nostdinc", f"-D__GLIBC_MINOR__={asm_part.LINUX_GLIBC_MINOR}",
                     "-fPIC"):
            self.assertIn(flag, cmd)
        self.assertEqual(cmd[cmd.index("-isystem") + 1], str(kit / "include" / "5"))

    def test_a_linux_part_links_nothing_but_its_objects(self):
        # Its names are the program's (cmake/Games.cmake exports them), bound when the game loads it.
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"OPENRAC_PART_ZIG": "/z/zig"}), \
                mock.patch.object(asm_part, "compile_all", return_value=["a.o"]), \
                mock.patch.object(asm_part.subprocess, "run") as run, \
                mock.patch.object(Path, "replace"), \
                contextlib.redirect_stdout(io.StringIO()):
            part = Path(tmp) / "libopenrac-rac2-ntsc-part.so"
            asm_part.compile_and_link("rac2-ntsc", [], part, Path(tmp), self.META, None, 1, None)
        link = run.call_args.args[0]
        self.assertEqual(link[:3], ["/z/zig", "ld.lld", "-shared"])
        self.assertEqual(link[-1], "a.o")
        self.assertIn("-soname=libopenrac-rac2-ntsc-part.so", link)

    def test_a_linux_kit_carries_zigs_headers_and_says_what_it_is_for(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            gen = tmp / "build" / "games" / "rac2-ntsc" / "gen"
            gen.mkdir(parents=True)
            (gen / "program.pickle").write_bytes(b"")
            (gen / "game_protos.h").write_text("")
            zig = tmp / "zig"
            _, folders = asm_part.linux_target("x86_64")
            for folder in folders:
                (zig / "lib" / folder).mkdir(parents=True)
            math = zig / "lib" / "libc/include/generic-glibc" / "math.h"
            math.write_text("")
            deps = f"probe.o: {tmp}/probe.c {gen / 'game_protos.h'} {math}\n"
            with mock.patch.object(asm_part.platform, "machine", return_value="x86_64"), \
                    mock.patch.object(asm_part.subprocess, "run", return_value=mock.Mock(stdout=deps)) as run, \
                    contextlib.redirect_stdout(io.StringIO()):
                asm_part.kit("rac2-ntsc", tmp / "build", tmp / "kit", None, None, linux_zig=zig)
            probe = run.call_args.args[0]
            self.assertEqual(probe[:2], [str(zig / "zig"), "clang"])
            self.assertIn("--target=x86_64-linux-gnu", probe)
            self.assertIn("-nostdinc", probe)
            meta = json.loads((tmp / "kit" / "kit.json").read_text())
            self.assertEqual((meta["target"], meta["triple"], meta["glibc_minor"]),
                             ("linux", "x86_64-linux-gnu", asm_part.LINUX_GLIBC_MINOR))
            # The game's own header under its user folder, glibc's under its system folder (gen is 0;
            # the five user folders come first, then Zig's in order: generic-glibc is the third).
            self.assertTrue((tmp / "kit" / "include" / "0" / "game_protos.h").is_file())
            self.assertTrue((tmp / "kit" / "include" / "7" / "math.h").is_file())
            self.assertIn(["7", "system"], meta["includes"])
            self.assertFalse((tmp / "kit" / asm_part.EXPORTS).exists())

    def test_one_kit_is_for_one_system(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            asm_part.main(["kit", "rac2-ntsc", "--build", ".", "--out", "k", "--windows-zig", "w", "--linux-zig", "l"])


if __name__ == "__main__":
    unittest.main()
