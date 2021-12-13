# -*- coding: utf-8 -*-
#
#
#######################################################################################
# GEF - Multi-Architecture GDB Enhanced Features for Exploiters & Reverse-Engineers
#
# by  @_hugsy_
#######################################################################################
#
# GEF is a kick-ass set of commands for X86, ARM, MIPS, PowerPC and SPARC to
# make GDB cool again for exploit dev. It is aimed to be used mostly by exploit
# devs and reversers, to provides additional features to GDB using the Python
# API to assist during the process of dynamic analysis.
#
# GEF fully relies on GDB API and other Linux-specific sources of information
# (such as /proc/<pid>). As a consequence, some of the features might not work
# on custom or hardened systems such as GrSec.
#
# Since January 2020, GEF solely support GDB compiled with Python3 and was tested on
#   * x86-32 & x86-64
#   * arm v5,v6,v7
#   * aarch64 (armv8)
#   * mips & mips64
#   * powerpc & powerpc64
#   * sparc & sparc64(v9)
#
# For GEF with Python2 (only) support was moved to the GEF-Legacy
# (https://github.com/hugsy/gef-legacy)
#
# To start: in gdb, type `source /path/to/gef.py`
#
#######################################################################################
#
# gef is distributed under the MIT License (MIT)
# Copyright (c) 2013-2021 crazy rabbidz
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import abc
import argparse
import binascii
import codecs
import collections
import ctypes
import enum
import functools
import hashlib
import importlib
import inspect
import itertools
import json
import os
import pathlib
import platform
import re
import shutil
import site
import socket
import string
import struct
import subprocess
import sys
import tempfile
import time
import traceback
import configparser
import xmlrpc.client as xmlrpclib
import warnings

from functools import lru_cache
from io import StringIO
from urllib.request import urlopen

LEFT_ARROW = " \u2190 "
RIGHT_ARROW = " \u2192 "
DOWN_ARROW = "\u21b3"
HORIZONTAL_LINE = "\u2500"
VERTICAL_LINE = "\u2502"
CROSS = "\u2718 "
TICK = "\u2713 "
BP_GLYPH = "\u25cf"
GEF_PROMPT = "gef\u27a4  "
GEF_PROMPT_ON = "\001\033[1;32m\002{0:s}\001\033[0m\002".format(GEF_PROMPT)
GEF_PROMPT_OFF = "\001\033[1;31m\002{0:s}\001\033[0m\002".format(GEF_PROMPT)


def http_get(url):
    """Basic HTTP wrapper for GET request. Return the body of the page if HTTP code is OK,
    otherwise return None."""
    try:
        http = urlopen(url)
        if http.getcode() != 200:
            return None
        return http.read()
    except Exception:
        return None


def update_gef(argv):
    """Try to update `gef` to the latest version pushed on GitHub master branch.
    Return 0 on success, 1 on failure. """
    ver = "dev" if "--dev" in argv[2:] else "master"
    latest_gef_data = http_get("https://raw.githubusercontent.com/hugsy/gef/{}/scripts/gef.sh".format(ver,))
    if latest_gef_data is None:
        print("[-] Failed to get remote gef")
        return 1

    fd, fname = tempfile.mkstemp(suffix=".sh")
    os.write(fd, latest_gef_data)
    os.close(fd)
    retcode = subprocess.run(["bash", fname, ver], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
    os.unlink(fname)
    return retcode


try:
    import gdb # pylint: disable=
except ImportError:
    # if out of gdb, the only action allowed is to update gef.py
    if len(sys.argv) == 2 and sys.argv[1].lower() in ("--update", "--upgrade"):
        sys.exit(update_gef(sys.argv))
    print("[-] gef cannot run as standalone")
    sys.exit(0)

gef                                    = None
__commands__                           = []
__functions__                          = []
__aliases__                            = []
__watches__                            = {}
__infos_files__                        = []
__gef_convenience_vars_index__         = 0
__context_messages__                   = []
__heap_allocated_list__                = []
__heap_freed_list__                    = []
__heap_uaf_watchpoints__               = []
__pie_breakpoints__                    = {}
__pie_counter__                        = 1
__gef_remote__                         = None
__gef_qemu_mode__                      = False
# __gef_current_arena__                  = "main_arena"
__gef_int_stream_buffer__              = None
__gef_redirect_output_fd__             = None

DEFAULT_PAGE_ALIGN_SHIFT               = 12
DEFAULT_PAGE_SIZE                      = 1 << DEFAULT_PAGE_ALIGN_SHIFT
GEF_RC                                 = os.getenv("GEF_RC") or os.path.join(os.getenv("HOME"), ".gef.rc")
GEF_TEMP_DIR                           = os.path.join(tempfile.gettempdir(), "gef")
GEF_MAX_STRING_LENGTH                  = 50

GDB_MIN_VERSION                        = (8, 0)
PYTHON_MIN_VERSION                     = (3, 6)
GDB_VERSION                            = tuple(map(int, re.search(r"(\d+)[^\d]+(\d+)", gdb.VERSION).groups()))
PYTHON_VERSION                         = sys.version_info[0:2]

LIBC_HEAP_MAIN_ARENA_DEFAULT_NAME      = "main_arena"

libc_args_definitions = {}

highlight_table = {}
ANSI_SPLIT_RE = r"(\033\[[\d;]*m)"


def reset_all_caches():
    """Free all caches. If an object is cached, it will have a callable attribute `cache_clear`
    which will be invoked to purge the function cache."""
    for mod in dir(sys.modules["__main__"]):
        obj = getattr(sys.modules["__main__"], mod)
        if hasattr(obj, "cache_clear"):
            obj.cache_clear()

    gef.heap.selected_arena = None
    return


def highlight_text(text):
    """
    Highlight text using highlight_table { match -> color } settings.

    If RegEx is enabled it will create a match group around all items in the
    highlight_table and wrap the specified color in the highlight_table
    around those matches.

    If RegEx is disabled, split by ANSI codes and 'colorify' each match found
    within the specified string.
    """
    if not highlight_table:
        return text

    if gef.config["highlight.regex"]:
        for match, color in highlight_table.items():
            text = re.sub("(" + match + ")", Color.colorify("\\1", color), text)
        return text

    ansiSplit = re.split(ANSI_SPLIT_RE, text)

    for match, color in highlight_table.items():
        for index, val in enumerate(ansiSplit):
            found = val.find(match)
            if found > -1:
                ansiSplit[index] = val.replace(match, Color.colorify(match, color))
                break
        text = "".join(ansiSplit)
        ansiSplit = re.split(ANSI_SPLIT_RE, text)

    return "".join(ansiSplit)


def gef_print(x="", *args, **kwargs):
    """Wrapper around print(), using string buffering feature."""
    x = highlight_text(x)
    if __gef_int_stream_buffer__ and not is_debug():
        return __gef_int_stream_buffer__.write(x + kwargs.get("end", "\n"))
    return print(x, *args, **kwargs)


def bufferize(f):
    """Store the content to be printed for a function in memory, and flush it on function exit."""

    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        global __gef_int_stream_buffer__, __gef_redirect_output_fd__

        if __gef_int_stream_buffer__:
            return f(*args, **kwargs)

        __gef_int_stream_buffer__ = StringIO()
        try:
            rv = f(*args, **kwargs)
        finally:
            redirect = gef.config["context.redirect"]
            if redirect.startswith("/dev/pts/"):
                if not __gef_redirect_output_fd__:
                    # if the FD has never been open, open it
                    fd = open(redirect, "wt")
                    __gef_redirect_output_fd__ = fd
                elif redirect != __gef_redirect_output_fd__.name:
                    # if the user has changed the redirect setting during runtime, update the state
                    __gef_redirect_output_fd__.close()
                    fd = open(redirect, "wt")
                    __gef_redirect_output_fd__ = fd
                else:
                    # otherwise, keep using it
                    fd = __gef_redirect_output_fd__
            else:
                fd = sys.stdout
                __gef_redirect_output_fd__ = None

            if __gef_redirect_output_fd__ and fd.closed:
                # if the tty was closed, revert back to stdout
                fd = sys.stdout
                __gef_redirect_output_fd__ = None
                gef.config["context.redirect"] = ""

            fd.write(__gef_int_stream_buffer__.getvalue())
            fd.flush()
            __gef_int_stream_buffer__ = None
        return rv

    return wrapper

#
# Helpers
#

def p8(x: int, s: bool = False) -> bytes:
    """Pack one byte respecting the current architecture endianness."""
    return struct.pack("{}B".format(endian_str()), x) if not s else struct.pack("{}b".format(endian_str()), x)

def p16(x: int, s: bool = False) -> bytes:
    """Pack one word respecting the current architecture endianness."""
    return struct.pack("{}H".format(endian_str()), x) if not s else struct.pack("{}h".format(endian_str()), x)

def p32(x: int, s: bool = False) -> bytes:
    """Pack one dword respecting the current architecture endianness."""
    return struct.pack("{}I".format(endian_str()), x) if not s else struct.pack("{}i".format(endian_str()), x)

def p64(x: int, s: bool = False) -> bytes:
    """Pack one qword respecting the current architecture endianness."""
    return struct.pack("{}Q".format(endian_str()), x) if not s else struct.pack("{}q".format(endian_str()), x)

def u8(x: bytes, s: bool = False) -> int:
    """Unpack one byte respecting the current architecture endianness."""
    return struct.unpack("{}B".format(endian_str()), x)[0] if not s else struct.unpack("{}b".format(endian_str()), x)[0]

def u16(x: bytes, s: bool = False) -> int:
    """Unpack one word respecting the current architecture endianness."""
    return struct.unpack("{}H".format(endian_str()), x)[0] if not s else struct.unpack("{}h".format(endian_str()), x)[0]

def u32(x: bytes, s: bool = False) -> int:
    """Unpack one dword respecting the current architecture endianness."""
    return struct.unpack("{}I".format(endian_str()), x)[0] if not s else struct.unpack("{}i".format(endian_str()), x)[0]

def u64(x: bytes, s: bool = False) -> int:
    """Unpack one qword respecting the current architecture endianness."""
    return struct.unpack("{}Q".format(endian_str()), x)[0] if not s else struct.unpack("{}q".format(endian_str()), x)[0]


def is_ascii_string(address):
    """Helper function to determine if the buffer pointed by `address` is an ASCII string (in GDB)"""
    try:
        return gef.memory.read_ascii_string(address) is not None
    except Exception:
        return False


def is_alive():
    """Check if GDB is running."""
    try:
        return gdb.selected_inferior().pid > 0
    except Exception:
        return False


#
# Decorators
#

def only_if_gdb_running(f):
    """Decorator wrapper to check if GDB is running."""

    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if is_alive():
            return f(*args, **kwargs)
        else:
            warn("No debugging session active")

    return wrapper


def only_if_gdb_target_local(f):
    """Decorator wrapper to check if GDB is running locally (target not remote)."""

    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if not is_remote_debug():
            return f(*args, **kwargs)
        else:
            warn("This command cannot work for remote sessions.")

    return wrapper


def deprecated(solution):
    """Decorator to add a warning when a command is obsolete and will be removed."""
    def decorator(f):
        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            warn("'{}' is deprecated and will be removed in a feature release.".format(f.__name__))
            warn(solution)
            return f(*args, **kwargs)
        return wrapper
    return decorator


def experimental_feature(f):
    """Decorator to add a warning when a feature is experimental."""

    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        warn("This feature is under development, expect bugs and unstability...")
        return f(*args, **kwargs)

    return wrapper


def only_if_gdb_version_higher_than(required_gdb_version):
    """Decorator to check whether current GDB version requirements."""

    def wrapper(f):
        def inner_f(*args, **kwargs):
            if GDB_VERSION >= required_gdb_version:
                f(*args, **kwargs)
            else:
                reason = "GDB >= {} for this command".format(required_gdb_version)
                raise EnvironmentError(reason)
        return inner_f
    return wrapper


def only_if_current_arch_in(valid_architectures):
    """Decorator to allow commands for only a subset of the architectured supported by GEF.
    This decorator is to use lightly, as it goes against the purpose of GEF to support all
    architectures GDB does. However in some cases, it is necessary."""

    def wrapper(f):
        def inner_f(*args, **kwargs):
            if gef.arch in valid_architectures:
                f(*args, **kwargs)
            else:
                reason = "This command cannot work for the '{}' architecture".format(gef.arch.arch)
                raise EnvironmentError(reason)
        return inner_f
    return wrapper


def FakeExit(*args, **kwargs):
    raise RuntimeWarning

sys.exit = FakeExit

def parse_arguments(required_arguments, optional_arguments):
    """Argument parsing decorator."""

    def int_wrapper(x): return int(x, 0)

    def decorator(f):
        def wrapper(*args, **kwargs):
            parser = argparse.ArgumentParser(prog=args[0]._cmdline_, add_help=True)
            for argname in required_arguments:
                argvalue = required_arguments[argname]
                argtype = type(argvalue)
                if argtype is int:
                    argtype = int_wrapper

                argname_is_list = isinstance(argname, list) or isinstance(argname, tuple)
                if not argname_is_list and argname.startswith("-"):
                    # optional args
                    if argtype is bool:
                        parser.add_argument(argname, action="store_true" if argvalue else "store_false")
                    else:
                        parser.add_argument(argname, type=argtype, required=True, default=argvalue)
                else:
                    if argtype in (list, tuple):
                        nargs = '*'
                        argtype = type(argvalue[0])
                    else:
                        nargs = '?'
                    # positional args
                    parser.add_argument(argname, type=argtype, default=argvalue, nargs=nargs)

            for argname in optional_arguments:
                argname_is_list = isinstance(argname, list) or isinstance(argname, tuple)
                if not argname_is_list and not argname.startswith("-"):
                    # refuse positional arguments
                    continue
                argvalue = optional_arguments[argname]
                argtype = type(argvalue)
                if not argname_is_list:
                    argname = [argname,]
                if argtype is int:
                    argtype = int_wrapper
                if argtype is bool:
                    parser.add_argument(*argname, action="store_true" if argvalue else "store_false")
                else:
                    parser.add_argument(*argname, type=argtype, default=argvalue)

            try:
                parsed_args = parser.parse_args(*(args[1:]))
            except RuntimeWarning:
                return
            kwargs["arguments"] = parsed_args
            return f(*args, **kwargs)
        return wrapper
    return decorator
class Color:
    """Used to colorify terminal output."""
    colors = {
        "normal"         : "\033[0m",
        "gray"           : "\033[1;38;5;240m",
        "light_gray"     : "\033[0;37m",
        "red"            : "\033[31m",
        "green"          : "\033[32m",
        "yellow"         : "\033[33m",
        "blue"           : "\033[34m",
        "pink"           : "\033[35m",
        "cyan"           : "\033[36m",
        "bold"           : "\033[1m",
        "underline"      : "\033[4m",
        "underline_off"  : "\033[24m",
        "highlight"      : "\033[3m",
        "highlight_off"  : "\033[23m",
        "blink"          : "\033[5m",
        "blink_off"      : "\033[25m",
    }

    @staticmethod
    def redify(msg):       return Color.colorify(msg, "red")
    @staticmethod
    def greenify(msg):     return Color.colorify(msg, "green")
    @staticmethod
    def blueify(msg):      return Color.colorify(msg, "blue")
    @staticmethod
    def yellowify(msg):    return Color.colorify(msg, "yellow")
    @staticmethod
    def grayify(msg):      return Color.colorify(msg, "gray")
    @staticmethod
    def light_grayify(msg):return Color.colorify(msg, "light_gray")
    @staticmethod
    def pinkify(msg):      return Color.colorify(msg, "pink")
    @staticmethod
    def cyanify(msg):      return Color.colorify(msg, "cyan")
    @staticmethod
    def boldify(msg):      return Color.colorify(msg, "bold")
    @staticmethod
    def underlinify(msg):  return Color.colorify(msg, "underline")
    @staticmethod
    def highlightify(msg): return Color.colorify(msg, "highlight")
    @staticmethod
    def blinkify(msg):     return Color.colorify(msg, "blink")

    @staticmethod
    def colorify(text, attrs):
        """Color text according to the given attributes."""
        if gef.config["gef.disable_color"] == True: return text

        colors = Color.colors
        msg = [colors[attr] for attr in attrs.split() if attr in colors]
        msg.append(str(text))
        if colors["highlight"] in msg:   msg.append(colors["highlight_off"])
        if colors["underline"] in msg:   msg.append(colors["underline_off"])
        if colors["blink"] in msg:       msg.append(colors["blink_off"])
        msg.append(colors["normal"])
        return "".join(msg)


class Address:
    """GEF representation of memory addresses."""
    def __init__(self, *args, **kwargs):
        self.value = kwargs.get("value", 0)
        self.section = kwargs.get("section", None)
        self.info = kwargs.get("info", None)
        self.valid = kwargs.get("valid", True)
        return

    def __str__(self):
        value = format_address(self.value)
        code_color = gef.config["theme.address_code"]
        stack_color = gef.config["theme.address_stack"]
        heap_color = gef.config["theme.address_heap"]
        if self.is_in_text_segment():
            return Color.colorify(value, code_color)
        if self.is_in_heap_segment():
            return Color.colorify(value, heap_color)
        if self.is_in_stack_segment():
            return Color.colorify(value, stack_color)
        return value

    def is_in_text_segment(self):
        return (hasattr(self.info, "name") and ".text" in self.info.name) or \
            (hasattr(self.section, "path") and get_filepath() == self.section.path and self.section.is_executable())

    def is_in_stack_segment(self):
        return hasattr(self.section, "path") and "[stack]" == self.section.path

    def is_in_heap_segment(self):
        return hasattr(self.section, "path") and "[heap]" == self.section.path

    def dereference(self):
        addr = align_address(int(self.value))
        derefed = dereference(addr)
        return None if derefed is None else int(derefed)


class Permission:
    """GEF representation of Linux permission."""
    NONE      = 0
    READ      = 1
    WRITE     = 2
    EXECUTE   = 4
    ALL       = READ | WRITE | EXECUTE

    def __init__(self, **kwargs):
        self.value = kwargs.get("value", 0)
        return

    def __or__(self, value):
        return self.value | value

    def __and__(self, value):
        return self.value & value

    def __xor__(self, value):
        return self.value ^ value

    def __eq__(self, value):
        return self.value == value

    def __ne__(self, value):
        return self.value != value

    def __str__(self):
        perm_str = ""
        perm_str += "r" if self & Permission.READ else "-"
        perm_str += "w" if self & Permission.WRITE else "-"
        perm_str += "x" if self & Permission.EXECUTE else "-"
        return perm_str

    @staticmethod
    def from_info_sections(*args):
        perm = Permission()
        for arg in args:
            if "READONLY" in arg:
                perm.value += Permission.READ
            if "DATA" in arg:
                perm.value += Permission.WRITE
            if "CODE" in arg:
                perm.value += Permission.EXECUTE
        return perm

    @staticmethod
    def from_process_maps(perm_str):
        perm = Permission()
        if perm_str[0] == "r":
            perm.value += Permission.READ
        if perm_str[1] == "w":
            perm.value += Permission.WRITE
        if perm_str[2] == "x":
            perm.value += Permission.EXECUTE
        return perm


class Section:
    """GEF representation of process memory sections."""

    def __init__(self, *args, **kwargs):
        self.page_start = kwargs.get("page_start")
        self.page_end = kwargs.get("page_end")
        self.offset = kwargs.get("offset")
        self.permission = kwargs.get("permission")
        self.inode = kwargs.get("inode")
        self.path = kwargs.get("path")
        return

    def is_readable(self):
        return self.permission.value and self.permission.value & Permission.READ

    def is_writable(self):
        return self.permission.value and self.permission.value & Permission.WRITE

    def is_executable(self):
        return self.permission.value and self.permission.value & Permission.EXECUTE

    @property
    def size(self):
        if self.page_end is None or self.page_start is None:
            return -1
        return self.page_end - self.page_start

    @property
    def realpath(self):
        # when in a `gef-remote` session, realpath returns the path to the binary on the local disk, not remote
        return self.path if __gef_remote__ is None else "/tmp/gef/{:d}/{:s}".format(__gef_remote__, self.path)


Zone = collections.namedtuple("Zone", ["name", "zone_start", "zone_end", "filename"])

class Endianness(enum.Enum):
    LITTLE_ENDIAN     = 1
    BIG_ENDIAN        = 2

    def __str__(self):
        if self == Endianness.LITTLE_ENDIAN:
            return "<"
        return ">"

class Elf:
    """Basic ELF parsing.
    Ref:
    - http://www.skyfree.org/linux/references/ELF_Format.pdf
    - http://refspecs.freestandards.org/elf/elfspec_ppc.pdf
    - http://refspecs.linuxfoundation.org/ELF/ppc64/PPC-elf64abi.html
    """
    ELF_32_BITS       = 0x01
    ELF_64_BITS       = 0x02
    ELF_MAGIC         = 0x7f454c46

    X86_64            = 0x3e
    X86_32            = 0x03
    ARM               = 0x28
    MIPS              = 0x08
    POWERPC           = 0x14
    POWERPC64         = 0x15
    SPARC             = 0x02
    SPARC64           = 0x2b
    AARCH64           = 0xb7
    RISCV             = 0xf3
    IA64              = 0x32

    ET_RELOC          = 1
    ET_EXEC           = 2
    ET_DYN            = 3
    ET_CORE           = 4

    OSABI_SYSTEMV     = 0x00
    OSABI_HPUX        = 0x01
    OSABI_NETBSD      = 0x02
    OSABI_LINUX       = 0x03
    OSABI_SOLARIS     = 0x06
    OSABI_AIX         = 0x07
    OSABI_IRIX        = 0x08
    OSABI_FREEBSD     = 0x09
    OSABI_OPENBSD     = 0x0C

    e_magic           = ELF_MAGIC
    e_class           = ELF_32_BITS
    e_endianness      = Endianness.LITTLE_ENDIAN
    e_eiversion       = None
    e_osabi           = None
    e_abiversion      = None
    e_pad             = None
    e_type            = ET_EXEC
    e_machine         = X86_32
    e_version         = None
    e_entry           = 0x00
    e_phoff           = None
    e_shoff           = None
    e_flags           = None
    e_ehsize          = None
    e_phentsize       = None
    e_phnum           = None
    e_shentsize       = None
    e_shnum           = None
    e_shstrndx        = None

    def __init__(self, elf="", minimalist=False):
        """
        Instantiate an ELF object. The default behavior is to create the object by parsing the ELF file.
        But in some cases (QEMU-stub), we may just want a simple minimal object with default values."""
        if minimalist:
            return

        if not os.access(elf, os.R_OK):
            err("'{0}' not found/readable".format(elf))
            err("Failed to get file debug information, most of gef features will not work")
            return

        self.fd = open(elf, "rb")

        # off 0x0
        self.e_magic, self.e_class, self.e_endianness, self.e_eiversion = struct.unpack(">IBBB", self.read(7))

        # adjust endianness in bin reading
        endian = endian_str()

        # off 0x7
        self.e_osabi, self.e_abiversion = struct.unpack("{}BB".format(endian), self.read(2))

        # off 0x9
        self.e_pad = self.read(7)

        # off 0x10
        self.e_type, self.e_machine, self.e_version = struct.unpack("{}HHI".format(endian), self.read(8))

        # off 0x18
        if self.e_class == Elf.ELF_64_BITS:
            # if arch 64bits
            self.e_entry, self.e_phoff, self.e_shoff = struct.unpack("{}QQQ".format(endian), self.read(24))
        else:
            # else arch 32bits
            self.e_entry, self.e_phoff, self.e_shoff = struct.unpack("{}III".format(endian), self.read(12))

        self.e_flags, self.e_ehsize, self.e_phentsize, self.e_phnum = struct.unpack("{}IHHH".format(endian), self.read(10))
        self.e_shentsize, self.e_shnum, self.e_shstrndx = struct.unpack("{}HHH".format(endian), self.read(6))

        self.phdrs = []
        for i in range(self.e_phnum):
            self.phdrs.append(Phdr(self, self.e_phoff + self.e_phentsize * i))

        self.shdrs = []
        for i in range(self.e_shnum):
            self.shdrs.append(Shdr(self, self.e_shoff + self.e_shentsize * i))

        if self.fd:
            self.fd.close()
            self.fd = None

        return

    def read(self, size):
        return self.fd.read(size)

    def seek(self, off):
        self.fd.seek(off, 0)

    def is_valid(self):
        return self.e_magic == Elf.ELF_MAGIC


class Phdr:
    PT_NULL         = 0
    PT_LOAD         = 1
    PT_DYNAMIC      = 2
    PT_INTERP       = 3
    PT_NOTE         = 4
    PT_SHLIB        = 5
    PT_PHDR         = 6
    PT_TLS          = 7
    PT_LOOS         = 0x60000000
    PT_GNU_EH_FRAME = 0x6474e550
    PT_GNU_STACK    = 0x6474e551
    PT_GNU_RELRO    = 0x6474e552
    PT_LOSUNW       = 0x6ffffffa
    PT_SUNWBSS      = 0x6ffffffa
    PT_SUNWSTACK    = 0x6ffffffb
    PT_HISUNW       = 0x6fffffff
    PT_HIOS         = 0x6fffffff
    PT_LOPROC       = 0x70000000
    PT_HIPROC       = 0x7fffffff

    PF_X            = 1
    PF_W            = 2
    PF_R            = 4

    p_type   = None
    p_flags  = None
    p_offset = None
    p_vaddr  = None
    p_paddr  = None
    p_filesz = None
    p_memsz  = None
    p_align  = None

    def __init__(self, elf, off):
        if not elf:
            return None
        elf.seek(off)
        endian = endian_str()
        if elf.e_class == Elf.ELF_64_BITS:
            self.p_type, self.p_flags, self.p_offset = struct.unpack("{}IIQ".format(endian), elf.read(16))
            self.p_vaddr, self.p_paddr = struct.unpack("{}QQ".format(endian), elf.read(16))
            self.p_filesz, self.p_memsz, self.p_align = struct.unpack("{}QQQ".format(endian), elf.read(24))
        else:
            self.p_type, self.p_offset = struct.unpack("{}II".format(endian), elf.read(8))
            self.p_vaddr, self.p_paddr = struct.unpack("{}II".format(endian), elf.read(8))
            self.p_filesz, self.p_memsz, self.p_flags, self.p_align = struct.unpack("{}IIII".format(endian), elf.read(16))


class Shdr:
    SHT_NULL             = 0
    SHT_PROGBITS         = 1
    SHT_SYMTAB           = 2
    SHT_STRTAB           = 3
    SHT_RELA             = 4
    SHT_HASH             = 5
    SHT_DYNAMIC          = 6
    SHT_NOTE             = 7
    SHT_NOBITS           = 8
    SHT_REL              = 9
    SHT_SHLIB            = 10
    SHT_DYNSYM           = 11
    SHT_NUM              = 12
    SHT_INIT_ARRAY       = 14
    SHT_FINI_ARRAY       = 15
    SHT_PREINIT_ARRAY    = 16
    SHT_GROUP            = 17
    SHT_SYMTAB_SHNDX     = 18
    SHT_NUM              = 19
    SHT_LOOS             = 0x60000000
    SHT_GNU_ATTRIBUTES   = 0x6ffffff5
    SHT_GNU_HASH         = 0x6ffffff6
    SHT_GNU_LIBLIST      = 0x6ffffff7
    SHT_CHECKSUM         = 0x6ffffff8
    SHT_LOSUNW           = 0x6ffffffa
    SHT_SUNW_move        = 0x6ffffffa
    SHT_SUNW_COMDAT      = 0x6ffffffb
    SHT_SUNW_syminfo     = 0x6ffffffc
    SHT_GNU_verdef       = 0x6ffffffd
    SHT_GNU_verneed      = 0x6ffffffe
    SHT_GNU_versym       = 0x6fffffff
    SHT_HISUNW           = 0x6fffffff
    SHT_HIOS             = 0x6fffffff
    SHT_LOPROC           = 0x70000000
    SHT_HIPROC           = 0x7fffffff
    SHT_LOUSER           = 0x80000000
    SHT_HIUSER           = 0x8fffffff

    SHF_WRITE            = 1
    SHF_ALLOC            = 2
    SHF_EXECINSTR        = 4
    SHF_MERGE            = 0x10
    SHF_STRINGS          = 0x20
    SHF_INFO_LINK        = 0x40
    SHF_LINK_ORDER       = 0x80
    SHF_OS_NONCONFORMING = 0x100
    SHF_GROUP            = 0x200
    SHF_TLS              = 0x400
    SHF_COMPRESSED       = 0x800
    SHF_RELA_LIVEPATCH   = 0x00100000
    SHF_RO_AFTER_INIT    = 0x00200000
    SHF_ORDERED          = 0x40000000
    SHF_EXCLUDE          = 0x80000000

    sh_name      = None
    sh_type      = None
    sh_flags     = None
    sh_addr      = None
    sh_offset    = None
    sh_size      = None
    sh_link      = None
    sh_info      = None
    sh_addralign = None
    sh_entsize   = None

    def __init__(self, elf, off):
        if elf is None:
            return None
        elf.seek(off)
        endian = endian_str()
        if elf.e_class == Elf.ELF_64_BITS:
            self.sh_name, self.sh_type, self.sh_flags = struct.unpack("{}IIQ".format(endian), elf.read(16))
            self.sh_addr, self.sh_offset = struct.unpack("{}QQ".format(endian), elf.read(16))
            self.sh_size, self.sh_link, self.sh_info = struct.unpack("{}QII".format(endian), elf.read(16))
            self.sh_addralign, self.sh_entsize = struct.unpack("{}QQ".format(endian), elf.read(16))
        else:
            self.sh_name, self.sh_type, self.sh_flags = struct.unpack("{}III".format(endian), elf.read(12))
            self.sh_addr, self.sh_offset = struct.unpack("{}II".format(endian), elf.read(8))
            self.sh_size, self.sh_link, self.sh_info = struct.unpack("{}III".format(endian), elf.read(12))
            self.sh_addralign, self.sh_entsize = struct.unpack("{}II".format(endian), elf.read(8))

        stroff = elf.e_shoff + elf.e_shentsize * elf.e_shstrndx

        if elf.e_class == Elf.ELF_64_BITS:
            elf.seek(stroff + 16 + 8)
            offset = struct.unpack("{}Q".format(endian), elf.read(8))[0]
        else:
            elf.seek(stroff + 12 + 4)
            offset = struct.unpack("{}I".format(endian), elf.read(4))[0]
        elf.seek(offset + self.sh_name)
        self.sh_name = ""
        while True:
            c = ord(elf.read(1))
            if c == 0:
                break
            self.sh_name += chr(c)
        return


class Instruction:
    """GEF representation of a CPU instruction."""

    def __init__(self, address, location, mnemo, operands, opcodes):
        self.address, self.location, self.mnemonic, self.operands, self.opcodes = address, location, mnemo, operands, opcodes
        return

    # Allow formatting an instruction with {:o} to show opcodes.
    # The number of bytes to display can be configured, e.g. {:4o} to only show 4 bytes of the opcodes
    def __format__(self, format_spec):
        if len(format_spec) == 0 or format_spec[-1] != "o":
            return str(self)

        if format_spec == "o":
            opcodes_len = len(self.opcodes)
        else:
            opcodes_len = int(format_spec[:-1])

        opcodes_text = "".join("{:02x}".format(b) for b in self.opcodes[:opcodes_len])
        if opcodes_len < len(self.opcodes):
            opcodes_text += "..."
        return "{:#10x} {:{:d}} {:16} {:6} {:s}".format(self.address,
                                                        opcodes_text,
                                                        opcodes_len * 2 + 3,
                                                        self.location,
                                                        self.mnemonic,
                                                        ", ".join(self.operands))

    def __str__(self):
        return "{:#10x} {:16} {:6} {:s}".format(
            self.address, self.location, self.mnemonic, ", ".join(self.operands)
        )

    def is_valid(self):
        return "(bad)" not in self.mnemonic


@lru_cache()
def search_for_main_arena():
    malloc_hook_addr = parse_address("(void *)&__malloc_hook")

    if is_x86():
        addr = align_address_to_size(malloc_hook_addr + gef.arch.ptrsize, 0x20)
    elif is_arch(Elf.AARCH64) or is_arch(Elf.ARM):
        addr = malloc_hook_addr - gef.arch.ptrsize*2 - MallocStateStruct("*0").struct_size
    else:
        raise OSError("Cannot find main_arena for {}".format(gef.arch.arch))

    addr = "*0x{:x}".format(addr)
    return addr


class MallocStateStruct:
    """GEF representation of malloc_state from https://github.com/bminor/glibc/blob/glibc-2.28/malloc/malloc.c#L1658"""

    def __init__(self, addr):
        try:
            self.__addr = parse_address("&{}".format(addr))
        except gdb.error:
            warn("Could not parse address '&{}' when searching malloc_state struct, "
                 "using '&main_arena' instead".format(addr))
            self.__addr = search_for_main_arena()
            # if `search_for_main_arena` throws `gdb.error` on symbol lookup: it means the session is not started
            # so just propagate the exception

        self.num_fastbins = 10
        self.num_bins = 254

        self.int_size = cached_lookup_type("int").sizeof
        self.size_t = cached_lookup_type("size_t")
        if not self.size_t:
            ptr_type = "unsigned long" if gef.arch.ptrsize == 8 else "unsigned int"
            self.size_t = cached_lookup_type(ptr_type)

        # Account for separation of have_fastchunks flag into its own field
        # within the malloc_state struct in GLIBC >= 2.27
        # https://sourceware.org/git/?p=glibc.git;a=commit;h=e956075a5a2044d05ce48b905b10270ed4a63e87
        # Be aware you could see this change backported into GLIBC release
        # branches.
        if get_libc_version() >= (2, 27):
            self.fastbin_offset = align_address_to_size(self.int_size * 3, 8)
        else:
            self.fastbin_offset = self.int_size * 2
        return

    # struct offsets
    @property
    def addr(self):
        return self.__addr

    @property
    def fastbins_addr(self):
        return self.__addr + self.fastbin_offset

    @property
    def top_addr(self):
        return self.fastbins_addr + self.num_fastbins * gef.arch.ptrsize

    @property
    def last_remainder_addr(self):
        return self.top_addr + gef.arch.ptrsize

    @property
    def bins_addr(self):
        return self.last_remainder_addr + gef.arch.ptrsize

    @property
    def next_addr(self):
        return self.bins_addr + self.num_bins * gef.arch.ptrsize + self.int_size * 4

    @property
    def next_free_addr(self):
        return self.next_addr + gef.arch.ptrsize

    @property
    def system_mem_addr(self):
        return self.next_free_addr + gef.arch.ptrsize * 2

    @property
    def struct_size(self):
        return self.system_mem_addr + gef.arch.ptrsize * 2 - self.__addr

    # struct members
    @property
    def fastbinsY(self):
        return self.get_size_t_array(self.fastbins_addr, self.num_fastbins)

    @property
    def top(self):
        return self.get_size_t_pointer(self.top_addr)

    @property
    def last_remainder(self):
        return self.get_size_t_pointer(self.last_remainder_addr)

    @property
    def bins(self):
        return self.get_size_t_array(self.bins_addr, self.num_bins)

    @property
    def next(self):
        return self.get_size_t_pointer(self.next_addr)

    @property
    def next_free(self):
        return self.get_size_t_pointer(self.next_free_addr)

    @property
    def system_mem(self):
        return self.get_size_t(self.system_mem_addr)

    # helper methods
    def get_size_t(self, addr):
        return dereference(addr).cast(self.size_t)

    def get_size_t_pointer(self, addr):
        size_t_pointer = self.size_t.pointer()
        return dereference(addr).cast(size_t_pointer)

    def get_size_t_array(self, addr, length):
        size_t_array = self.size_t.array(length)
        return dereference(addr).cast(size_t_array)

    def __getitem__(self, item):
        return getattr(self, item)


class GlibcHeapInfo:
    """Glibc heap_info struct
    See https://github.com/bminor/glibc/blob/glibc-2.34/malloc/arena.c#L64"""

    def __init__(self, addr):
        self.__addr = addr if type(addr) is int else parse_address(addr)
        self.size_t = cached_lookup_type("size_t")
        if not self.size_t:
            ptr_type = "unsigned long" if gef.arch.ptrsize == 8 else "unsigned int"
            self.size_t = cached_lookup_type(ptr_type)

    @property
    def addr(self):
        return self.__addr

    @property
    def ar_ptr_addr(self):
        return self.addr

    @property
    def prev_addr(self):
        return self.ar_ptr_addr + gef.arch.ptrsize

    @property
    def size_addr(self):
        return self.prev_addr + gef.arch.ptrsize

    @property
    def mprotect_size_addr(self):
        return self.size_addr + self.size_t.sizeof

    @property
    def ar_ptr(self):
        return self._get_size_t_pointer(self.ar_ptr_addr)

    @property
    def prev(self):
        return self._get_size_t_pointer(self.prev_addr)

    @property
    def size(self):
        return self._get_size_t(self.size_addr)

    @property
    def mprotect_size(self):
        return self._get_size_t(self.mprotect_size_addr)

    # helper methods
    def _get_size_t_pointer(self, addr):
        size_t_pointer = self.size_t.pointer()
        return dereference(addr).cast(size_t_pointer)

    def _get_size_t(self, addr):
        return dereference(addr).cast(self.size_t)


class GlibcArena:
    """Glibc arena class
    Ref: https://github.com/sploitfun/lsploits/blob/master/glibc/malloc/malloc.c#L1671"""

    def __init__(self, addr):
        # self.__name = name or __gef_current_arena__
        try:
            arena = gdb.parse_and_eval(addr)
            malloc_state_t = cached_lookup_type("struct malloc_state")
            self.__arena = arena.cast(malloc_state_t)
            self.__addr = int(arena.address)
            self.struct_size = malloc_state_t.sizeof
        except:
            self.__arena = MallocStateStruct(addr)
            self.__addr = self.__arena.addr
        try:
            self.top             = int(self.top)
            self.last_remainder  = int(self.last_remainder)
            self.n               = int(self.next)
            self.nfree           = int(self.next_free)
            self.sysmem          = int(self.system_mem)
        except gdb.error as e:
            err("Glibc arena: {}".format(e))
        return

    def __getitem__(self, item):
        return self.__arena[item]

    def __getattr__(self, item):
        return self.__arena[item]

    def __int__(self):
        return self.__addr

    def __iter__(self):
        return self

    def __next__(self):
        # arena = self
        # while arena is not None:
        #     yield arena
        #     arena = arena.get_next()
        next_arena_address = int(self.next)
        # arena_main = GlibcArena(self.__name)
        if next_arena_address == int(gef.heap.main_arena):
            raise StopIteration
        return GlibcArena("*{:#x} ".format(next_arena_address))

    def __eq__(self, other):
        # You cannot have 2 arenas at the same address, so this check should be enough
        return self.__addr == int(self)

    def fastbin(self, i):
        """Return head chunk in fastbinsY[i]."""
        addr = int(self.fastbinsY[i])
        if addr == 0:
            return None
        return GlibcChunk(addr + 2 * gef.arch.ptrsize)

    def bin(self, i):
        idx = i * 2
        fd = int(self.bins[idx])
        bw = int(self.bins[idx + 1])
        return fd, bw

    # def get_next(self):
    #     addr_next = int(self.next)
    #     arena_main = GlibcArena(self.__name)
    #     if addr_next == arena_main.__addr:
    #         return None
    #     return GlibcArena("*{:#x} ".format(addr_next))

    @deprecated("use `==` operator instead")
    def is_main_arena(self):
        return int(self) == int(gef.heap.main_arena)

    def heap_addr(self, allow_unaligned=False):
        if self.is_main_arena():
            heap_section = HeapBaseFunction.heap_base()
            if not heap_section:
                err("Heap not initialized")
                return None
            return heap_section
        _addr = int(self) + self.struct_size
        if allow_unaligned:
            return _addr
        return malloc_align_address(_addr)

    def get_heap_info_list(self):
        if self.is_main_arena():
            return None
        heap_addr = self.get_heap_for_ptr(self.top)
        heap_infos = [GlibcHeapInfo(heap_addr)]
        while heap_infos[-1].prev != 0:
            prev = int(heap_infos[-1].prev)
            heap_info = GlibcHeapInfo(prev)
            heap_infos.append(heap_info)
        return heap_infos[::-1]

    @staticmethod
    def get_heap_for_ptr(ptr):
        """Find the corresponding heap for a given pointer (int).
        See https://github.com/bminor/glibc/blob/glibc-2.34/malloc/arena.c#L129"""
        if is_32bit():
            default_mmap_threshold_max = 512 * 1024
        else:  # 64bit
            default_mmap_threshold_max = 4 * 1024 * 1024 * cached_lookup_type("long").sizeof
        heap_max_size = 2 * default_mmap_threshold_max
        return ptr & ~(heap_max_size - 1)

    def __str__(self):
        fmt = "{:s}(base={:#x}, top={:#x}, last_remainder={:#x}, next={:#x}, next_free={:#x}, system_mem={:#x})"
        return fmt.format(
            Color.colorify("Arena", "blue bold underline"),
            self.__addr, self.top, self.last_remainder, self.n, self.nfree, self.sysmem
        )


class GlibcChunk:
    """Glibc chunk class. The default behavior (from_base=False) is to interpret the data starting at the memory
    address pointed to as the chunk data. Setting from_base to True instead treats that data as the chunk header.
    Ref:  https://sploitfun.wordpress.com/2015/02/10/understanding-glibc-malloc/."""

    def __init__(self, addr, from_base=False, allow_unaligned=True):
        self.ptrsize = gef.arch.ptrsize
        if from_base:
            self.data_address = addr + 2 * self.ptrsize
        else:
            self.data_address = addr
        if not allow_unaligned:
            self.data_address = malloc_align_address(self.data_address)
        self.base_address = addr - 2 * self.ptrsize

        self.size_addr = int(self.data_address - self.ptrsize)
        self.prev_size_addr = self.base_address
        return

    def get_chunk_size(self):
        return gef.memory.read_integer(self.size_addr) & (~0x07)

    @property
    def size(self):
        return self.get_chunk_size()

    def get_usable_size(self):
        # https://github.com/sploitfun/lsploits/blob/master/glibc/malloc/malloc.c#L4537
        cursz = self.get_chunk_size()
        if cursz == 0: return cursz
        if self.has_m_bit(): return cursz - 2 * self.ptrsize
        return cursz - self.ptrsize

    @property
    def usable_size(self):
        return self.get_usable_size()

    def get_prev_chunk_size(self):
        return gef.memory.read_integer(self.prev_size_addr)

    def __iter__(self):
        return self

    def __next__(self):
        return self.get_next_chunk()

    def get_next_chunk(self, allow_unaligned=False):
        addr = self.get_next_chunk_addr()
        return GlibcChunk(addr, allow_unaligned=allow_unaligned)

    def get_next_chunk_addr(self):
        return self.data_address + self.get_chunk_size()

    # if free-ed functions
    def get_fwd_ptr(self, sll):
        # Not a single-linked-list (sll) or no Safe-Linking support yet
        if not sll or get_libc_version() < (2, 32):
            return gef.memory.read_integer(self.data_address)
        # Unmask ("reveal") the Safe-Linking pointer
        else:
            return gef.memory.read_integer(self.data_address) ^ (self.data_address >> 12)

    @property
    def fwd(self):
        return self.get_fwd_ptr(False)

    fd = fwd  # for compat

    def get_bkw_ptr(self):
        return gef.memory.read_integer(self.data_address + self.ptrsize)

    @property
    def bck(self):
        return self.get_bkw_ptr()

    bk = bck  # for compat
    # endif free-ed functions

    def has_p_bit(self):
        return gef.memory.read_integer(self.size_addr) & 0x01

    def has_m_bit(self):
        return gef.memory.read_integer(self.size_addr) & 0x02

    def has_n_bit(self):
        return gef.memory.read_integer(self.size_addr) & 0x04

    def is_used(self):
        """Check if the current block is used by:
        - checking the M bit is true
        - or checking that next chunk PREV_INUSE flag is true"""
        if self.has_m_bit():
            return True

        next_chunk = self.get_next_chunk()
        return True if next_chunk.has_p_bit() else False

    def str_chunk_size_flag(self):
        msg = []
        msg.append("PREV_INUSE flag: {}".format(Color.greenify("On") if self.has_p_bit() else Color.redify("Off")))
        msg.append("IS_MMAPPED flag: {}".format(Color.greenify("On") if self.has_m_bit() else Color.redify("Off")))
        msg.append("NON_MAIN_ARENA flag: {}".format(Color.greenify("On") if self.has_n_bit() else Color.redify("Off")))
        return "\n".join(msg)

    def _str_sizes(self):
        msg = []
        failed = False

        try:
            msg.append("Chunk size: {0:d} ({0:#x})".format(self.get_chunk_size()))
            msg.append("Usable size: {0:d} ({0:#x})".format(self.get_usable_size()))
            failed = True
        except gdb.MemoryError:
            msg.append("Chunk size: Cannot read at {:#x} (corrupted?)".format(self.size_addr))

        try:
            msg.append("Previous chunk size: {0:d} ({0:#x})".format(self.get_prev_chunk_size()))
            failed = True
        except gdb.MemoryError:
            msg.append("Previous chunk size: Cannot read at {:#x} (corrupted?)".format(self.base_address))

        if failed:
            msg.append(self.str_chunk_size_flag())

        return "\n".join(msg)

    def _str_pointers(self):
        fwd = self.data_address
        bkw = self.data_address + self.ptrsize

        msg = []
        try:
            msg.append("Forward pointer: {0:#x}".format(self.get_fwd_ptr(False)))
        except gdb.MemoryError:
            msg.append("Forward pointer: {0:#x} (corrupted?)".format(fwd))

        try:
            msg.append("Backward pointer: {0:#x}".format(self.get_bkw_ptr()))
        except gdb.MemoryError:
            msg.append("Backward pointer: {0:#x} (corrupted?)".format(bkw))

        return "\n".join(msg)

    def str_as_alloced(self):
        return self._str_sizes()

    def str_as_freed(self):
        return "{}\n\n{}".format(self._str_sizes(), self._str_pointers())

    def flags_as_string(self):
        flags = []
        if self.has_p_bit():
            flags.append(Color.colorify("PREV_INUSE", "red bold"))
        else:
            flags.append(Color.colorify("! PREV_INUSE", "green bold"))
        if self.has_m_bit():
            flags.append(Color.colorify("IS_MMAPPED", "red bold"))
        if self.has_n_bit():
            flags.append(Color.colorify("NON_MAIN_ARENA", "red bold"))
        return "|".join(flags)

    def __str__(self):
        msg = "{:s}(addr={:#x}, size={:#x}, flags={:s})".format(Color.colorify("Chunk", "yellow bold underline"),
                                                                int(self.data_address),
                                                                self.get_chunk_size(),
                                                                self.flags_as_string())
        return msg

    def psprint(self):
        msg = []
        msg.append(str(self))
        if self.is_used():
            msg.append(self.str_as_alloced())
        else:
            msg.append(self.str_as_freed())

        return "\n".join(msg) + "\n"


pattern_libc_ver = re.compile(rb"glibc (\d+)\.(\d+)")


@lru_cache()
def get_libc_version():
    sections = get_process_maps()
    for section in sections:
        match = re.search(r"libc6?[-_](\d+)\.(\d+)\.so", section.path)
        if match:
            return tuple(int(_) for _ in match.groups())
        if "libc" in section.path:
            try:
                with open(section.path, "rb") as f:
                    data = f.read()
            except OSError:
                continue
            match = re.search(pattern_libc_ver, data)
            if match:
                return tuple(int(_) for _ in match.groups())
    return 0, 0


# @lru_cache()
# def get_glibc_arena(addr=None):
#     try:
#         addr = "*{}".format(addr) if addr else __gef_current_arena__
#         return GlibcArena(addr)
#     except Exception as e:
#         err("Failed to get the glibc arena, heap commands may not work properly: {}".format(e))
#         return None

# def get_glibc_arenas(addr=None):
#     return iter(  GlibcArena(addr) )

# def get_first_arena(addr=None):
#     return next( get_glibc_arenas(addr) )


def titlify(text, color=None, msg_color=None):
    """Print a centered title."""
    cols = get_terminal_size()[1]
    nb = (cols - len(text) - 2) // 2
    if color is None:
        color = gef.config["theme.default_title_line"]
    if msg_color is None:
        msg_color = gef.config["theme.default_title_message"]

    msg = []
    msg.append(Color.colorify("{} ".format(HORIZONTAL_LINE * nb), color))
    msg.append(Color.colorify(text, msg_color))
    msg.append(Color.colorify(" {}".format(HORIZONTAL_LINE * nb), color))
    return "".join(msg)


def err(msg):   return gef_print("{} {}".format(Color.colorify("[!]", "bold red"), msg))
def warn(msg):  return gef_print("{} {}".format(Color.colorify("[*]", "bold yellow"), msg))
def ok(msg):    return gef_print("{} {}".format(Color.colorify("[+]", "bold green"), msg))
def info(msg):  return gef_print("{} {}".format(Color.colorify("[+]", "bold blue"), msg))


def push_context_message(level, message):
    """Push the message to be displayed the next time the context is invoked."""
    global __context_messages__
    if level not in ("error", "warn", "ok", "info"):
        err("Invalid level '{}', discarding message".format(level))
        return
    __context_messages__.append((level, message))
    return


def show_last_exception():
    """Display the last Python exception."""

    def _show_code_line(fname, idx):
        fname = os.path.expanduser(os.path.expandvars(fname))
        with open(fname, "r") as f:
            __data = f.readlines()
        return __data[idx - 1] if idx < len(__data) else ""

    gef_print("")
    exc_type, exc_value, exc_traceback = sys.exc_info()

    gef_print(" Exception raised ".center(80, HORIZONTAL_LINE))
    gef_print("{}: {}".format(Color.colorify(exc_type.__name__, "bold underline red"), exc_value))
    gef_print(" Detailed stacktrace ".center(80, HORIZONTAL_LINE))

    for fs in traceback.extract_tb(exc_traceback)[::-1]:
        filename, lineno, method, code = fs

        if not code or not code.strip():
            code = _show_code_line(filename, lineno)

        gef_print("""{} File "{}", line {:d}, in {}()""".format(DOWN_ARROW, Color.yellowify(filename),
                                                                lineno, Color.greenify(method)))
        gef_print("   {}    {}".format(RIGHT_ARROW, code))

    gef_print(" Version ".center(80, HORIZONTAL_LINE))
    gdb.execute("version full")
    gef_print(" Last 10 GDB commands ".center(80, HORIZONTAL_LINE))
    gdb.execute("show commands")
    gef_print(" Runtime environment ".center(80, HORIZONTAL_LINE))
    gef_print("* GDB: {}".format(gdb.VERSION))
    gef_print("* Python: {:d}.{:d}.{:d} - {:s}".format(sys.version_info.major, sys.version_info.minor,
                                                       sys.version_info.micro, sys.version_info.releaselevel))
    gef_print("* OS: {:s} - {:s} ({:s})".format(platform.system(), platform.release(), platform.machine()))

    try:
        lsb_release = which("lsb_release")
        gdb.execute("!{} -a".format(lsb_release,))
    except FileNotFoundError:
        gef_print("lsb_release is missing, cannot collect additional debug information")

    gef_print(HORIZONTAL_LINE*80)
    gef_print("")
    return


def gef_pystring(x):
    """Returns a sanitized version as string of the bytes list given in input."""
    res = str(x, encoding="utf-8")
    substs = [("\n", "\\n"), ("\r", "\\r"), ("\t", "\\t"), ("\v", "\\v"), ("\b", "\\b"), ]
    for x, y in substs: res = res.replace(x, y)
    return res


def gef_pybytes(x):
    """Returns an immutable bytes list from the string given as input."""
    return bytes(str(x), encoding="utf-8")


@lru_cache()
def which(program):
    """Locate a command on the filesystem."""

    def is_exe(fpath):
        return os.path.isfile(fpath) and os.access(fpath, os.X_OK)

    fpath = os.path.split(program)[0]
    if fpath:
        if is_exe(program):
            return program
    else:
        for path in os.environ["PATH"].split(os.pathsep):
            path = path.strip('"')
            exe_file = os.path.join(path, program)
            if is_exe(exe_file):
                return exe_file

    raise FileNotFoundError("Missing file `{:s}`".format(program))


def style_byte(b, color=True):
    style = {
        "nonprintable": "yellow",
        "printable": "white",
        "00": "gray",
        "0a": "blue",
        "ff": "green",
    }
    sbyte = "{:02x}".format(b)
    if not color or gef.config["highlight.regex"]:
        return sbyte

    if sbyte in style:
        st = style[sbyte]
    elif chr(b) in (string.ascii_letters + string.digits + string.punctuation + " "):
        st = style.get("printable")
    else:
        st = style.get("nonprintable")
    if st:
        sbyte = Color.colorify(sbyte, st)
    return sbyte


def hexdump(source, length=0x10, separator=".", show_raw=False, show_symbol=True, base=0x00):
    """Return the hexdump of `src` argument.
    @param source *MUST* be of type bytes or bytearray
    @param length is the length of items per line
    @param separator is the default character to use if one byte is not printable
    @param show_raw if True, do not add the line nor the text translation
    @param base is the start address of the block being hexdump
    @return a string with the hexdump"""
    result = []
    align = get_memory_alignment() * 2 + 2 if is_alive() else 18

    for i in range(0, len(source), length):
        chunk = bytearray(source[i : i + length])
        hexa = " ".join([style_byte(b, color=not show_raw) for b in chunk])

        if show_raw:
            result.append(hexa)
            continue

        text = "".join([chr(b) if 0x20 <= b < 0x7F else separator for b in chunk])
        if show_symbol:
            sym = gdb_get_location_from_symbol(base + i)
            sym = "<{:s}+{:04x}>".format(*sym) if sym else ""
        else:
            sym = ""

        result.append("{addr:#0{aw}x} {sym}    {data:<{dw}}    {text}".format(aw=align,
                                                                              addr=base+i,
                                                                              sym=sym,
                                                                              dw=3*length,
                                                                              data=hexa,
                                                                              text=text))
    return "\n".join(result)


def is_debug():
    """Check if debug mode is enabled."""
    return gef.config["gef.debug"] == True

context_hidden = False


def hide_context():
    global context_hidden
    context_hidden = True


def unhide_context():
    global context_hidden
    context_hidden = False


def enable_redirect_output(to_file="/dev/null"):
    """Redirect all GDB output to `to_file` parameter. By default, `to_file` redirects to `/dev/null`."""
    gdb.execute("set logging overwrite")
    gdb.execute("set logging file {:s}".format(to_file))
    gdb.execute("set logging redirect on")
    gdb.execute("set logging on")
    return


def disable_redirect_output():
    """Disable the output redirection, if any."""
    gdb.execute("set logging off")
    gdb.execute("set logging redirect off")
    return


def gef_makedirs(path, mode=0o755):
    """Recursive mkdir() creation. If successful, return the absolute path of the directory created."""
    abspath = os.path.expanduser(path)
    abspath = os.path.realpath(abspath)

    if os.path.isdir(abspath):
        return abspath

    os.makedirs(abspath, mode=mode, exist_ok=True)
    return abspath


@lru_cache()
def gdb_lookup_symbol(sym):
    """Fetch the proper symbol or None if not defined."""
    try:
        return gdb.decode_line(sym)[1]
    except gdb.error:
        return None


@lru_cache(maxsize=512)
def gdb_get_location_from_symbol(address):
    """Retrieve the location of the `address` argument from the symbol table.
    Return a tuple with the name and offset if found, None otherwise."""
    # this is horrible, ugly hack and shitty perf...
    # find a *clean* way to get gdb.Location from an address
    name = None
    sym = gdb.execute("info symbol {:#x}".format(address), to_string=True)
    if sym.startswith("No symbol matches"):
        return None

    i = sym.find(" in section ")
    sym = sym[:i].split()
    name, offset = sym[0], 0
    if len(sym) == 3 and sym[2].isdigit():
        offset = int(sym[2])
    return name, offset


def gdb_disassemble(start_pc, **kwargs):
    """Disassemble instructions from `start_pc` (Integer). Accepts the following named parameters:
    - `end_pc` (Integer) only instructions whose start address fall in the interval from start_pc to end_pc are returned.
    - `count` (Integer) list at most this many disassembled instructions
    If `end_pc` and `count` are not provided, the function will behave as if `count=1`.
    Return an iterator of Instruction objects
    """
    frame = gdb.selected_frame()
    arch = frame.architecture()

    for insn in arch.disassemble(start_pc, **kwargs):
        address = insn["addr"]
        asm = insn["asm"].rstrip().split(None, 1)
        if len(asm) > 1:
            mnemo, operands = asm
            operands = operands.split(",")
        else:
            mnemo, operands = asm[0], []

        loc = gdb_get_location_from_symbol(address)
        location = "<{}+{}>".format(*loc) if loc else ""

        opcodes = gef.memory.read(insn["addr"], insn["length"])

        yield Instruction(address, location, mnemo, operands, opcodes)


def gdb_get_nth_previous_instruction_address(addr, n):
    """Return the address (Integer) of the `n`-th instruction before `addr`."""
    # fixed-length ABI
    if gef.arch.instruction_length:
        return max(0, addr - n * gef.arch.instruction_length)

    # variable-length ABI
    cur_insn_addr = gef_current_instruction(addr).address

    # we try to find a good set of previous instructions by "guessing" disassembling backwards
    # the 15 comes from the longest instruction valid size
    for i in range(15 * n, 0, -1):
        try:
            insns = list(gdb_disassemble(addr - i, end_pc=cur_insn_addr))
        except gdb.MemoryError:
            # this is because we can hit an unmapped page trying to read backward
            break

        # 1. check that the disassembled instructions list size can satisfy
        if len(insns) < n + 1:  # we expect the current instruction plus the n before it
            continue

        # If the list of instructions is longer than what we need, then we
        # could get lucky and already have more than what we need, so slice down
        insns = insns[-n - 1 :]

        # 2. check that the sequence ends with the current address
        if insns[-1].address != cur_insn_addr:
            continue

        # 3. check all instructions are valid
        if all(insn.is_valid() for insn in insns):
            return insns[0].address

    return None


def gdb_get_nth_next_instruction_address(addr, n):
    """Return the address (Integer) of the `n`-th instruction after `addr`."""
    # fixed-length ABI
    if gef.arch.instruction_length:
        return addr + n * gef.arch.instruction_length

    # variable-length ABI
    insn = list(gdb_disassemble(addr, count=n))[-1]
    return insn.address


def gef_instruction_n(addr, n):
    """Return the `n`-th instruction after `addr` as an Instruction object."""
    return list(gdb_disassemble(addr, count=n + 1))[n]


def gef_get_instruction_at(addr):
    """Return the full Instruction found at the specified address."""
    insn = next(gef_disassemble(addr, 1))
    return insn


def gef_current_instruction(addr):
    """Return the current instruction as an Instruction object."""
    return gef_instruction_n(addr, 0)


def gef_next_instruction(addr):
    """Return the next instruction as an Instruction object."""
    return gef_instruction_n(addr, 1)


def gef_disassemble(addr, nb_insn, nb_prev=0):
    """Disassemble `nb_insn` instructions after `addr` and `nb_prev` before `addr`.
    Return an iterator of Instruction objects."""
    nb_insn = max(1, nb_insn)

    if nb_prev:
        start_addr = gdb_get_nth_previous_instruction_address(addr, nb_prev)
        if start_addr:
            for insn in gdb_disassemble(start_addr, count=nb_prev):
                if insn.address == addr: break
                yield insn

    for insn in gdb_disassemble(addr, count=nb_insn):
        yield insn


def capstone_disassemble(location, nb_insn, **kwargs):
    """Disassemble `nb_insn` instructions after `addr` and `nb_prev` before
    `addr` using the Capstone-Engine disassembler, if available.
    Return an iterator of Instruction objects."""

    def cs_insn_to_gef_insn(cs_insn):
        sym_info = gdb_get_location_from_symbol(cs_insn.address)
        loc = "<{}+{}>".format(*sym_info) if sym_info else ""
        ops = [] + cs_insn.op_str.split(", ")
        return Instruction(cs_insn.address, loc, cs_insn.mnemonic, ops, cs_insn.bytes)

    capstone    = sys.modules["capstone"]
    arch, mode  = get_capstone_arch(arch=kwargs.get("arch"), mode=kwargs.get("mode"), endian=kwargs.get("endian"))
    cs          = capstone.Cs(arch, mode)
    cs.detail   = True

    page_start  = align_address_to_page(location)
    offset      = location - page_start
    pc          = gef.arch.pc

    skip       = int(kwargs.get("skip", 0))
    nb_prev    = int(kwargs.get("nb_prev", 0))
    if nb_prev > 0:
        location = gdb_get_nth_previous_instruction_address(pc, nb_prev)
        nb_insn += nb_prev

    code = kwargs.get("code", gef.memory.read(location, gef_getpagesize() - offset - 1))
    code = bytes(code)

    for insn in cs.disasm(code, location):
        if skip:
            skip -= 1
            continue
        nb_insn -= 1
        yield cs_insn_to_gef_insn(insn)
        if nb_insn == 0:
            break
    return


def gef_execute_external(command, as_list=False, *args, **kwargs):
    """Execute an external command and return the result."""
    res = subprocess.check_output(command, stderr=subprocess.STDOUT, shell=kwargs.get("shell", False))
    return [gef_pystring(_) for _ in res.splitlines()] if as_list else gef_pystring(res)


def gef_execute_gdb_script(commands):
    """Execute the parameter `source` as GDB command. This is done by writing `commands` to
    a temporary file, which is then executed via GDB `source` command. The tempfile is then deleted."""
    fd, fname = tempfile.mkstemp(suffix=".gdb", prefix="gef_")
    with os.fdopen(fd, "w") as f:
        f.write(commands)
        f.flush()
    if os.access(fname, os.R_OK):
        gdb.execute("source {:s}".format(fname))
        os.unlink(fname)
    return


@lru_cache(32)
def checksec(filename):
    """Check the security property of the ELF binary. The following properties are:
    - Canary
    - NX
    - PIE
    - Fortify
    - Partial/Full RelRO.
    Return a dict() with the different keys mentioned above, and the boolean
    associated whether the protection was found."""

    if is_macho(filename):
        return {
            "Canary": False,
            "NX": False,
            "PIE": False,
            "Fortify": False,
            "Partial RelRO": False,
        }

    try:
        readelf = which("readelf")
    except IOError:
        err("Missing `readelf`")
        return

    def __check_security_property(opt, filename, pattern):
        cmd   = [readelf,]
        cmd  += opt.split()
        cmd  += [filename,]
        lines = gef_execute_external(cmd, as_list=True)
        for line in lines:
            if re.search(pattern, line):
                return True
        return False

    results = collections.OrderedDict()
    results["Canary"] = __check_security_property("-s", filename, r"__stack_chk_fail") is True
    has_gnu_stack = __check_security_property("-W -l", filename, r"GNU_STACK") is True
    if has_gnu_stack:
        results["NX"] = __check_security_property("-W -l", filename, r"GNU_STACK.*RWE") is False
    else:
        results["NX"] = False
    results["PIE"] = __check_security_property("-h", filename, r":.*EXEC") is False
    results["Fortify"] = __check_security_property("-s", filename, r"_chk@GLIBC") is True
    results["Partial RelRO"] = __check_security_property("-l", filename, r"GNU_RELRO") is True
    results["Full RelRO"] = results["Partial RelRO"] and __check_security_property("-d", filename, r"BIND_NOW") is True
    return results


@lru_cache()
def get_arch():
    """Return the binary's architecture."""
    if is_alive():
        arch = gdb.selected_frame().architecture()
        return arch.name()

    arch_str = gdb.execute("show architecture", to_string=True).strip()
    if "The target architecture is set automatically (currently " in arch_str:
        arch_str = arch_str.split("(currently ", 1)[1]
        arch_str = arch_str.split(")", 1)[0]
    elif "The target architecture is assumed to be " in arch_str:
        arch_str = arch_str.replace("The target architecture is assumed to be ", "")
    elif "The target architecture is set to " in arch_str:
        # GDB version >= 10.1
        arch_str = re.findall(r"\"(.+)\"", arch_str)[0]
    else:
        # Unknown, we throw an exception to be safe
        raise RuntimeError("Unknown architecture: {}".format(arch_str))
    return arch_str


# @lru_cache()
# def get_endian():
#     """Return the binary endianness."""

#     endian = gdb.execute("show endian", to_string=True).strip().lower()
#     if "little endian" in endian:
#         return Elf.LITTLE_ENDIAN
#     if "big endian" in endian:
#         return Elf.BIG_ENDIAN

#     raise EnvironmentError("Invalid endianness")


@lru_cache()
def get_entry_point():
    """Return the binary entry point."""

    for line in gdb.execute("info target", to_string=True).split("\n"):
        if "Entry point:" in line:
            return int(line.strip().split(" ")[-1], 16)

    return None


def is_pie(fpath):
    return checksec(fpath)["PIE"]


@deprecated("Prefer `gef.arch.endianness == Endianness.BIG_ENDIAN`")
def is_big_endian():
    return gef.arch.endianness == Endianness.BIG_ENDIAN


@deprecated("gef.arch.endianness == Endianness.LITTLE_ENDIAN")
def is_little_endian():
    return gef.arch.endianness == Endianness.LITTLE_ENDIAN


def flags_to_human(reg_value, value_table):
    """Return a human readable string showing the flag states."""
    flags = []
    for i in value_table:
        flag_str = Color.boldify(value_table[i].upper()) if reg_value & (1<<i) else value_table[i].lower()
        flags.append(flag_str)
    return "[{}]".format(" ".join(flags))


#
# Architecture classes
#

class Architecture(metaclass=abc.ABCMeta):
    """Generic metaclass for the architecture supported by GEF."""

    @abc.abstractproperty
    def all_registers(self):                       pass
    @abc.abstractproperty
    def instruction_length(self):                  pass
    @abc.abstractproperty
    def nop_insn(self):                            pass
    @abc.abstractproperty
    def return_register(self):                     pass
    @abc.abstractproperty
    def flag_register(self):                       pass
    @abc.abstractproperty
    def flags_table(self):                         pass
    @abc.abstractproperty
    def function_parameters(self):                 pass
    @abc.abstractmethod
    def flag_register_to_human(self, val=None):    pass
    @abc.abstractmethod
    def is_call(self, insn):                       pass
    @abc.abstractmethod
    def is_ret(self, insn):                        pass
    @abc.abstractmethod
    def is_conditional_branch(self, insn):         pass
    @abc.abstractmethod
    def is_branch_taken(self, insn):               pass
    @abc.abstractmethod
    def get_ra(self, insn, frame):                 pass

    special_registers = []

    @property
    def pc(self):
        return get_register("$pc")

    @property
    def sp(self):
        return get_register("$sp")

    @property
    def fp(self):
        return get_register("$fp")

    __ptrsize = None
    @property
    def ptrsize(self):
        if not self.__ptrsize:
            res = cached_lookup_type("size_t")
            if res is not None:
                self.__ptrsize = res.sizeof
            else:
                self.__ptrsize = gdb.parse_and_eval("$pc").type.sizeof
        return self.__ptrsize

    __endianness = None
    @property
    def endianness(self) -> Endianness:
        if not self.__endianness:
            output = gdb.execute("show endian", to_string=True).strip().lower()
            if "little endian" in output:
                self.__endianness = Endianness.LITTLE_ENDIAN
            elif "big endian" in output:
                self.__endianness = Endianness.BIG_ENDIAN
            else:
                raise EnvironmentError(f"No valid endianess found in '{output}'")
        return self.__endianness

    def get_ith_parameter(self, i, in_func=True):
        """Retrieves the correct parameter used for the current function call."""
        reg = self.function_parameters[i]
        val = get_register(reg)
        key = reg
        return key, val


class GenericArchitecture(Architecture):
    arch = "Generic"
    mode = ""
    all_registers = ()
    instruction_length = 0
    return_register = ""
    function_parameters = ()
    syscall_register = ""
    syscall_instructions = ()
    nop_insn = b""
    flag_register = None
    flags_table = None
    def flag_register_to_human(self, val=None):    raise NotImplemented
    def is_call(self, insn):                       raise NotImplemented
    def is_ret(self, insn):                        raise NotImplemented
    def is_conditional_branch(self, insn):         raise NotImplemented
    def is_branch_taken(self, insn):               raise NotImplemented
    def get_ra(self, insn, frame):                 raise NotImplemented


class RISCV(Architecture):
    arch = "RISCV"
    mode = "RISCV"

    all_registers = ["$zero", "$ra", "$sp", "$gp", "$tp", "$t0", "$t1",
                     "$t2", "$fp", "$s1", "$a0", "$a1", "$a2", "$a3",
                     "$a4", "$a5", "$a6", "$a7", "$s2", "$s3", "$s4",
                     "$s5", "$s6", "$s7", "$s8", "$s9", "$s10", "$s11",
                     "$t3", "$t4", "$t5", "$t6",]
    return_register = "$a0"
    function_parameters = ["$a0", "$a1", "$a2", "$a3", "$a4", "$a5", "$a6", "$a7"]
    syscall_register = "$a7"
    syscall_instructions = ["ecall"]
    nop_insn = b"\x00\x00\x00\x13"
    # RISC-V has no flags registers
    flag_register = None
    flag_register_to_human = None
    flags_table = None

    @property
    def instruction_length(self):
        return 4

    def is_call(self, insn):
        return insn.mnemonic == "call"

    def is_ret(self, insn):
        mnemo = insn.mnemonic
        if mnemo == "ret":
            return True
        elif (mnemo == "jalr" and insn.operands[0] == "zero" and
              insn.operands[1] == "ra" and insn.operands[2] == 0):
            return True
        elif (mnemo == "c.jalr" and insn.operands[0] == "ra"):
            return True
        return False

    @classmethod
    def mprotect_asm(cls, addr, size, perm):
        raise OSError("Architecture {:s} not supported yet".format(cls.arch))

    def is_conditional_branch(self, insn):
        return insn.mnemonic.startswith("b")

    def is_branch_taken(self, insn):
        def long_to_twos_complement(v):
            """Convert a python long value to its two's complement."""
            if is_32bit():
                if v & 0x80000000:
                    return v - 0x100000000
            elif is_64bit():
                if v & 0x8000000000000000:
                    return v - 0x10000000000000000
            else:
                raise OSError("RISC-V: ELF file is not ELF32 or ELF64. This is not currently supported")
            return v

        mnemo = insn.mnemonic
        condition = mnemo[1:]

        if condition.endswith("z"):
            # r2 is the zero register if we are comparing to 0
            rs1 = get_register(insn.operands[0])
            rs2 = get_register("$zero")
            condition = condition[:-1]
        elif len(insn.operands) > 2:
            # r2 is populated with the second operand
            rs1 = get_register(insn.operands[0])
            rs2 = get_register(insn.operands[1])
        else:
            raise OSError("RISC-V: Failed to get rs1 and rs2 for instruction: `{}`".format(insn))

        # If the conditional operation is not unsigned, convert the python long into
        # its two's complement
        if not condition.endswith("u"):
            rs2 = long_to_twos_complement(rs2)
            rs1 = long_to_twos_complement(rs1)
        else:
            condition = condition[:-1]

        if condition == "eq":
            if rs1 == rs2: taken, reason = True, "{}={}".format(rs1, rs2)
            else: taken, reason = False, "{}!={}".format(rs1, rs2)
        elif condition == "ne":
            if rs1 != rs2: taken, reason = True, "{}!={}".format(rs1, rs2)
            else: taken, reason = False, "{}={}".format(rs1, rs2)
        elif condition == "lt":
            if rs1 < rs2: taken, reason = True, "{}<{}".format(rs1, rs2)
            else: taken, reason = False, "{}>={}".format(rs1, rs2)
        elif condition == "ge":
            if rs1 < rs2: taken, reason = True, "{}>={}".format(rs1, rs2)
            else: taken, reason = False, "{}<{}".format(rs1, rs2)
        else:
            raise OSError("RISC-V: Conditional instruction `{:s}` not supported yet".format(insn))

        return taken, reason

    def get_ra(self, insn, frame):
        ra = None
        if self.is_ret(insn):
            ra = get_register("$ra")
        elif frame.older():
            ra = frame.older().pc()
        return ra


class ARM(Architecture):
    arch = "ARM"

    all_registers = ["$r0", "$r1", "$r2", "$r3", "$r4", "$r5", "$r6",
                     "$r7", "$r8", "$r9", "$r10", "$r11", "$r12", "$sp",
                     "$lr", "$pc", "$cpsr",]

    # http://infocenter.arm.com/help/index.jsp?topic=/com.arm.doc.dui0041c/Caccegih.html
    nop_insn = b"\x01\x10\xa0\xe1" # mov r1, r1
    return_register = "$r0"
    flag_register = "$cpsr"
    flags_table = {
        31: "negative",
        30: "zero",
        29: "carry",
        28: "overflow",
        7: "interrupt",
        6: "fast",
        5: "thumb",
    }
    function_parameters = ["$r0", "$r1", "$r2", "$r3"]
    syscall_register = "$r7"
    syscall_instructions = ["swi 0x0", "swi NR"]

    def is_thumb(self):
        """Determine if the machine is currently in THUMB mode."""
        return is_alive() and get_register(self.flag_register) & (1 << 5)

    @property
    def pc(self):
        pc = get_register("$pc")
        if self.is_thumb():
            pc += 1
        return pc

    @property
    def mode(self):
        return "THUMB" if self.is_thumb() else "ARM"

    @property
    def instruction_length(self):
        # Thumb instructions have variable-length (2 or 4-byte)
        return None if self.is_thumb() else 4

    def is_call(self, insn):
        mnemo = insn.mnemonic
        call_mnemos = {"bl", "blx"}
        return mnemo in call_mnemos

    def is_ret(self, insn):
        pop_mnemos = {"pop"}
        branch_mnemos = {"bl", "bx"}
        write_mnemos = {"ldr", "add"}
        if insn.mnemonic in pop_mnemos:
            return insn.operands[-1] == " pc}"
        if insn.mnemonic in branch_mnemos:
            return insn.operands[-1] == "lr"
        if insn.mnemonic in write_mnemos:
            return insn.operands[0] == "pc"
        return

    def flag_register_to_human(self, val=None):
        # http://www.botskool.com/user-pages/tutorials/electronics/arm-7-tutorial-part-1
        if val is None:
            reg = self.flag_register
            val = get_register(reg)
        return flags_to_human(val, self.flags_table)

    def is_conditional_branch(self, insn):
        conditions = {"eq", "ne", "lt", "le", "gt", "ge", "vs", "vc", "mi", "pl", "hi", "ls", "cc", "cs"}
        return insn.mnemonic[-2:] in conditions

    def is_branch_taken(self, insn):
        mnemo = insn.mnemonic
        # ref: http://www.davespace.co.uk/arm/introduction-to-arm/conditional.html
        flags = dict((self.flags_table[k], k) for k in self.flags_table)
        val = get_register(self.flag_register)
        taken, reason = False, ""

        if mnemo.endswith("eq"): taken, reason = bool(val&(1<<flags["zero"])), "Z"
        elif mnemo.endswith("ne"): taken, reason = not val&(1<<flags["zero"]), "!Z"
        elif mnemo.endswith("lt"):
            taken, reason = bool(val&(1<<flags["negative"])) != bool(val&(1<<flags["overflow"])), "N!=V"
        elif mnemo.endswith("le"):
            taken, reason = val&(1<<flags["zero"]) or \
                bool(val&(1<<flags["negative"])) != bool(val&(1<<flags["overflow"])), "Z || N!=V"
        elif mnemo.endswith("gt"):
            taken, reason = val&(1<<flags["zero"]) == 0 and \
                bool(val&(1<<flags["negative"])) == bool(val&(1<<flags["overflow"])), "!Z && N==V"
        elif mnemo.endswith("ge"):
            taken, reason = bool(val&(1<<flags["negative"])) == bool(val&(1<<flags["overflow"])), "N==V"
        elif mnemo.endswith("vs"): taken, reason = bool(val&(1<<flags["overflow"])), "V"
        elif mnemo.endswith("vc"): taken, reason = not val&(1<<flags["overflow"]), "!V"
        elif mnemo.endswith("mi"):
            taken, reason = bool(val&(1<<flags["negative"])), "N"
        elif mnemo.endswith("pl"):
            taken, reason = not val&(1<<flags["negative"]), "N==0"
        elif mnemo.endswith("hi"):
            taken, reason = val&(1<<flags["carry"]) and not val&(1<<flags["zero"]), "C && !Z"
        elif mnemo.endswith("ls"):
            taken, reason = not val&(1<<flags["carry"]) or val&(1<<flags["zero"]), "!C || Z"
        elif mnemo.endswith("cs"): taken, reason = bool(val&(1<<flags["carry"])), "C"
        elif mnemo.endswith("cc"): taken, reason = not val&(1<<flags["carry"]), "!C"
        return taken, reason

    def get_ra(self, insn, frame):
        ra = None
        if self.is_ret(insn):
            # If it's a pop, we have to peek into the stack, otherwise use lr
            if insn.mnemonic == "pop":
                ra_addr = gef.arch.sp + (len(insn.operands)-1) * get_memory_alignment()
                ra = to_unsigned_long(dereference(ra_addr))
            elif insn.mnemonic == "ldr":
                return to_unsigned_long(dereference(gef.arch.sp))
            else:  # 'bx lr' or 'add pc, lr, #0'
                return get_register("$lr")
        elif frame.older():
            ra = frame.older().pc()
        return ra

    @classmethod
    def mprotect_asm(cls, addr, size, perm):
        _NR_mprotect = 125
        insns = [
            "push {r0-r2, r7}",
            "mov r1, {:d}".format(addr & 0xffff),
            "mov r0, {:d}".format((addr & 0xffff0000) >> 16),
            "lsl r0, r0, 16",
            "add r0, r0, r1",
            "mov r1, {:d}".format(size & 0xffff),
            "mov r2, {:d}".format(perm & 0xff),
            "mov r7, {:d}".format(_NR_mprotect),
            "svc 0",
            "pop {r0-r2, r7}",
        ]
        return "; ".join(insns)


class AARCH64(ARM):
    arch = "ARM64"
    mode = ""

    all_registers = [
        "$x0", "$x1", "$x2", "$x3", "$x4", "$x5", "$x6", "$x7",
        "$x8", "$x9", "$x10", "$x11", "$x12", "$x13", "$x14","$x15",
        "$x16", "$x17", "$x18", "$x19", "$x20", "$x21", "$x22", "$x23",
        "$x24", "$x25", "$x26", "$x27", "$x28", "$x29", "$x30", "$sp",
        "$pc", "$cpsr", "$fpsr", "$fpcr",]
    return_register = "$x0"
    flag_register = "$cpsr"
    flags_table = {
        31: "negative",
        30: "zero",
        29: "carry",
        28: "overflow",
        7: "interrupt",
        6: "fast",
    }
    function_parameters = ["$x0", "$x1", "$x2", "$x3", "$x4", "$x5", "$x6", "$x7"]
    syscall_register = "$x8"
    syscall_instructions = ["svc $x0"]

    def is_call(self, insn):
        mnemo = insn.mnemonic
        call_mnemos = {"bl", "blr"}
        return mnemo in call_mnemos

    def flag_register_to_human(self, val=None):
        # http://events.linuxfoundation.org/sites/events/files/slides/KoreaLinuxForum-2014.pdf
        reg = self.flag_register
        if not val:
            val = get_register(reg)
        return flags_to_human(val, self.flags_table)

    @classmethod
    def mprotect_asm(cls, addr, size, perm):
        _NR_mprotect = 226
        insns = [
            "str x8, [sp, -16]!",
            "str x0, [sp, -16]!",
            "str x1, [sp, -16]!",
            "str x2, [sp, -16]!",
            "mov x8, {:d}".format(_NR_mprotect),
            "movz x0, 0x{:x}".format(addr & 0xFFFF),
            "movk x0, 0x{:x}, lsl 16".format((addr >> 16) & 0xFFFF),
            "movk x0, 0x{:x}, lsl 32".format((addr >> 32) & 0xFFFF),
            "movk x0, 0x{:x}, lsl 48".format((addr >> 48) & 0xFFFF),
            "movz x1, 0x{:x}".format(size & 0xFFFF),
            "movk x1, 0x{:x}, lsl 16".format((size >> 16) & 0xFFFF),
            "mov x2, {:d}".format(perm),
            "svc 0",
            "ldr x2, [sp], 16",
            "ldr x1, [sp], 16",
            "ldr x0, [sp], 16",
            "ldr x8, [sp], 16",
        ]
        return "; ".join(insns)

    def is_conditional_branch(self, insn):
        # https://www.element14.com/community/servlet/JiveServlet/previewBody/41836-102-1-229511/ARM.Reference_Manual.pdf
        # sect. 5.1.1
        mnemo = insn.mnemonic
        branch_mnemos = {"cbnz", "cbz", "tbnz", "tbz"}
        return mnemo.startswith("b.") or mnemo in branch_mnemos

    def is_branch_taken(self, insn):
        mnemo, operands = insn.mnemonic, insn.operands
        taken, reason = False, ""

        if mnemo in {"cbnz", "cbz", "tbnz", "tbz"}:
            reg = "${}".format(operands[0])
            op = get_register(reg)
            if mnemo == "cbnz":
                if op!=0: taken, reason = True, "{}!=0".format(reg)
                else: taken, reason = False, "{}==0".format(reg)
            elif mnemo == "cbz":
                if op == 0: taken, reason = True, "{}==0".format(reg)
                else: taken, reason = False, "{}!=0".format(reg)
            elif mnemo == "tbnz":
                # operands[1] has one or more white spaces in front, then a #, then the number
                # so we need to eliminate them
                i = int(operands[1].strip().lstrip("#"))
                if (op & 1<<i) != 0: taken, reason = True, "{}&1<<{}!=0".format(reg, i)
                else: taken, reason = False, "{}&1<<{}==0".format(reg, i)
            elif mnemo == "tbz":
                # operands[1] has one or more white spaces in front, then a #, then the number
                # so we need to eliminate them
                i = int(operands[1].strip().lstrip("#"))
                if (op & 1<<i) == 0: taken, reason = True, "{}&1<<{}==0".format(reg, i)
                else: taken, reason = False, "{}&1<<{}!=0".format(reg, i)

        if not reason:
            taken, reason = super().is_branch_taken(insn)
        return taken, reason


class X86(Architecture):
    arch = "X86"
    mode = "32"

    nop_insn = b"\x90"
    flag_register = "$eflags"
    special_registers = ["$cs", "$ss", "$ds", "$es", "$fs", "$gs", ]
    gpr_registers = ["$eax", "$ebx", "$ecx", "$edx", "$esp", "$ebp", "$esi", "$edi", "$eip", ]
    all_registers = gpr_registers + [ flag_register, ] + special_registers
    instruction_length = None
    return_register = "$eax"
    function_parameters = ["$esp", ]
    flags_table = {
        6: "zero",
        0: "carry",
        2: "parity",
        4: "adjust",
        7: "sign",
        8: "trap",
        9: "interrupt",
        10: "direction",
        11: "overflow",
        16: "resume",
        17: "virtualx86",
        21: "identification",
    }
    syscall_register = "$eax"
    syscall_instructions = ["sysenter", "int 0x80"]

    def flag_register_to_human(self, val=None):
        reg = self.flag_register
        if not val:
            val = get_register(reg)
        return flags_to_human(val, self.flags_table)

    def is_call(self, insn):
        mnemo = insn.mnemonic
        call_mnemos = {"call", "callq"}
        return mnemo in call_mnemos

    def is_ret(self, insn):
        return insn.mnemonic == "ret"

    def is_conditional_branch(self, insn):
        mnemo = insn.mnemonic
        branch_mnemos = {
            "ja", "jnbe", "jae", "jnb", "jnc", "jb", "jc", "jnae", "jbe", "jna",
            "jcxz", "jecxz", "jrcxz", "je", "jz", "jg", "jnle", "jge", "jnl",
            "jl", "jnge", "jle", "jng", "jne", "jnz", "jno", "jnp", "jpo", "jns",
            "jo", "jp", "jpe", "js"
        }
        return mnemo in branch_mnemos

    def is_branch_taken(self, insn):
        mnemo = insn.mnemonic
        # all kudos to fG! (https://github.com/gdbinit/Gdbinit/blob/master/gdbinit#L1654)
        flags = dict((self.flags_table[k], k) for k in self.flags_table)
        val = get_register(self.flag_register)

        taken, reason = False, ""

        if mnemo in ("ja", "jnbe"):
            taken, reason = not val&(1<<flags["carry"]) and not val&(1<<flags["zero"]), "!C && !Z"
        elif mnemo in ("jae", "jnb", "jnc"):
            taken, reason = not val&(1<<flags["carry"]), "!C"
        elif mnemo in ("jb", "jc", "jnae"):
            taken, reason = val&(1<<flags["carry"]), "C"
        elif mnemo in ("jbe", "jna"):
            taken, reason = val&(1<<flags["carry"]) or val&(1<<flags["zero"]), "C || Z"
        elif mnemo in ("jcxz", "jecxz", "jrcxz"):
            cx = get_register("$rcx") if self.mode == 64 else get_register("$ecx")
            taken, reason = cx == 0, "!$CX"
        elif mnemo in ("je", "jz"):
            taken, reason = val&(1<<flags["zero"]), "Z"
        elif mnemo in ("jne", "jnz"):
            taken, reason = not val&(1<<flags["zero"]), "!Z"
        elif mnemo in ("jg", "jnle"):
            taken, reason = not val&(1<<flags["zero"]) and bool(val&(1<<flags["overflow"])) == bool(val&(1<<flags["sign"])), "!Z && S==O"
        elif mnemo in ("jge", "jnl"):
            taken, reason = bool(val&(1<<flags["sign"])) == bool(val&(1<<flags["overflow"])), "S==O"
        elif mnemo in ("jl", "jnge"):
            taken, reason = val&(1<<flags["overflow"]) != val&(1<<flags["sign"]), "S!=O"
        elif mnemo in ("jle", "jng"):
            taken, reason = val&(1<<flags["zero"]) or bool(val&(1<<flags["overflow"])) != bool(val&(1<<flags["sign"])), "Z || S!=O"
        elif mnemo in ("jo",):
            taken, reason = val&(1<<flags["overflow"]), "O"
        elif mnemo in ("jno",):
            taken, reason = not val&(1<<flags["overflow"]), "!O"
        elif mnemo in ("jpe", "jp"):
            taken, reason = val&(1<<flags["parity"]), "P"
        elif mnemo in ("jnp", "jpo"):
            taken, reason = not val&(1<<flags["parity"]), "!P"
        elif mnemo in ("js",):
            taken, reason = val&(1<<flags["sign"]), "S"
        elif mnemo in ("jns",):
            taken, reason = not val&(1<<flags["sign"]), "!S"
        return taken, reason

    def get_ra(self, insn, frame):
        ra = None
        if self.is_ret(insn):
            ra = to_unsigned_long(dereference(gef.arch.sp))
        if frame.older():
            ra = frame.older().pc()

        return ra

    @classmethod
    def mprotect_asm(cls, addr, size, perm):
        _NR_mprotect = 125
        insns = [
            "pushad",
            "mov eax, {:d}".format(_NR_mprotect),
            "mov ebx, {:d}".format(addr),
            "mov ecx, {:d}".format(size),
            "mov edx, {:d}".format(perm),
            "int 0x80",
            "popad",
        ]
        return "; ".join(insns)

    def get_ith_parameter(self, i, in_func=True):
        if in_func:
            i += 1  # Account for RA being at the top of the stack
        sp = gef.arch.sp
        sz = gef.arch.ptrsize
        loc = sp + (i * sz)
        val = gef.memory.read_integer(loc)
        key = "[sp + {:#x}]".format(i * sz)
        return key, val


class X86_64(X86):
    arch = "X86"
    mode = "64"

    gpr_registers = [
        "$rax", "$rbx", "$rcx", "$rdx", "$rsp", "$rbp", "$rsi", "$rdi", "$rip",
        "$r8", "$r9", "$r10", "$r11", "$r12", "$r13", "$r14", "$r15", ]
    all_registers = gpr_registers + [ X86.flag_register, ] + X86.special_registers
    return_register = "$rax"
    function_parameters = ["$rdi", "$rsi", "$rdx", "$rcx", "$r8", "$r9"]
    syscall_register = "$rax"
    syscall_instructions = ["syscall"]
    # We don't want to inherit x86's stack based param getter
    get_ith_parameter = Architecture.get_ith_parameter

    @classmethod
    def mprotect_asm(cls, addr, size, perm):
        _NR_mprotect = 10
        insns = [
            "push rax",
            "push rdi",
            "push rsi",
            "push rdx",
            "push rcx",
            "push r11",
            "mov rax, {:d}".format(_NR_mprotect),
            "mov rdi, {:d}".format(addr),
            "mov rsi, {:d}".format(size),
            "mov rdx, {:d}".format(perm),
            "syscall",
            "pop r11",
            "pop rcx",
            "pop rdx",
            "pop rsi",
            "pop rdi",
            "pop rax",
        ]
        return "; ".join(insns)


class PowerPC(Architecture):
    arch = "PPC"
    mode = "PPC32"

    all_registers = [
        "$r0", "$r1", "$r2", "$r3", "$r4", "$r5", "$r6", "$r7",
        "$r8", "$r9", "$r10", "$r11", "$r12", "$r13", "$r14", "$r15",
        "$r16", "$r17", "$r18", "$r19", "$r20", "$r21", "$r22", "$r23",
        "$r24", "$r25", "$r26", "$r27", "$r28", "$r29", "$r30", "$r31",
        "$pc", "$msr", "$cr", "$lr", "$ctr", "$xer", "$trap",]
    instruction_length = 4
    nop_insn = b"\x60\x00\x00\x00" # http://www.ibm.com/developerworks/library/l-ppc/index.html
    return_register = "$r0"
    flag_register = "$cr"
    flags_table = {
        3: "negative[0]",
        2: "positive[0]",
        1: "equal[0]",
        0: "overflow[0]",
        # cr7
        31: "less[7]",
        30: "greater[7]",
        29: "equal[7]",
        28: "overflow[7]",
    }
    function_parameters = ["$i0", "$i1", "$i2", "$i3", "$i4", "$i5"]
    syscall_register = "$r0"
    syscall_instructions = ["sc"]

    def flag_register_to_human(self, val=None):
        # http://www.cebix.net/downloads/bebox/pem32b.pdf (% 2.1.3)
        if not val:
            reg = self.flag_register
            val = get_register(reg)
        return flags_to_human(val, self.flags_table)

    def is_call(self, insn):
        return False

    def is_ret(self, insn):
        return insn.mnemonic == "blr"

    def is_conditional_branch(self, insn):
        mnemo = insn.mnemonic
        branch_mnemos = {"beq", "bne", "ble", "blt", "bgt", "bge"}
        return mnemo in branch_mnemos

    def is_branch_taken(self, insn):
        mnemo = insn.mnemonic
        flags = dict((self.flags_table[k], k) for k in self.flags_table)
        val = get_register(self.flag_register)
        taken, reason = False, ""
        if mnemo == "beq": taken, reason = val&(1<<flags["equal[7]"]), "E"
        elif mnemo == "bne": taken, reason = val&(1<<flags["equal[7]"]) == 0, "!E"
        elif mnemo == "ble": taken, reason = val&(1<<flags["equal[7]"]) or val&(1<<flags["less[7]"]), "E || L"
        elif mnemo == "blt": taken, reason = val&(1<<flags["less[7]"]), "L"
        elif mnemo == "bge": taken, reason = val&(1<<flags["equal[7]"]) or val&(1<<flags["greater[7]"]), "E || G"
        elif mnemo == "bgt": taken, reason = val&(1<<flags["greater[7]"]), "G"
        return taken, reason

    def get_ra(self, insn, frame):
        ra = None
        if self.is_ret(insn):
            ra = get_register("$lr")
        elif frame.older():
            ra = frame.older().pc()
        return ra

    @classmethod
    def mprotect_asm(cls, addr, size, perm):
        # Ref: http://www.ibm.com/developerworks/library/l-ppc/index.html
        _NR_mprotect = 125
        insns = [
            "addi 1, 1, -16",  # 1 = r1 = sp
            "stw 0, 0(1)",
            "stw 3, 4(1)",  # r0 = syscall_code | r3, r4, r5 = args
            "stw 4, 8(1)",
            "stw 5, 12(1)",
            "li 0, {:d}".format(_NR_mprotect),
            "lis 3, {:#x}@h".format(addr),
            "ori 3, 3, {:#x}@l".format(addr),
            "lis 4, {:#x}@h".format(size),
            "ori 4, 4, {:#x}@l".format(size),
            "li 5, {:d}".format(perm),
            "sc",
            "lwz 0, 0(1)",
            "lwz 3, 4(1)",
            "lwz 4, 8(1)",
            "lwz 5, 12(1)",
            "addi 1, 1, 16",
        ]
        return ";".join(insns)


class PowerPC64(PowerPC):
    arch = "PPC"
    mode = "PPC64"


class SPARC(Architecture):
    """ Refs:
    - http://www.cse.scu.edu/~atkinson/teaching/sp05/259/sparc.pdf
    """
    arch = "SPARC"
    mode = ""

    all_registers = [
        "$g0", "$g1", "$g2", "$g3", "$g4", "$g5", "$g6", "$g7",
        "$o0", "$o1", "$o2", "$o3", "$o4", "$o5", "$o7",
        "$l0", "$l1", "$l2", "$l3", "$l4", "$l5", "$l6", "$l7",
        "$i0", "$i1", "$i2", "$i3", "$i4", "$i5", "$i7",
        "$pc", "$npc", "$sp ", "$fp ", "$psr",]
    instruction_length = 4
    nop_insn = b"\x00\x00\x00\x00"  # sethi 0, %g0
    return_register = "$i0"
    flag_register = "$psr"
    flags_table = {
        23: "negative",
        22: "zero",
        21: "overflow",
        20: "carry",
        7: "supervisor",
        5: "trap",
    }
    function_parameters = ["$o0 ", "$o1 ", "$o2 ", "$o3 ", "$o4 ", "$o5 ", "$o7 ",]
    syscall_register = "%g1"
    syscall_instructions = ["t 0x10"]

    def flag_register_to_human(self, val=None):
        # http://www.gaisler.com/doc/sparcv8.pdf
        reg = self.flag_register
        if not val:
            val = get_register(reg)
        return flags_to_human(val, self.flags_table)

    def is_call(self, insn):
        return False

    def is_ret(self, insn):
        return insn.mnemonic == "ret"

    def is_conditional_branch(self, insn):
        mnemo = insn.mnemonic
        # http://moss.csc.ncsu.edu/~mueller/codeopt/codeopt00/notes/condbranch.html
        branch_mnemos = {
            "be", "bne", "bg", "bge", "bgeu", "bgu", "bl", "ble", "blu", "bleu",
            "bneg", "bpos", "bvs", "bvc", "bcs", "bcc"
        }
        return mnemo in branch_mnemos

    def is_branch_taken(self, insn):
        mnemo = insn.mnemonic
        flags = dict((self.flags_table[k], k) for k in self.flags_table)
        val = get_register(self.flag_register)
        taken, reason = False, ""

        if mnemo == "be": taken, reason = val&(1<<flags["zero"]), "Z"
        elif mnemo == "bne": taken, reason = val&(1<<flags["zero"]) == 0, "!Z"
        elif mnemo == "bg": taken, reason = val&(1<<flags["zero"]) == 0 and (val&(1<<flags["negative"]) == 0 or val&(1<<flags["overflow"]) == 0), "!Z && (!N || !O)"
        elif mnemo == "bge": taken, reason = val&(1<<flags["negative"]) == 0 or val&(1<<flags["overflow"]) == 0, "!N || !O"
        elif mnemo == "bgu": taken, reason = val&(1<<flags["carry"]) == 0 and val&(1<<flags["zero"]) == 0, "!C && !Z"
        elif mnemo == "bgeu": taken, reason = val&(1<<flags["carry"]) == 0, "!C"
        elif mnemo == "bl": taken, reason = val&(1<<flags["negative"]) and val&(1<<flags["overflow"]), "N && O"
        elif mnemo == "blu": taken, reason = val&(1<<flags["carry"]), "C"
        elif mnemo == "ble": taken, reason = val&(1<<flags["zero"]) or (val&(1<<flags["negative"]) or val&(1<<flags["overflow"])), "Z || (N || O)"
        elif mnemo == "bleu": taken, reason = val&(1<<flags["carry"]) or val&(1<<flags["zero"]), "C || Z"
        elif mnemo == "bneg": taken, reason = val&(1<<flags["negative"]), "N"
        elif mnemo == "bpos": taken, reason = val&(1<<flags["negative"]) == 0, "!N"
        elif mnemo == "bvs": taken, reason = val&(1<<flags["overflow"]), "O"
        elif mnemo == "bvc": taken, reason = val&(1<<flags["overflow"]) == 0, "!O"
        elif mnemo == "bcs": taken, reason = val&(1<<flags["carry"]), "C"
        elif mnemo == "bcc": taken, reason = val&(1<<flags["carry"]) == 0, "!C"
        return taken, reason

    def get_ra(self, insn, frame):
        ra = None
        if self.is_ret(insn):
            ra = get_register("$o7")
        elif frame.older():
            ra = frame.older().pc()
        return ra

    @classmethod
    def mprotect_asm(cls, addr, size, perm):
        hi = (addr & 0xffff0000) >> 16
        lo = (addr & 0x0000ffff)
        _NR_mprotect = 125
        insns = ["add %sp, -16, %sp",
                 "st %g1, [ %sp ]", "st %o0, [ %sp + 4 ]",
                 "st %o1, [ %sp + 8 ]", "st %o2, [ %sp + 12 ]",
                 "sethi  %hi({}), %o0".format(hi),
                 "or  %o0, {}, %o0".format(lo),
                 "clr  %o1",
                 "clr  %o2",
                 "mov  {}, %g1".format(_NR_mprotect),
                 "t 0x10",
                 "ld [ %sp ], %g1", "ld [ %sp + 4 ], %o0",
                 "ld [ %sp + 8 ], %o1", "ld [ %sp + 12 ], %o2",
                 "add %sp, 16, %sp",]
        return "; ".join(insns)


class SPARC64(SPARC):
    """Refs:
    - http://math-atlas.sourceforge.net/devel/assembly/abi_sysV_sparc.pdf
    - https://cr.yp.to/2005-590/sparcv9.pdf
    """

    arch = "SPARC"
    mode = "V9"

    all_registers = [
        "$g0", "$g1", "$g2", "$g3", "$g4", "$g5", "$g6", "$g7",
        "$o0", "$o1", "$o2", "$o3", "$o4", "$o5", "$o7",
        "$l0", "$l1", "$l2", "$l3", "$l4", "$l5", "$l6", "$l7",
        "$i0", "$i1", "$i2", "$i3", "$i4", "$i5", "$i7",
        "$pc", "$npc", "$sp", "$fp", "$state", ]

    flag_register = "$state"  # sparcv9.pdf, 5.1.5.1 (ccr)
    flags_table = {
        35: "negative",
        34: "zero",
        33: "overflow",
        32: "carry",
    }

    syscall_instructions = ["t 0x6d"]

    @classmethod
    def mprotect_asm(cls, addr, size, perm):
        hi = (addr & 0xffff0000) >> 16
        lo = (addr & 0x0000ffff)
        _NR_mprotect = 125
        insns = ["add %sp, -16, %sp",
                 "st %g1, [ %sp ]", "st %o0, [ %sp + 4 ]",
                 "st %o1, [ %sp + 8 ]", "st %o2, [ %sp + 12 ]",
                 "sethi  %hi({}), %o0".format(hi),
                 "or  %o0, {}, %o0".format(lo),
                 "clr  %o1",
                 "clr  %o2",
                 "mov  {}, %g1".format(_NR_mprotect),
                 "t 0x6d",
                 "ld [ %sp ], %g1", "ld [ %sp + 4 ], %o0",
                 "ld [ %sp + 8 ], %o1", "ld [ %sp + 12 ], %o2",
                 "add %sp, 16, %sp",]
        return "; ".join(insns)


class MIPS(Architecture):
    arch = "MIPS"
    mode = "MIPS32"

    # http://vhouten.home.xs4all.nl/mipsel/r3000-isa.html
    all_registers = [
        "$zero", "$at", "$v0", "$v1", "$a0", "$a1", "$a2", "$a3",
        "$t0", "$t1", "$t2", "$t3", "$t4", "$t5", "$t6", "$t7",
        "$s0", "$s1", "$s2", "$s3", "$s4", "$s5", "$s6", "$s7",
        "$t8", "$t9", "$k0", "$k1", "$s8", "$pc", "$sp", "$hi",
        "$lo", "$fir", "$ra", "$gp", ]
    instruction_length = 4
    nop_insn = b"\x00\x00\x00\x00"  # sll $0,$0,0
    return_register = "$v0"
    flag_register = "$fcsr"
    flags_table = {}
    function_parameters = ["$a0", "$a1", "$a2", "$a3"]
    syscall_register = "$v0"
    syscall_instructions = ["syscall"]

    def flag_register_to_human(self, val=None):
        return Color.colorify("No flag register", "yellow underline")

    def is_call(self, insn):
        return False

    def is_ret(self, insn):
        return insn.mnemonic == "jr" and insn.operands[0] == "ra"

    def is_conditional_branch(self, insn):
        mnemo = insn.mnemonic
        branch_mnemos = {"beq", "bne", "beqz", "bnez", "bgtz", "bgez", "bltz", "blez"}
        return mnemo in branch_mnemos

    def is_branch_taken(self, insn):
        mnemo, ops = insn.mnemonic, insn.operands
        taken, reason = False, ""

        if mnemo == "beq":
            taken, reason = get_register(ops[0]) == get_register(ops[1]), "{0[0]} == {0[1]}".format(ops)
        elif mnemo == "bne":
            taken, reason = get_register(ops[0]) != get_register(ops[1]), "{0[0]} != {0[1]}".format(ops)
        elif mnemo == "beqz":
            taken, reason = get_register(ops[0]) == 0, "{0[0]} == 0".format(ops)
        elif mnemo == "bnez":
            taken, reason = get_register(ops[0]) != 0, "{0[0]} != 0".format(ops)
        elif mnemo == "bgtz":
            taken, reason = get_register(ops[0]) > 0, "{0[0]} > 0".format(ops)
        elif mnemo == "bgez":
            taken, reason = get_register(ops[0]) >= 0, "{0[0]} >= 0".format(ops)
        elif mnemo == "bltz":
            taken, reason = get_register(ops[0]) < 0, "{0[0]} < 0".format(ops)
        elif mnemo == "blez":
            taken, reason = get_register(ops[0]) <= 0, "{0[0]} <= 0".format(ops)
        return taken, reason

    def get_ra(self, insn, frame):
        ra = None
        if self.is_ret(insn):
            ra = get_register("$ra")
        elif frame.older():
            ra = frame.older().pc()
        return ra

    @classmethod
    def mprotect_asm(cls, addr, size, perm):
        _NR_mprotect = 4125
        insns = ["addi $sp, $sp, -16",
                 "sw $v0, 0($sp)", "sw $a0, 4($sp)",
                 "sw $a3, 8($sp)", "sw $a3, 12($sp)",
                 "li $v0, {:d}".format(_NR_mprotect),
                 "li $a0, {:d}".format(addr),
                 "li $a1, {:d}".format(size),
                 "li $a2, {:d}".format(perm),
                 "syscall",
                 "lw $v0, 0($sp)", "lw $a1, 4($sp)",
                 "lw $a3, 8($sp)", "lw $a3, 12($sp)",
                 "addi $sp, $sp, 16",]
        return "; ".join(insns)





def copy_to_clipboard(data):
    """Helper function to submit data to the clipboard"""
    if sys.platform == "linux":
        xclip = which("xclip")
        prog = [xclip, "-selection", "clipboard", "-i"]
    elif sys.platform == "darwin":
        pbcopy = which("pbcopy")
        prog = [pbcopy]
    else:
        raise NotImplementedError("copy: Unsupported OS")

    with subprocess.Popen(prog, stdin=subprocess.PIPE) as p:
        p.stdin.write(data)
        p.stdin.close()
        p.wait()
    return


def use_stdtype():
    if is_32bit(): return "uint32_t"
    elif is_64bit(): return "uint64_t"
    return "uint16_t"


def use_default_type():
    if is_32bit(): return "unsigned int"
    elif is_64bit(): return "unsigned long"
    return "unsigned short"


def use_golang_type():
    if is_32bit(): return "uint32"
    elif is_64bit(): return "uint64"
    return "uint16"


def use_rust_type():
    if is_32bit(): return "u32"
    elif is_64bit(): return "u64"
    return "u16"


def to_unsigned_long(v):
    """Cast a gdb.Value to unsigned long."""
    mask = (1 << 64) - 1
    return int(v.cast(gdb.Value(mask).type)) & mask


def get_register(regname):
    """Return a register's value."""
    curframe = gdb.selected_frame()
    key = curframe.pc() ^ int(curframe.read_register('sp')) # todo: check when/if gdb.Frame implements `level()`
    return __get_register_for_selected_frame(regname, key)


@lru_cache()
def __get_register_for_selected_frame(regname, hash_key):
    # 1st chance
    try:
        return parse_address(regname)
    except gdb.error:
        pass

    # 2nd chance
    try:
        regname = regname.lstrip("$")
        value = gdb.selected_frame().read_register(regname)
        return int(value)
    except (ValueError, gdb.error):
        pass
    return None


def get_path_from_info_proc():
    for x in gdb.execute("info proc", to_string=True).splitlines():
        if x.startswith("exe = "):
            return x.split(" = ")[1].replace("'", "")
    return None


@lru_cache()
def get_os():
    """Return the current OS."""
    return platform.system().lower()


@lru_cache()
def is_qemu():
    if not is_remote_debug():
        return False
    response = gdb.execute('maintenance packet Qqemu.sstepbits', to_string=True, from_tty=False)
    return 'ENABLE=' in response


@lru_cache()
def is_qemu_usermode():
    if not is_qemu():
        return False
    response = gdb.execute('maintenance packet QOffsets', to_string=True, from_tty=False)
    return "Text=" in response


@lru_cache()
def is_qemu_system():
    if not is_qemu():
        return False
    response = gdb.execute('maintenance packet QOffsets', to_string=True, from_tty=False)
    return 'received: ""' in response


@lru_cache()
def get_pid():
    """Return the PID of the target process."""
    pid = gdb.selected_inferior().pid if not __gef_qemu_mode__ else gdb.selected_thread().ptid[1]
    if not pid:
        raise RuntimeError("cannot retrieve PID for target process")
    return pid


@lru_cache()
def get_filepath():
    """Return the local absolute path of the file currently debugged."""
    filename = gdb.current_progspace().filename

    if is_remote_debug():
        # if no filename specified, try downloading target from /proc
        if filename is None:
            pid = get_pid()
            if pid > 0:
                return download_file("/proc/{:d}/exe".format(pid), use_cache=True)
            return None

        # if target is remote file, download
        elif filename.startswith("target:"):
            fname = filename[len("target:") :]
            return download_file(fname, use_cache=True, local_name=fname)

        elif filename.startswith(".gnu_debugdata for target:"):
            fname = filename[len(".gnu_debugdata for target:") :]
            return download_file(fname, use_cache=True, local_name=fname)

        elif __gef_remote__ is not None:
            return "/tmp/gef/{:d}/{:s}".format(__gef_remote__, get_path_from_info_proc())
        return filename
    else:
        if filename is not None:
            return filename
        # inferior probably did not have name, extract cmdline from info proc
        return get_path_from_info_proc()


@lru_cache()
def get_filename():
    """Return the full filename of the file currently debugged."""
    return os.path.basename(gdb.current_progspace().filename)


@lru_cache()
def inferior_is_macho():
    """Return True if the current file is a Mach-O binary."""
    for x in gdb.execute("info files", to_string=True).splitlines():
        if "file type mach-o" in x:
            return True
    return False


@lru_cache()
def is_macho(filename):
    """Return True if the specified file is a Mach-O binary."""
    file_bin = which("file")
    cmd = [file_bin, filename]
    out = gef_execute_external(cmd)
    if "Mach-O" in out:
        return True
    return False


def download_file(target, use_cache=False, local_name=None):
    """Download filename `target` inside the mirror tree inside the gef.config["gef.tempdir"].
    The tree architecture must be gef.config["gef.tempdir"]/gef/<local_pid>/<remote_filepath>.
    This allow a "chroot-like" tree format."""

    try:
        local_root = os.path.sep.join([gef.config["gef.tempdir"], str(get_pid())])
        if local_name is None:
            local_path = os.path.sep.join([local_root, os.path.dirname(target)])
            local_name = os.path.sep.join([local_path, os.path.basename(target)])
        else:
            local_path = os.path.sep.join([local_root, os.path.dirname(local_name)])
            local_name = os.path.sep.join([local_path, os.path.basename(local_name)])

        if use_cache and os.access(local_name, os.R_OK):
            return local_name

        gef_makedirs(local_path)
        gdb.execute("remote get {0:s} {1:s}".format(target, local_name))

    except gdb.error:
        # fallback memory view
        with open(local_name, "w") as f:
            if is_32bit():
                f.write("00000000-ffffffff rwxp 00000000 00:00 0                    {}\n".format(get_filepath()))
            else:
                f.write("0000000000000000-ffffffffffffffff rwxp 00000000 00:00 0                    {}\n".format(get_filepath()))

    except Exception as e:
        err("download_file() failed: {}".format(str(e)))
        local_name = None
    return local_name


def open_file(path, use_cache=False):
    """Attempt to open the given file, if remote debugging is active, download
    it first to the mirror in /tmp/."""
    if is_remote_debug() and not __gef_qemu_mode__:
        lpath = download_file(path, use_cache)
        if not lpath:
            raise IOError("cannot open remote path {:s}".format(path))
        path = lpath

    return open(path, "r")


def get_function_length(sym):
    """Attempt to get the length of the raw bytes of a function."""
    dis = gdb.execute("disassemble {:s}".format(sym), to_string=True).splitlines()
    start_addr = int(dis[1].split()[0], 16)
    end_addr = int(dis[-2].split()[0], 16)
    return end_addr - start_addr


def get_process_maps_linux(proc_map_file):
    """Parse the Linux process `/proc/pid/maps` file."""
    with open_file(proc_map_file, use_cache=False) as f:
        file = f.readlines()
    for line in file:
        line = line.strip()
        addr, perm, off, _, rest = line.split(" ", 4)
        rest = rest.split(" ", 1)
        if len(rest) == 1:
            inode = rest[0]
            pathname = ""
        else:
            inode = rest[0]
            pathname = rest[1].lstrip()

        addr_start, addr_end = [int(x, 16) for x in addr.split("-")]
        off = int(off, 16)
        perm = Permission.from_process_maps(perm)

        yield Section(page_start=addr_start,
                      page_end=addr_end,
                      offset=off,
                      permission=perm,
                      inode=inode,
                      path=pathname)
    return


def get_mach_regions():
    sp = gef.arch.sp
    for line in gdb.execute("info mach-regions", to_string=True).splitlines():
        line = line.strip()
        addr, perm, _ = line.split(" ", 2)
        addr_start, addr_end = [int(x, 16) for x in addr.split("-")]
        perm = Permission.from_process_maps(perm.split("/")[0])

        zone = file_lookup_address(addr_start)
        if zone:
            path = zone.filename
        else:
            path = "[stack]" if sp >= addr_start and sp < addr_end else ""

        yield Section(page_start=addr_start,
                      page_end=addr_end,
                      offset=0,
                      permission=perm,
                      inode=None,
                      path=path)
    return


@lru_cache()
def get_process_maps():
    """Return the mapped memory sections"""

    if inferior_is_macho():
        return list(get_mach_regions())

    try:
        pid = get_pid()
        fpath = "/proc/{:d}/maps".format(pid)
        return list(get_process_maps_linux(fpath))
    except FileNotFoundError as e:
        warn("Failed to read /proc/<PID>/maps, using GDB sections info: {}".format(e))
        return list(get_info_sections())


@lru_cache()
def get_info_sections():
    """Retrieve the debuggee sections."""
    stream = StringIO(gdb.execute("maintenance info sections", to_string=True))

    for line in stream:
        if not line:
            break

        try:
            parts = [x for x in line.split()]
            addr_start, addr_end = [int(x, 16) for x in parts[1].split("->")]
            off = int(parts[3][:-1], 16)
            path = parts[4]
            inode = ""
            perm = Permission.from_info_sections(parts[5:])

            yield Section(page_start=addr_start,
                          page_end=addr_end,
                          offset=off,
                          permission=perm,
                          inode=inode,
                          path=path)

        except IndexError:
            continue
        except ValueError:
            continue

    return


@lru_cache()
def get_info_files():
    """Retrieve all the files loaded by debuggee."""
    lines = gdb.execute("info files", to_string=True).splitlines()

    if len(lines) < len(__infos_files__):
        return __infos_files__

    for line in lines:
        line = line.strip()

        if not line:
            break

        if not line.startswith("0x"):
            continue

        blobs = [x.strip() for x in line.split(" ")]
        addr_start = int(blobs[0], 16)
        addr_end = int(blobs[2], 16)
        section_name = blobs[4]

        if len(blobs) == 7:
            filename = blobs[6]
        else:
            filename = get_filepath()

        info = Zone(section_name, addr_start, addr_end, filename)

        __infos_files__.append(info)

    return __infos_files__


def process_lookup_address(address):
    """Look up for an address in memory.
    Return an Address object if found, None otherwise."""
    if not is_alive():
        err("Process is not running")
        return None

    if is_x86():
        if is_in_x86_kernel(address):
            return None

    for sect in get_process_maps():
        if sect.page_start <= address < sect.page_end:
            return sect

    return None


@lru_cache()
def process_lookup_path(name, perm=Permission.ALL):
    """Look up for a path in the process memory mapping.
    Return a Section object if found, None otherwise."""
    if not is_alive():
        err("Process is not running")
        return None

    for sect in get_process_maps():
        if name in sect.path and sect.permission.value & perm:
            return sect

    return None


@lru_cache()
def file_lookup_name_path(name, path):
    """Look up a file by name and path.
    Return a Zone object if found, None otherwise."""
    for xfile in get_info_files():
        if path == xfile.filename and name == xfile.name:
            return xfile
    return None


@lru_cache()
def file_lookup_address(address):
    """Look up for a file by its address.
    Return a Zone object if found, None otherwise."""
    for info in get_info_files():
        if info.zone_start <= address < info.zone_end:
            return info
    return None


@lru_cache()
def lookup_address(address):
    """Try to find the address in the process address space.
    Return an Address object, with validity flag set based on success."""
    sect = process_lookup_address(address)
    info = file_lookup_address(address)
    if sect is None and info is None:
        # i.e. there is no info on this address
        return Address(value=address, valid=False)
    return Address(value=address, section=sect, info=info)


def xor(data, key):
    """Return `data` xor-ed with `key`."""
    key = key.lstrip("0x")
    key = binascii.unhexlify(key)
    return bytearray([x ^ y for x, y in zip(data, itertools.cycle(key))])


def is_hex(pattern):
    """Return whether provided string is a hexadecimal value."""
    if not pattern.startswith("0x") and not pattern.startswith("0X"):
        return False
    return len(pattern) % 2 == 0 and all(c in string.hexdigits for c in pattern[2:])


def ida_synchronize_handler(event):
    gdb.execute("ida-interact sync", from_tty=True)
    return


def continue_handler(event):
    """GDB event handler for new object continue cases."""
    return


def hook_stop_handler(event):
    """GDB event handler for stop cases."""
    reset_all_caches()
    gdb.execute("context")
    return


def new_objfile_handler(event):
    """GDB event handler for new object file cases."""
    reset_all_caches()
    set_arch()
    load_libc_args()
    return


def exit_handler(event):
    """GDB event handler for exit cases."""
    global __gef_remote__, __gef_qemu_mode__

    reset_all_caches()
    __gef_qemu_mode__ = False
    if __gef_remote__ and gef.config["gef-remote.clean_on_exit"] == True:
        shutil.rmtree("/tmp/gef/{:d}".format(__gef_remote__))
        __gef_remote__ = None
    return


def memchanged_handler(event):
    """GDB event handler for mem changes cases."""
    reset_all_caches()


def regchanged_handler(event):
    """GDB event handler for reg changes cases."""
    reset_all_caches()


def load_libc_args():
    # load libc function arguments' definitions
    if not gef.config["context.libc_args"]:
        return

    path = gef.config["context.libc_args_path"]
    if path is None:
        warn("Config `context.libc_args_path` not set but `context.libc_args` is True. Make sure you have `gef-extras` installed")
        return

    path = os.path.realpath(os.path.expanduser(path))

    if not os.path.isdir(path):
        warn("Config `context.libc_args_path` set but it's not a directory")
        return

    _arch_mode = "{}_{}".format(gef.arch.arch.lower(), gef.arch.mode)
    _libc_args_file = "{}/{}.json".format(path, _arch_mode)

    global libc_args_definitions

    # current arch and mode already loaded
    if _arch_mode in libc_args_definitions:
        return

    libc_args_definitions[_arch_mode] = {}
    try:
        with open(_libc_args_file) as _libc_args:
            libc_args_definitions[_arch_mode] = json.load(_libc_args)
    except FileNotFoundError:
        del(libc_args_definitions[_arch_mode])
        warn("Config context.libc_args is set but definition cannot be loaded: file {} not found".format(_libc_args_file))
    except json.decoder.JSONDecodeError as e:
        del(libc_args_definitions[_arch_mode])
        warn("Config context.libc_args is set but definition cannot be loaded from file {}: {}".format(_libc_args_file, e))
    return


def get_terminal_size():
    """Return the current terminal size."""
    if is_debug():
        return 600, 100

    if platform.system() == "Windows":
        from ctypes import windll, create_string_buffer
        hStdErr = -12
        herr = windll.kernel32.GetStdHandle(hStdErr)
        csbi = create_string_buffer(22)
        res = windll.kernel32.GetConsoleScreenBufferInfo(herr, csbi)
        if res:
            _, _, _, _, _, left, top, right, bottom, _, _ = struct.unpack("hhhhHhhhhhh", csbi.raw)
            tty_columns = right - left + 1
            tty_rows = bottom - top + 1
            return tty_rows, tty_columns
        else:
            return 600, 100
    else:
        import fcntl
        import termios
        try:
            tty_rows, tty_columns = struct.unpack("hh", fcntl.ioctl(1, termios.TIOCGWINSZ, "1234"))
            return tty_rows, tty_columns
        except OSError:
            return 600, 100


def get_generic_arch(module, prefix, arch, mode, big_endian, to_string=False):
    """
    Retrieves architecture and mode from the arguments for use for the holy
    {cap,key}stone/unicorn trinity.
    """
    if to_string:
        arch = "{:s}.{:s}_ARCH_{:s}".format(module.__name__, prefix, arch)
        if mode:
            mode = "{:s}.{:s}_MODE_{:s}".format(module.__name__, prefix, str(mode))
        else:
            mode = ""
        if is_big_endian():
            mode += " + {:s}.{:s}_MODE_BIG_ENDIAN".format(module.__name__, prefix)
        else:
            mode += " + {:s}.{:s}_MODE_LITTLE_ENDIAN".format(module.__name__, prefix)

    else:
        arch = getattr(module, "{:s}_ARCH_{:s}".format(prefix, arch))
        if mode:
            mode = getattr(module, "{:s}_MODE_{:s}".format(prefix, mode))
        else:
            mode = 0
        if big_endian:
            mode |= getattr(module, "{:s}_MODE_BIG_ENDIAN".format(prefix))
        else:
            mode |= getattr(module, "{:s}_MODE_LITTLE_ENDIAN".format(prefix))

    return arch, mode


def get_generic_running_arch(module, prefix, to_string=False):
    """
    Retrieves architecture and mode from the current context.
    """

    if not is_alive():
        return None, None

    if gef.arch is not None:
        arch, mode = gef.arch.arch, gef.arch.mode
    else:
        raise OSError("Emulation not supported for your OS")

    return get_generic_arch(module, prefix, arch, mode, is_big_endian(), to_string)


def get_unicorn_arch(arch=None, mode=None, endian=None, to_string=False):
    unicorn = sys.modules["unicorn"]
    if (arch, mode, endian) == (None, None, None):
        return get_generic_running_arch(unicorn, "UC", to_string)
    return get_generic_arch(unicorn, "UC", arch, mode, endian, to_string)


def get_capstone_arch(arch=None, mode=None, endian=None, to_string=False):
    capstone = sys.modules["capstone"]

    # hacky patch to unify capstone/ppc syntax with keystone & unicorn:
    # CS_MODE_PPC32 does not exist (but UC_MODE_32 & KS_MODE_32 do)
    if is_arch(Elf.POWERPC64):
        raise OSError("Capstone not supported for PPC64 yet.")

    if is_alive() and is_arch(Elf.POWERPC):

        arch = "PPC"
        mode = "32"
        endian = is_big_endian()
        return get_generic_arch(capstone, "CS",
                                arch or gef.arch.arch,
                                mode or gef.arch.mode,
                                endian or is_big_endian(),
                                to_string)

    if (arch, mode, endian) == (None, None, None):
        return get_generic_running_arch(capstone, "CS", to_string)
    return get_generic_arch(capstone, "CS",
                            arch or gef.arch.arch,
                            mode or gef.arch.mode,
                            endian or is_big_endian(),
                            to_string)


def get_keystone_arch(arch=None, mode=None, endian=None, to_string=False):
    keystone = sys.modules["keystone"]
    if (arch, mode, endian) == (None, None, None):
        return get_generic_running_arch(keystone, "KS", to_string)

    if arch in ["ARM64", "SYSTEMZ"]:
        modes = [None]
    elif arch == "ARM" and mode == "ARMV8":
        modes = ["ARM", "V8"]
    elif arch == "ARM" and mode == "THUMBV8":
        modes = ["THUMB", "V8"]
    else:
        modes = [mode]
    a = arch
    if not to_string:
        mode = 0
        for m in modes:
            arch, _mode = get_generic_arch(keystone, "KS", a, m, endian, to_string)
            mode |= _mode
    else:
        mode = ""
        for m in modes:
            arch, _mode = get_generic_arch(keystone, "KS", a, m, endian, to_string)
            mode += "|{}".format(_mode)
        mode = mode[1:]
    return arch, mode


def get_unicorn_registers(to_string=False):
    "Return a dict matching the Unicorn identifier for a specific register."
    unicorn = sys.modules["unicorn"]
    regs = {}

    if gef.arch is not None:
        arch = gef.arch.arch.lower()
    else:
        raise OSError("Oops")

    const = getattr(unicorn, "{}_const".format(arch))
    for reg in gef.arch.all_registers:
        regname = "UC_{:s}_REG_{:s}".format(arch.upper(), reg[1:].upper())
        if to_string:
            regs[reg] = "{:s}.{:s}".format(const.__name__, regname)
        else:
            regs[reg] = getattr(const, regname)
    return regs


def keystone_assemble(code, arch, mode, *args, **kwargs):
    """Assembly encoding function based on keystone."""
    keystone = sys.modules["keystone"]
    code = gef_pybytes(code)
    addr = kwargs.get("addr", 0x1000)

    try:
        ks = keystone.Ks(arch, mode)
        enc, cnt = ks.asm(code, addr)
    except keystone.KsError as e:
        err("Keystone assembler error: {:s}".format(str(e)))
        return None

    if cnt == 0:
        return ""

    enc = bytearray(enc)
    if "raw" not in kwargs:
        s = binascii.hexlify(enc)
        enc = b"\\x" + b"\\x".join([s[i : i + 2] for i in range(0, len(s), 2)])
        enc = enc.decode("utf-8")

    return enc


@lru_cache()
def get_elf_headers(filename=None):
    """Return an Elf object with info from `filename`. If not provided, will return
    the currently debugged file."""
    if filename is None:
        filename = get_filepath()

    if filename.startswith("target:"):
        warn("Your file is remote, you should try using `gef-remote` instead")
        return

    return Elf(filename)


# def _ptr_width():
#     void = cached_lookup_type("void")
#     if void is None:
#         uintptr_t = cached_lookup_type("uintptr_t")
#         return uintptr_t.sizeof
#     else:
#         return void.pointer().sizeof


@lru_cache()
def is_64bit():
    """Checks if current target is 64bit."""
    return gef.arch.ptrsize == 8


@lru_cache()
def is_32bit():
    """Checks if current target is 32bit."""
    return gef.arch.ptrsize == 4


@lru_cache()
def is_x86_64():
    """Checks if current target is x86-64"""
    return get_arch() == "i386:x86-64"


@lru_cache()
def is_x86_32():
    """Checks if current target is an x86-32"""
    return get_arch() == "i386"


@lru_cache()
def is_x86():
    return is_x86_32() or is_x86_64()


@lru_cache()
def is_arch(arch):
    elf = gef.binary or get_elf_headers()
    return elf.e_machine == arch


def set_arch(arch=None, default=None):
    """Sets the current architecture.
    If an arch is explicitly specified, use that one, otherwise try to parse it
    out of the current target. If that fails, and default is specified, select and
    set that arch.
    Return the selected arch, or raise an OSError.
    """
    global gef
    arches = {
        "ARM": ARM, Elf.ARM: ARM,
        "AARCH64": AARCH64, "ARM64": AARCH64, Elf.AARCH64: AARCH64,
        "X86": X86, Elf.X86_32: X86,
        "X86_64": X86_64, Elf.X86_64: X86_64, "i386:x86-64": X86_64,
        "PowerPC": PowerPC, "PPC": PowerPC, Elf.POWERPC: PowerPC,
        "PowerPC64": PowerPC64, "PPC64": PowerPC64, Elf.POWERPC64: PowerPC64,
        "RISCV": RISCV, Elf.RISCV: RISCV,
        "SPARC": SPARC, Elf.SPARC: SPARC,
        "SPARC64": SPARC64, Elf.SPARC64: SPARC64,
        "MIPS": MIPS, Elf.MIPS: MIPS,
    }

    if arch:
        try:
            gef.arch = arches[arch.upper()]()
            return gef.arch
        except KeyError:
            raise OSError("Specified arch {:s} is not supported".format(arch.upper()))

    if not gef.binary:
        elf = get_elf_headers()
        gef.binary = elf if elf.is_valid() else None

    arch_name = gef.binary.e_machine if gef.binary else get_arch()
    try:
        gef.arch = arches[arch_name]()
    except KeyError:
        if default:
            try:
                gef.arch = arches[default.upper()]()
            except KeyError:
                raise OSError("CPU not supported, neither is default {:s}".format(default.upper()))
        else:
            raise OSError("CPU type is currently not supported: {:s}".format(get_arch()))
    return gef.arch


@lru_cache()
def cached_lookup_type(_type):
    try:
        return gdb.lookup_type(_type).strip_typedefs()
    except RuntimeError:
        return None


@deprecated("Use `gef.arch.ptrsize` instead")
def get_memory_alignment(in_bits=False):
    """Try to determine the size of a pointer on this system.
    First, try to parse it out of the ELF header.
    Next, use the size of `size_t`.
    Finally, try the size of $pc.
    If `in_bits` is set to True, the result is returned in bits, otherwise in
    bytes."""
    res = cached_lookup_type("size_t")
    if res is not None:
        return res.sizeof if not in_bits else res.sizeof * 8

    try:
        return gdb.parse_and_eval("$pc").type.sizeof
    except:
        pass

    raise EnvironmentError("GEF is running under an unsupported mode")


def clear_screen(tty=""):
    """Clear the screen."""
    global __gef_redirect_output_fd__
    if not tty:
        gdb.execute("shell clear -x")
        return

    # Since the tty can be closed at any time, a PermissionError exception can
    # occur when `clear_screen` is called. We handle this scenario properly
    try:
        with open(tty, "wt") as f:
            f.write("\x1b[H\x1b[J")
    except PermissionError:
        __gef_redirect_output_fd__ = None
        gef.config["context.redirect"] = ""
    return


def format_address(addr):
    """Format the address according to its size."""
    memalign_size = get_memory_alignment()
    addr = align_address(addr)

    if memalign_size == 4:
        return "0x{:08x}".format(addr)

    return "0x{:016x}".format(addr)


def format_address_spaces(addr, left=True):
    """Format the address according to its size, but with spaces instead of zeroes."""
    width = get_memory_alignment() * 2 + 2
    addr = align_address(addr)

    if not left:
        return "0x{:x}".format(addr).rjust(width)

    return "0x{:x}".format(addr).ljust(width)


def align_address(address):
    """Align the provided address to the process's native length."""
    if get_memory_alignment() == 4:
        return address & 0xFFFFFFFF

    return address & 0xFFFFFFFFFFFFFFFF


def align_address_to_size(address, align):
    """Align the address to the given size."""
    return address + ((align - (address % align)) % align)


def align_address_to_page(address):
    """Align the address to a page."""
    a = align_address(address) >> DEFAULT_PAGE_ALIGN_SHIFT
    return a << DEFAULT_PAGE_ALIGN_SHIFT

def malloc_align_address(address):
    """Align addresses according to glibc's MALLOC_ALIGNMENT. See also Issue #689 on Github"""
    __default_malloc_alignment = 0x10
    if is_x86_32() and get_libc_version() >= (2, 26):
        # Special case introduced in Glibc 2.26:
        # https://elixir.bootlin.com/glibc/glibc-2.26/source/sysdeps/i386/malloc-alignment.h#L22
        malloc_alignment = __default_malloc_alignment
    else:
        # Generic case:
        # https://elixir.bootlin.com/glibc/glibc-2.26/source/sysdeps/generic/malloc-alignment.h#L22
        __alignof__long_double = int(safe_parse_and_eval("_Alignof(long double)") or __default_malloc_alignment) # fallback to default if the expression fails to evaluate
        malloc_alignment = max(__alignof__long_double, 2 * gef.arch.ptrsize)

    ceil = lambda n: int(-1 * n // 1 * -1)
    # align address to nearest next multiple of malloc_alignment
    return malloc_alignment * ceil((address / malloc_alignment))


def parse_address(address):
    """Parse an address and return it as an Integer."""
    if is_hex(address):
        return int(address, 16)
    return to_unsigned_long(gdb.parse_and_eval(address))


def is_in_x86_kernel(address):
    address = align_address(address)
    memalign = gef.arch.ptrsize*8 - 1
    return (address >> memalign) == 0xF

@deprecated("Use `str(gef.arch.endianness)` instead")
def endian_str():
    return str(gef.arch.endianness)


@lru_cache()
def is_remote_debug():
    """"Return True is the current debugging session is running through GDB remote session."""
    return __gef_remote__ is not None or "remote" in gdb.execute("maintenance print target-stack", to_string=True)


def de_bruijn(alphabet, n):
    """De Bruijn sequence for alphabet and subsequences of length n (for compat. w/ pwnlib)."""
    k = len(alphabet)
    a = [0] * k * n

    def db(t, p):
        if t > n:
            if n % p == 0:
                for j in range(1, p + 1):
                    yield alphabet[a[j]]
        else:
            a[t] = a[t - p]
            for c in db(t + 1, p):
                yield c

            for j in range(a[t - p] + 1, k):
                a[t] = j
                for c in db(t + 1, t):
                    yield c

    return db(1, 1)


def generate_cyclic_pattern(length, cycle=4):
    """Create a `length` byte bytearray of a de Bruijn cyclic pattern."""
    charset = bytearray(b"abcdefghijklmnopqrstuvwxyz")
    return bytearray(itertools.islice(de_bruijn(charset, cycle), length))


def safe_parse_and_eval(value):
    """GEF wrapper for gdb.parse_and_eval(): this function returns None instead of raising
    gdb.error if the eval failed."""
    try:
        return gdb.parse_and_eval(value)
    except gdb.error:
        pass
    return None


@lru_cache()
def dereference(addr):
    """GEF wrapper for gdb dereference function."""
    try:
        ulong_t = cached_lookup_type(use_stdtype()) or \
                  cached_lookup_type(use_default_type()) or \
                  cached_lookup_type(use_golang_type()) or \
                  cached_lookup_type(use_rust_type())
        unsigned_long_type = ulong_t.pointer()
        res = gdb.Value(addr).cast(unsigned_long_type).dereference()
        # GDB does lazy fetch by default so we need to force access to the value
        res.fetch_lazy()
        return res
    except gdb.MemoryError:
        pass
    return None


def gef_convenience(value):
    """Defines a new convenience value."""
    global __gef_convenience_vars_index__
    var_name = "$_gef{:d}".format(__gef_convenience_vars_index__)
    __gef_convenience_vars_index__ += 1
    gdb.execute("""set {:s} = "{:s}" """.format(var_name, value))
    return var_name


def parse_string_range(s):
    """Parses an address range (e.g. 0x400000-0x401000)"""
    addrs = s.split("-")
    return map(lambda x: int(x, 16), addrs)


@lru_cache()
def gef_get_auxiliary_values():
    """Retrieve the ELF auxiliary values of the current execution. This
    information is provided by the operating system to transfer some kernel
    level information to the user process. Return None if not found, or a
    dict() of values as: {aux_vect_name: int(aux_vect_value)}."""
    if not is_alive():
        return None

    __auxiliary_vector = {}
    auxv_info = gdb.execute("info auxv", to_string=True)
    if "failed" in auxv_info:
        err(auxv_info)  # print GDB error
        return None
    for line in auxv_info.splitlines():
        line = line.split('"')[0].strip()  # remove the ending string (if any)
        line = line.split()  # split the string by whitespace(s)
        if len(line) < 4:
            continue  # a valid entry should have at least 4 columns
        __av_type = line[1]
        __av_value = line[-1]
        __auxiliary_vector[__av_type] = int(__av_value, base=0)
    return __auxiliary_vector


def gef_read_canary():
    """Read the canary of a running process using Auxiliary Vector. Return a tuple of (canary, location)
    if found, None otherwise."""
    auxval = gef_get_auxiliary_values()
    if not auxval:
        return None

    canary_location = auxval["AT_RANDOM"]
    canary = gef.memory.read_integer(canary_location)
    canary &= ~0xFF
    return canary, canary_location


def gef_get_pie_breakpoint(num):
    global __pie_breakpoints__
    return __pie_breakpoints__[num]


@lru_cache()
def gef_getpagesize():
    """Get the page size from auxiliary values."""
    auxval = gef_get_auxiliary_values()
    if not auxval:
        return DEFAULT_PAGE_SIZE
    return auxval["AT_PAGESZ"]


def only_if_events_supported(event_type):
    """Checks if GDB supports events without crashing."""

    def wrap(f):
        def wrapped_f(*args, **kwargs):
            if getattr(gdb, "events") and getattr(gdb.events, event_type):
                return f(*args, **kwargs)
            warn("GDB events cannot be set")

        return wrapped_f

    return wrap


#
# Event hooking
#


@only_if_events_supported("cont")
def gef_on_continue_hook(func):
    return gdb.events.cont.connect(func)


@only_if_events_supported("cont")
def gef_on_continue_unhook(func):
    return gdb.events.cont.disconnect(func)


@only_if_events_supported("stop")
def gef_on_stop_hook(func):
    return gdb.events.stop.connect(func)


@only_if_events_supported("stop")
def gef_on_stop_unhook(func):
    return gdb.events.stop.disconnect(func)


@only_if_events_supported("exited")
def gef_on_exit_hook(func):
    return gdb.events.exited.connect(func)


@only_if_events_supported("exited")
def gef_on_exit_unhook(func):
    return gdb.events.exited.disconnect(func)


@only_if_events_supported("new_objfile")
def gef_on_new_hook(func):
    return gdb.events.new_objfile.connect(func)


@only_if_events_supported("new_objfile")
def gef_on_new_unhook(func):
    return gdb.events.new_objfile.disconnect(func)


@only_if_events_supported("memory_changed")
def gef_on_memchanged_hook(func):
    return gdb.events.memory_changed.connect(func)


@only_if_events_supported("memory_changed")
def gef_on_memchanged_unhook(func):
    return gdb.events.memory_changed.disconnect(func)


@only_if_events_supported("register_changed")
def gef_on_regchanged_hook(func):
    return gdb.events.register_changed.connect(func)


@only_if_events_supported("register_changed")
def gef_on_regchanged_unhook(func):
    return gdb.events.register_changed.disconnect(func)


#
# Virtual breakpoints
#


class PieVirtualBreakpoint:
    """PIE virtual breakpoint (not real breakpoint)."""

    def __init__(self, set_func, vbp_num, addr):
        # set_func(base): given a base address return a
        # "set breakpoint" gdb command string
        self.set_func = set_func
        self.vbp_num = vbp_num
        # breakpoint num, 0 represents not instantiated yet
        self.bp_num = 0
        self.bp_addr = 0
        # this address might be a symbol, just to know where to break
        if isinstance(addr, int):
            self.addr = hex(addr)
        else:
            self.addr = addr
        return

    def instantiate(self, base):
        if self.bp_num:
            self.destroy()

        try:
            res = gdb.execute(self.set_func(base), to_string=True)
        except gdb.error as e:
            err(e)
            return

        if "Breakpoint" not in res:
            err(res)
            return
        res_list = res.split()
        self.bp_num = res_list[1]
        self.bp_addr = res_list[3]
        return

    def destroy(self):
        if not self.bp_num:
            err("Destroy PIE breakpoint not even set")
            return
        gdb.execute("delete {}".format(self.bp_num))
        self.bp_num = 0
        return


#
# Breakpoints
#

class FormatStringBreakpoint(gdb.Breakpoint):
    """Inspect stack for format string."""
    def __init__(self, spec, num_args):
        super().__init__(spec, type=gdb.BP_BREAKPOINT, internal=False)
        self.num_args = num_args
        self.enabled = True
        return

    def stop(self):
        reset_all_caches()
        msg = []
        ptr, addr = gef.arch.get_ith_parameter(self.num_args)
        addr = lookup_address(addr)

        if not addr.valid:
            return False

        if addr.section.permission.value & Permission.WRITE:
            content = gef.memory.read_cstring(addr.value)
            name = addr.info.name if addr.info else addr.section.path
            msg.append(Color.colorify("Format string helper", "yellow bold"))
            msg.append("Possible insecure format string: {:s}('{:s}' {:s} {:#x}: '{:s}')".format(self.location, ptr, RIGHT_ARROW, addr.value, content))
            msg.append("Reason: Call to '{:s}()' with format string argument in position "
                       "#{:d} is in page {:#x} ({:s}) that has write permission".format(self.location, self.num_args, addr.section.page_start, name))
            push_context_message("warn", "\n".join(msg))
            return True

        return False


class StubBreakpoint(gdb.Breakpoint):
    """Create a breakpoint to permanently disable a call (fork/alarm/signal/etc.)."""

    def __init__(self, func, retval):
        super().__init__(func, gdb.BP_BREAKPOINT, internal=False)
        self.func = func
        self.retval = retval

        m = "All calls to '{:s}' will be skipped".format(self.func)
        if self.retval is not None:
            m += " (with return value set to {:#x})".format(self.retval)
        info(m)
        return

    def stop(self):
        m = "Ignoring call to '{:s}' ".format(self.func)
        m += "(setting return value to {:#x})".format(self.retval)
        gdb.execute("return (unsigned int){:#x}".format(self.retval))
        ok(m)
        return False


class ChangePermissionBreakpoint(gdb.Breakpoint):
    """When hit, this temporary breakpoint will restore the original code, and position
    $pc correctly."""

    def __init__(self, loc, code, pc):
        super().__init__(loc, gdb.BP_BREAKPOINT, internal=False)
        self.original_code = code
        self.original_pc = pc
        return

    def stop(self):
        info("Restoring original context")
        gef.memory.write(self.original_pc, self.original_code, len(self.original_code))
        info("Restoring $pc")
        gdb.execute("set $pc = {:#x}".format(self.original_pc))
        return True


class TraceMallocBreakpoint(gdb.Breakpoint):
    """Track allocations done with malloc() or calloc()."""

    def __init__(self, name):
        super().__init__(name, gdb.BP_BREAKPOINT, internal=True)
        self.silent = True
        self.name = name
        return

    def stop(self):
        reset_all_caches()
        _, size = gef.arch.get_ith_parameter(0)
        self.retbp = TraceMallocRetBreakpoint(size, self.name)
        return False


class TraceMallocRetBreakpoint(gdb.FinishBreakpoint):
    """Internal temporary breakpoint to retrieve the return value of malloc()."""

    def __init__(self, size, name):
        super().__init__(gdb.newest_frame(), internal=True)
        self.size = size
        self.name = name
        self.silent = True
        return

    def stop(self):
        global __heap_uaf_watchpoints__, __heap_freed_list__, __heap_allocated_list__

        if self.return_value:
            loc = int(self.return_value)
        else:
            loc = parse_address(gef.arch.return_register)

        size = self.size
        ok("{} - {}({})={:#x}".format(Color.colorify("Heap-Analysis", "yellow bold"), self.name, size, loc))
        check_heap_overlap = gef.config["heap-analysis-helper.check_heap_overlap"]

        # pop from free-ed list if it was in it
        if __heap_freed_list__:
            idx = 0
            for item in __heap_freed_list__:
                addr = item[0]
                if addr == loc:
                    __heap_freed_list__.remove(item)
                    continue
                idx += 1

        # pop from uaf watchlist
        if __heap_uaf_watchpoints__:
            idx = 0
            for wp in __heap_uaf_watchpoints__:
                wp_addr = wp.address
                if loc <= wp_addr < loc + size:
                    __heap_uaf_watchpoints__.remove(wp)
                    wp.enabled = False
                    continue
                idx += 1

        item = (loc, size)

        if check_heap_overlap:
            # seek all the currently allocated chunks, read their effective size and check for overlap
            msg = []
            align = get_memory_alignment()
            for chunk_addr, _ in __heap_allocated_list__:
                current_chunk = GlibcChunk(chunk_addr)
                current_chunk_size = current_chunk.get_chunk_size()

                if chunk_addr <= loc < chunk_addr + current_chunk_size:
                    offset = loc - chunk_addr - 2*align
                    if offset < 0: continue # false positive, discard

                    msg.append(Color.colorify("Heap-Analysis", "yellow bold"))
                    msg.append("Possible heap overlap detected")
                    msg.append("Reason {} new allocated chunk {:#x} (of size {:d}) overlaps in-used chunk {:#x} (of size {:#x})".format(RIGHT_ARROW, loc, size, chunk_addr, current_chunk_size))
                    msg.append("Writing {0:d} bytes from {1:#x} will reach chunk {2:#x}".format(offset, chunk_addr, loc))
                    msg.append("Payload example for chunk {1:#x} (to overwrite {0:#x} headers):".format(loc, chunk_addr))
                    msg.append("  data = 'A'*{0:d} + 'B'*{1:d} + 'C'*{1:d}".format(offset, align))
                    push_context_message("warn", "\n".join(msg))
                    return True

        # add it to alloc-ed list
        __heap_allocated_list__.append(item)
        return False


class TraceReallocBreakpoint(gdb.Breakpoint):
    """Track re-allocations done with realloc()."""

    def __init__(self):
        super().__init__("__libc_realloc", gdb.BP_BREAKPOINT, internal=True)
        self.silent = True
        return

    def stop(self):
        _, ptr = gef.arch.get_ith_parameter(0)
        _, size = gef.arch.get_ith_parameter(1)
        self.retbp = TraceReallocRetBreakpoint(ptr, size)
        return False


class TraceReallocRetBreakpoint(gdb.FinishBreakpoint):
    """Internal temporary breakpoint to retrieve the return value of realloc()."""

    def __init__(self, ptr, size):
        super().__init__(gdb.newest_frame(), internal=True)
        self.ptr = ptr
        self.size = size
        self.silent = True
        return

    def stop(self):
        global __heap_uaf_watchpoints__, __heap_freed_list__, __heap_allocated_list__

        if self.return_value:
            newloc = int(self.return_value)
        else:
            newloc = parse_address(gef.arch.return_register)

        if newloc != self:
            ok("{} - realloc({:#x}, {})={}".format(Color.colorify("Heap-Analysis", "yellow bold"),
                                                   self.ptr, self.size,
                                                   Color.colorify("{:#x}".format(newloc), "green"),))
        else:
            ok("{} - realloc({:#x}, {})={}".format(Color.colorify("Heap-Analysis", "yellow bold"),
                                                   self.ptr, self.size,
                                                   Color.colorify("{:#x}".format(newloc), "red"),))

        item = (newloc, self.size)

        try:
            # check if item was in alloc-ed list
            idx = [x for x, y in __heap_allocated_list__].index(self.ptr)
            # if so pop it out
            item = __heap_allocated_list__.pop(idx)
        except ValueError:
            if is_debug():
                warn("Chunk {:#x} was not in tracking list".format(self.ptr))
        finally:
            # add new item to alloc-ed list
            __heap_allocated_list__.append(item)

        return False


class TraceFreeBreakpoint(gdb.Breakpoint):
    """Track calls to free() and attempts to detect inconsistencies."""

    def __init__(self):
        super().__init__("__libc_free", gdb.BP_BREAKPOINT, internal=True)
        self.silent = True
        return

    def stop(self):
        reset_all_caches()
        _, addr = gef.arch.get_ith_parameter(0)
        msg = []
        check_free_null = gef.config["heap-analysis-helper.check_free_null"]
        check_double_free = gef.config["heap-analysis-helper.check_double_free"]
        check_weird_free = gef.config["heap-analysis-helper.check_weird_free"]
        check_uaf = gef.config["heap-analysis-helper.check_uaf"]

        ok("{} - free({:#x})".format(Color.colorify("Heap-Analysis", "yellow bold"), addr))
        if addr == 0:
            if check_free_null:
                msg.append(Color.colorify("Heap-Analysis", "yellow bold"))
                msg.append("Attempting to free(NULL) at {:#x}".format(gef.arch.pc))
                msg.append("Reason: if NULL page is allocatable, this can lead to code execution.")
                push_context_message("warn", "\n".join(msg))
                return True
            return False

        if addr in [x for (x, y) in __heap_freed_list__]:
            if check_double_free:
                msg.append(Color.colorify("Heap-Analysis", "yellow bold"))
                msg.append("Double-free detected {} free({:#x}) is called at {:#x} but is already in the free-ed list".format(RIGHT_ARROW, addr, gef.arch.pc))
                msg.append("Execution will likely crash...")
                push_context_message("warn", "\n".join(msg))
                return True
            return False

        # if here, no error
        # 1. move alloc-ed item to free list
        try:
            # pop from alloc-ed list
            idx = [x for x, y in __heap_allocated_list__].index(addr)
            item = __heap_allocated_list__.pop(idx)

        except ValueError:
            if check_weird_free:
                msg.append(Color.colorify("Heap-Analysis", "yellow bold"))
                msg.append("Heap inconsistency detected:")
                msg.append("Attempting to free an unknown value: {:#x}".format(addr))
                push_context_message("warn", "\n".join(msg))
                return True
            return False

        # 2. add it to free-ed list
        __heap_freed_list__.append(item)

        self.retbp = None
        if check_uaf:
            # 3. (opt.) add a watchpoint on pointer
            self.retbp = TraceFreeRetBreakpoint(addr)
        return False


class TraceFreeRetBreakpoint(gdb.FinishBreakpoint):
    """Internal temporary breakpoint to track free()d values."""

    def __init__(self, addr):
        super().__init__(gdb.newest_frame(), internal=True)
        self.silent = True
        self.addr = addr
        return

    def stop(self):
        reset_all_caches()
        wp = UafWatchpoint(self.addr)
        __heap_uaf_watchpoints__.append(wp)
        return False


class UafWatchpoint(gdb.Breakpoint):
    """Custom watchpoints set TraceFreeBreakpoint() to monitor free()d pointers being used."""

    def __init__(self, addr):
        super().__init__("*{:#x}".format(addr), gdb.BP_WATCHPOINT, internal=True)
        self.address = addr
        self.silent = True
        self.enabled = True
        return

    def stop(self):
        """If this method is triggered, we likely have a UaF. Break the execution and report it."""
        reset_all_caches()
        frame = gdb.selected_frame()
        if frame.name() in ("_int_malloc", "malloc_consolidate", "__libc_calloc", ):
            return False

        # software watchpoints stop after the next statement (see
        # https://sourceware.org/gdb/onlinedocs/gdb/Set-Watchpoints.html)
        pc = gdb_get_nth_previous_instruction_address(gef.arch.pc, 2)
        insn = gef_current_instruction(pc)
        msg = []
        msg.append(Color.colorify("Heap-Analysis", "yellow bold"))
        msg.append("Possible Use-after-Free in '{:s}': pointer {:#x} was freed, but is attempted to be used at {:#x}"
                   .format(get_filepath(), self.address, pc))
        msg.append("{:#x}   {:s} {:s}".format(insn.address, insn.mnemonic, Color.yellowify(", ".join(insn.operands))))
        push_context_message("warn", "\n".join(msg))
        return True


class EntryBreakBreakpoint(gdb.Breakpoint):
    """Breakpoint used internally to stop execution at the most convenient entry point."""

    def __init__(self, location):
        super().__init__(location, gdb.BP_BREAKPOINT, internal=True, temporary=True)
        self.silent = True
        return

    def stop(self):
        reset_all_caches()
        return True


class NamedBreakpoint(gdb.Breakpoint):
    """Breakpoint which shows a specified name, when hit."""

    def __init__(self, location, name):
        super().__init__(spec=location, type=gdb.BP_BREAKPOINT, internal=False, temporary=False)
        self.name = name
        self.loc = location
        return

    def stop(self):
        reset_all_caches()
        push_context_message("info", "Hit breakpoint {} ({})".format(self.loc, Color.colorify(self.name, "red bold")))
        return True


#
# Context Panes
#

def register_external_context_pane(pane_name, display_pane_function, pane_title_function):
    """
    Registering function for new GEF Context View.
    pane_name: a string that has no spaces (used in settings)
    display_pane_function: a function that uses gef_print() to print strings
    pane_title_function: a function that returns a string or None, which will be displayed as the title.
    If None, no title line is displayed.

    Example Usage:
    def display_pane(): gef_print("Wow, I am a context pane!")
    def pane_title(): return "example:pane"
    register_external_context_pane("example_pane", display_pane, pane_title)
    """
    gef.instance.add_context_pane(pane_name, display_pane_function, pane_title_function)
    return


#
# Commands
#

def register_external_command(obj):
    """Registering function for new GEF (sub-)command to GDB."""
    global __commands__, gef
    cls = obj.__class__
    __commands__.append(cls)
    gef.instance.load(initial=False)
    gef.instance.doc.add_command_to_doc((cls._cmdline_, cls, None))
    gef.instance.doc.refresh()
    return cls


def register_command(cls):
    """Decorator for registering new GEF (sub-)command to GDB."""
    global __commands__
    __commands__.append(cls)
    return cls


def register_priority_command(cls):
    """Decorator for registering new command with priority, meaning that it must
    loaded before the other generic commands."""
    global __commands__
    __commands__.insert(0, cls)
    return cls


def register_function(cls):
    """Decorator for registering a new convenience function to GDB."""
    global __functions__
    __functions__.append(cls)
    return cls


class GenericCommand(gdb.Command, metaclass=abc.ABCMeta):
    """This is an abstract class for invoking commands, should not be instantiated."""

    def __init__(self, *args, **kwargs):
        self.pre_load()
        syntax = Color.yellowify("\nSyntax: ") + self._syntax_
        example = Color.yellowify("\nExample: ") + self._example_ if self._example_ else ""
        self.__doc__ = self.__doc__.replace(" "*4, "") + syntax + example
        self.repeat = False
        self.repeat_count = 0
        self.__last_command = None
        command_type = kwargs.setdefault("command", gdb.COMMAND_OBSCURE)
        complete_type = kwargs.setdefault("complete", gdb.COMPLETE_NONE)
        prefix = kwargs.setdefault("prefix", False)
        super().__init__(self._cmdline_, command_type, complete_type, prefix)
        self.post_load()
        return

    def invoke(self, args, from_tty):
        try:
            argv = gdb.string_to_argv(args)
            self.__set_repeat_count(argv, from_tty)
            bufferize(self.do_invoke)(argv)
        except Exception as e:
            # Note: since we are intercepting cleaning exceptions here, commands preferably should avoid
            # catching generic Exception, but rather specific ones. This is allows a much cleaner use.
            if is_debug():
                show_last_exception()
            else:
                err("Command '{:s}' failed to execute properly, reason: {:s}".format(self._cmdline_, str(e)))
        return

    def usage(self):
        err("Syntax\n{}".format(self._syntax_))
        return

    @abc.abstractproperty
    def _cmdline_(self): pass

    @abc.abstractproperty
    def _syntax_(self): pass

    @abc.abstractproperty
    def _example_(self): return ""

    @abc.abstractmethod
    def do_invoke(self, argv): pass

    def pre_load(self): pass

    def post_load(self): pass

    def __get_setting_name(self, name):
        def __sanitize_class_name(clsname):
            if " " not in clsname:
                return clsname
            return "-".join(clsname.split())
        class_name = __sanitize_class_name(self.__class__._cmdline_)
        return "{:s}.{:s}".format(class_name, name)

    def __iter__(self):
        for key in gef.config.keys():
            if key.startswith(self._cmdline_):
                yield key.replace("{:s}.".format(self._cmdline_), "", 1)

    @property
    def settings(self):
        """Return the list of settings for this command."""
        return list(iter(self))

    @deprecated("")
    def get_setting(self, name):
        return self.__getitem__(name)

    def __getitem__(self, name):
        key = self.__get_setting_name(name)
        return gef.config[key]

    @deprecated("")
    def has_setting(self, name):
        return self.__contains__(name)

    def __contains__(self, name):
        return self.__get_setting_name(name) in gef.config

    @deprecated("")
    def add_setting(self, name, value, description=""):
        return self.__setitem__(name, (value, type(value), description))

    def __setitem__(self, name, value):
        # make sure settings are always associated to the root command (which derives from GenericCommand)
        if "GenericCommand" not in [x.__name__ for x in self.__class__.__bases__]:
            return
        key = self.__get_setting_name(name)
        if key in gef.config:
            gef.config[key].value = value
        else:
            if len(value) == 1:
                gef.config[key] = GefSetting(value[0])
            elif len(value) == 2:
                gef.config[key] = GefSetting(value[0], description=value[1])
        return

    @deprecated("")
    def del_setting(self, name):
        return self.__delitem__(name)

    def __delitem__(self, name):
        del gef.config[self.__get_setting_name(name)]
        return

    def __set_repeat_count(self, argv, from_tty):
        if not from_tty:
            self.repeat = False
            self.repeat_count = 0
            return

        command = gdb.execute("show commands", to_string=True).strip().split("\n")[-1]
        self.repeat = self.__last_command == command
        self.repeat_count = self.repeat_count + 1 if self.repeat else 0
        self.__last_command = command
        return


@register_command
class VersionCommand(GenericCommand):
    """Display GEF version info."""

    _cmdline_ = "version"
    _syntax_ = "{:s}".format(_cmdline_)
    _example_ = "{:s}".format(_cmdline_)

    def do_invoke(self, argv):
        gef_fpath = os.path.abspath(os.path.expanduser(inspect.stack()[0][1]))
        gef_dir = os.path.dirname(gef_fpath)
        with open(gef_fpath, "rb") as f:
            gef_hash = hashlib.sha256(f.read()).hexdigest()

        if os.access("{}/.git".format(gef_dir), os.X_OK):
            ver = subprocess.check_output("git log --format='%H' -n 1 HEAD", cwd=gef_dir, shell=True).decode("utf8").strip()
            extra = "dirty" if len(subprocess.check_output("git ls-files -m", cwd=gef_dir, shell=True).decode("utf8").strip()) else "clean"
            gef_print("GEF: rev:{} (Git - {})".format(ver, extra))
        else:
            gef_blob_hash = subprocess.check_output("git hash-object {}".format(gef_fpath), shell=True).decode().strip()
            gef_print("GEF: (Standalone)")
            gef_print("Blob Hash({}): {}".format(gef_fpath, gef_blob_hash))
        gef_print("SHA256({}): {}".format(gef_fpath, gef_hash))
        gef_print("GDB: {}".format(gdb.VERSION, ))
        py_ver = "{:d}.{:d}".format(sys.version_info.major, sys.version_info.minor)
        gef_print("GDB-Python: {}".format(py_ver, ))

        if "full" in argv:
            gef_print("Loaded commands: {}".format(", ".join(gef.instance.loaded_command_names)))
        return


@register_command
class PrintFormatCommand(GenericCommand):
    """Print bytes format in high level languages."""

    valid_formats = ("py", "c", "js", "asm")
    valid_bitness = (8, 16, 32, 64)

    _cmdline_ = "print-format"
    _aliases_ = ["pf",]
    _syntax_  = """{} [--lang LANG] [--bitlen SIZE] [(--length,-l) LENGTH] [--clip] LOCATION
\t--lang LANG specifies the output format for programming language (available: {}, default 'py').
\t--bitlen SIZE specifies size of bit (possible values: {}, default is 8).
\t--length LENGTH specifies length of array (default is 256).
\t--clip The output data will be copied to clipboard
\tLOCATION specifies where the address of bytes is stored.""".format(_cmdline_, str(valid_formats), str(valid_bitness))
    _example_ = "{} --lang py -l 16 $rsp".format(_cmdline_)


    def __init__(self):
        super().__init__(complete=gdb.COMPLETE_LOCATION)
        return

    @property
    def format_matrix(self):
        # `endian_str()` is a runtime property, should not be defined as a class property
        return {
            8:  (endian_str() + "B", "char", "db"),
            16: (endian_str() + "H", "short", "dw"),
            32: (endian_str() + "I", "int", "dd"),
            64: (endian_str() + "Q", "long long", "dq"),
        }

    @only_if_gdb_running
    @parse_arguments({"location": "$pc", }, {("--length", "-l"): 256, "--bitlen": 0, "--lang": "py", "--clip": True,})
    def do_invoke(self, argv, *args, **kwargs):
        """Default value for print-format command."""
        args = kwargs["arguments"]
        args.bitlen = args.bitlen or gef.arch.ptrsize * 2

        valid_bitlens = self.format_matrix.keys()
        if args.bitlen not in valid_bitlens:
            err("Size of bit must be in: {}".format(str(valid_bitlens)))
            return

        if args.lang not in self.valid_formats:
            err("Language must be in: {}".format(str(self.valid_formats)))
            return

        start_addr = parse_address(args.location)
        size = int(args.bitlen / 8)
        end_addr = start_addr + args.length * size
        fmt = self.format_matrix[args.bitlen][0]
        data = []

        for addr in range(start_addr, end_addr, size):
            value = struct.unpack(fmt, gef.memory.read(addr, size))[0]
            data += [value]
        sdata = ", ".join(map(hex, data))

        if args.lang == "py":
            out = "buf = [{}]".format(sdata)
        elif args.lang == "c":
            c_type = self.format_matrix[args.bitlen][1]
            out = "unsigned {0} buf[{1}] = {{{2}}};".format(c_type, args.length, sdata)
        elif args.lang == "js":
            out = "var buf = [{}]".format(sdata)
        elif args.lang == "asm":
            asm_type = self.format_matrix[args.bitlen][2]
            out = "buf {0} {1}".format(asm_type, sdata)

        if args.clip:
            if copy_to_clipboard(gef_pybytes(out)):
                info("Copied to clipboard")
            else:
                warn("There's a problem while copying")

        gef_print(out)
        return


@register_command
class PieCommand(GenericCommand):
    """PIE breakpoint support."""

    _cmdline_ = "pie"
    _syntax_ = "{:s} (breakpoint|info|delete|run|attach|remote)".format(_cmdline_)

    def __init__(self):
        super().__init__(prefix=True)
        return

    def do_invoke(self, argv):
        if not argv:
            self.usage()
        return


@register_command
class PieBreakpointCommand(GenericCommand):
    """Set a PIE breakpoint at an offset from the target binaries base address."""

    _cmdline_ = "pie breakpoint"
    _syntax_ = "{:s} OFFSET".format(_cmdline_)

    @parse_arguments({"offset": ""}, {})
    def do_invoke(self, argv, *args, **kwargs):
        global __pie_counter__, __pie_breakpoints__
        args = kwargs["arguments"]
        if not args.offset:
            self.usage()
            return

        addr = parse_address(args.offset)
        self.set_pie_breakpoint(lambda base: "b *{}".format(base + addr), addr)

        # When the process is already on, set real breakpoints immediately
        if is_alive():
            vmmap = get_process_maps()
            base_address = [x.page_start for x in vmmap if x.path == get_filepath()][0]
            for bp_ins in __pie_breakpoints__.values():
                bp_ins.instantiate(base_address)
        return

    @staticmethod
    def set_pie_breakpoint(set_func, addr):
        global __pie_counter__, __pie_breakpoints__
        __pie_breakpoints__[__pie_counter__] = PieVirtualBreakpoint(set_func, __pie_counter__, addr)
        __pie_counter__ += 1
        return


@register_command
class PieInfoCommand(GenericCommand):
    """Display breakpoint info."""

    _cmdline_ = "pie info"
    _syntax_ = "{:s} BREAKPOINT".format(_cmdline_)

    @parse_arguments({"breakpoints": [-1,]}, {})
    def do_invoke(self, argv, *args, **kwargs):
        global __pie_breakpoints__

        args = kwargs["arguments"]
        if args.breakpoints[0] == -1:
            # No breakpoint info needed
            bps = [__pie_breakpoints__[x] for x in __pie_breakpoints__]
        else:
            bps = [__pie_breakpoints__[x] for x in args.breakpoints]

        lines = []
        lines.append("VNum\tNum\tAddr")
        lines += [
            "{}\t{}\t{}".format(x.vbp_num, x.bp_num if x.bp_num else "N/A", x.addr) for x in bps
        ]
        gef_print("\n".join(lines))
        return


@register_command
class PieDeleteCommand(GenericCommand):
    """Delete a PIE breakpoint."""

    _cmdline_ = "pie delete"
    _syntax_ = "{:s} [BREAKPOINT]".format(_cmdline_)

    @parse_arguments({"breakpoints": [-1,]}, {})
    def do_invoke(self, argv, *args, **kwargs):
        global __pie_breakpoints__
        args = kwargs["arguments"]
        if args.breakpoints[0] == -1:
            # no arg, delete all
            to_delete = [__pie_breakpoints__[x] for x in __pie_breakpoints__]
            self.delete_bp(to_delete)
        else:
            self.delete_bp([__pie_breakpoints__[x] for x in args.breakpoints])
        return


    @staticmethod
    def delete_bp(breakpoints):
        global __pie_breakpoints__
        for bp in breakpoints:
            # delete current real breakpoints if exists
            if bp.bp_num:
                gdb.execute("delete {}".format(bp.bp_num))
            # delete virtual breakpoints
            del __pie_breakpoints__[bp.vbp_num]
        return


@register_command
class PieRunCommand(GenericCommand):
    """Run process with PIE breakpoint support."""

    _cmdline_ = "pie run"
    _syntax_ = _cmdline_

    def do_invoke(self, argv):
        global __pie_breakpoints__
        fpath = get_filepath()
        if fpath is None:
            warn("No executable to debug, use `file` to load a binary")
            return

        if not os.access(fpath, os.X_OK):
            warn("The file '{}' is not executable.".format(fpath))
            return

        if is_alive():
            warn("gdb is already running. Restart process.")

        # get base address
        gdb.execute("set stop-on-solib-events 1")
        hide_context()
        gdb.execute("run {}".format(" ".join(argv)))
        unhide_context()
        gdb.execute("set stop-on-solib-events 0")
        vmmap = get_process_maps()
        base_address = [x.page_start for x in vmmap if x.path == get_filepath()][0]
        info("base address {}".format(hex(base_address)))

        # modify all breakpoints
        for bp_ins in __pie_breakpoints__.values():
            bp_ins.instantiate(base_address)

        try:
            gdb.execute("continue")
        except gdb.error as e:
            err(e)
            gdb.execute("kill")
        return


@register_command
class PieAttachCommand(GenericCommand):
    """Do attach with PIE breakpoint support."""

    _cmdline_ = "pie attach"
    _syntax_ = "{:s} PID".format(_cmdline_)

    def do_invoke(self, argv):
        try:
            gdb.execute("attach {}".format(" ".join(argv)), to_string=True)
        except gdb.error as e:
            err(e)
            return
        # after attach, we are stopped so that we can
        # get base address to modify our breakpoint
        vmmap = get_process_maps()
        base_address = [x.page_start for x in vmmap if x.path == get_filepath()][0]

        for bp_ins in __pie_breakpoints__.values():
            bp_ins.instantiate(base_address)
        gdb.execute("context")
        return


@register_command
class PieRemoteCommand(GenericCommand):
    """Attach to a remote connection with PIE breakpoint support."""

    _cmdline_ = "pie remote"
    _syntax_ = "{:s} REMOTE".format(_cmdline_)

    def do_invoke(self, argv):
        try:
            gdb.execute("gef-remote {}".format(" ".join(argv)))
        except gdb.error as e:
            err(e)
            return
        # after remote attach, we are stopped so that we can
        # get base address to modify our breakpoint
        vmmap = get_process_maps()
        base_address = [x.page_start for x in vmmap if x.realpath == get_filepath()][0]

        for bp_ins in __pie_breakpoints__.values():
            bp_ins.instantiate(base_address)
        gdb.execute("context")
        return


@register_command
class SmartEvalCommand(GenericCommand):
    """SmartEval: Smart eval (vague approach to mimic WinDBG `?`)."""

    _cmdline_ = "$"
    _syntax_  = "{0:s} EXPR\n{0:s} ADDRESS1 ADDRESS2".format(_cmdline_)
    _example_ = "\n{0:s} $pc+1\n{0:s} 0x00007ffff7a10000 0x00007ffff7bce000".format(_cmdline_)

    def do_invoke(self, argv):
        argc = len(argv)
        if argc == 1:
            self.evaluate(argv)
            return

        if argc == 2:
            self.distance(argv)
        return

    def evaluate(self, expr):
        def show_as_int(i):
            off = gef.arch.ptrsize*8
            def comp2_x(x): return "{:x}".format((x + (1 << off)) % (1 << off))
            def comp2_b(x): return "{:b}".format((x + (1 << off)) % (1 << off))

            try:
                s_i = comp2_x(res)
                s_i = s_i.rjust(len(s_i)+1, "0") if len(s_i)%2 else s_i
                gef_print("{:d}".format(i))
                gef_print("0x" + comp2_x(res))
                gef_print("0b" + comp2_b(res))
                gef_print("{}".format(binascii.unhexlify(s_i)))
                gef_print("{}".format(binascii.unhexlify(s_i)[::-1]))
            except:
                pass
            return

        parsed_expr = []
        for xp in expr:
            try:
                xp = gdb.parse_and_eval(xp)
                xp = int(xp)
                parsed_expr.append("{:d}".format(xp))
            except gdb.error:
                parsed_expr.append(str(xp))

        try:
            res = eval(" ".join(parsed_expr))
            if isinstance(res, int):
                show_as_int(res)
            else:
                gef_print("{}".format(res))
        except SyntaxError:
            gef_print(" ".join(parsed_expr))
        return

    def distance(self, args):
        try:
            x = int(args[0], 16) if is_hex(args[0]) else int(args[0])
            y = int(args[1], 16) if is_hex(args[1]) else int(args[1])
            gef_print("{}".format(abs(x - y)))
        except ValueError:
            warn("Distance requires 2 numbers: {} 0 0xffff".format(self._cmdline_))
        return


@register_command
class CanaryCommand(GenericCommand):
    """Shows the canary value of the current process. Apply the techique detailed in
    https://www.elttam.com.au/blog/playing-with-canaries/ to show the canary."""

    _cmdline_ = "canary"
    _syntax_ = _cmdline_

    @only_if_gdb_running
    def do_invoke(self, argv):
        self.dont_repeat()

        has_canary = checksec(get_filepath())["Canary"]
        if not has_canary:
            warn("This binary was not compiled with SSP.")
            return

        res = gef_read_canary()
        if not res:
            err("Failed to get the canary")
            return

        canary, location = res
        info("Found AT_RANDOM at {:#x}, reading {} bytes".format(location, gef.arch.ptrsize))
        info("The canary of process {} is {:#x}".format(get_pid(), canary))
        return


@register_command
class ProcessStatusCommand(GenericCommand):
    """Extends the info given by GDB `info proc`, by giving an exhaustive description of the
    process status (file descriptors, ancestor, descendants, etc.)."""

    _cmdline_ = "process-status"
    _syntax_  = _cmdline_
    _aliases_ = ["status", ]

    def __init__(self):
        super().__init__(complete=gdb.COMPLETE_NONE)
        return

    @only_if_gdb_running
    @only_if_gdb_target_local
    def do_invoke(self, argv):
        self.show_info_proc()
        self.show_ancestor()
        self.show_descendants()
        self.show_fds()
        self.show_connections()
        return

    def get_state_of(self, pid):
        res = {}
        with open("/proc/{}/status".format(pid), "r") as f:
            file = f.readlines()
        for line in file:
            key, value = line.split(":", 1)
            res[key.strip()] = value.strip()
        return res

    def get_cmdline_of(self, pid):
        with open("/proc/{}/cmdline".format(pid), "r") as f:
            return f.read().replace("\x00", "\x20").strip()

    def get_process_path_of(self, pid):
        return os.readlink("/proc/{}/exe".format(pid))

    def get_children_pids(self, pid):
        ps = which("ps")
        cmd = [ps, "-o", "pid", "--ppid", "{}".format(pid), "--noheaders"]
        try:
            return [int(x) for x in gef_execute_external(cmd, as_list=True)]
        except Exception:
            return []

    def show_info_proc(self):
        info("Process Information")
        pid = get_pid()
        cmdline = self.get_cmdline_of(pid)
        gef_print("\tPID {} {}".format(RIGHT_ARROW, pid))
        gef_print("\tExecutable {} {}".format(RIGHT_ARROW, self.get_process_path_of(pid)))
        gef_print("\tCommand line {} '{}'".format(RIGHT_ARROW, cmdline))
        return

    def show_ancestor(self):
        info("Parent Process Information")
        ppid = int(self.get_state_of(get_pid())["PPid"])
        state = self.get_state_of(ppid)
        cmdline = self.get_cmdline_of(ppid)
        gef_print("\tParent PID {} {}".format(RIGHT_ARROW, state["Pid"]))
        gef_print("\tCommand line {} '{}'".format(RIGHT_ARROW, cmdline))
        return

    def show_descendants(self):
        info("Children Process Information")
        children = self.get_children_pids(get_pid())
        if not children:
            gef_print("\tNo child process")
            return

        for child_pid in children:
            state = self.get_state_of(child_pid)
            pid = state["Pid"]
            gef_print("\tPID {} {} (Name: '{}', CmdLine: '{}')".format(RIGHT_ARROW,
                                                                       pid,
                                                                       self.get_process_path_of(pid),
                                                                       self.get_cmdline_of(pid)))
            return

    def show_fds(self):
        pid = get_pid()
        path = "/proc/{:d}/fd".format(pid)

        info("File Descriptors:")
        items = os.listdir(path)
        if not items:
            gef_print("\tNo FD opened")
            return

        for fname in items:
            fullpath = os.path.join(path, fname)
            if os.path.islink(fullpath):
                gef_print("\t{:s} {:s} {:s}".format (fullpath, RIGHT_ARROW, os.readlink(fullpath)))
        return

    def list_sockets(self, pid):
        sockets = []
        path = "/proc/{:d}/fd".format(pid)
        items = os.listdir(path)
        for fname in items:
            fullpath = os.path.join(path, fname)
            if os.path.islink(fullpath) and os.readlink(fullpath).startswith("socket:"):
                p = os.readlink(fullpath).replace("socket:", "")[1:-1]
                sockets.append(int(p))
        return sockets

    def parse_ip_port(self, addr):
        ip, port = addr.split(":")
        return socket.inet_ntoa(struct.pack("<I", int(ip, 16))), int(port, 16)

    def show_connections(self):
        # https://github.com/torvalds/linux/blob/v4.7/include/net/tcp_states.h#L16
        tcp_states_str = {
            0x01: "TCP_ESTABLISHED",
            0x02: "TCP_SYN_SENT",
            0x03: "TCP_SYN_RECV",
            0x04: "TCP_FIN_WAIT1",
            0x05: "TCP_FIN_WAIT2",
            0x06: "TCP_TIME_WAIT",
            0x07: "TCP_CLOSE",
            0x08: "TCP_CLOSE_WAIT",
            0x09: "TCP_LAST_ACK",
            0x0A: "TCP_LISTEN",
            0x0B: "TCP_CLOSING",
            0x0C: "TCP_NEW_SYN_RECV",
        }

        udp_states_str = {
            0x07: "UDP_LISTEN",
        }

        info("Network Connections")
        pid = get_pid()
        sockets = self.list_sockets(pid)
        if not sockets:
            gef_print("\tNo open connections")
            return

        entries = dict()
        with open("/proc/{:d}/net/tcp".format(pid), "r") as tcp:
            entries["TCP"] = [x.split() for x in tcp.readlines()[1:]]
        with open("/proc/{:d}/net/udp".format(pid), "r") as udp:
            entries["UDP"] = [x.split() for x in udp.readlines()[1:]]

        for proto in entries:
            for entry in entries[proto]:
                local, remote, state = entry[1:4]
                inode = int(entry[9])
                if inode in sockets:
                    local = self.parse_ip_port(local)
                    remote = self.parse_ip_port(remote)
                    state = int(state, 16)
                    state_str = tcp_states_str[state] if proto == "TCP" else udp_states_str[state]

                    gef_print("\t{}:{} {} {}:{} ({})".format(local[0], local[1],
                                                             RIGHT_ARROW,
                                                             remote[0], remote[1],
                                                             state_str))
        return


@register_priority_command
class GefThemeCommand(GenericCommand):
    """Customize GEF appearance."""

    _cmdline_ = "theme"
    _syntax_ = "{:s} [KEY [VALUE]]".format(_cmdline_)

    def __init__(self, *args, **kwargs):
        super().__init__(self._cmdline_)
        self["context_title_line"] = ( "gray", "Color of the borders in context window")
        self["context_title_message"] = ( "cyan", "Color of the title in context window")
        self["default_title_line"] = ( "gray", "Default color of borders")
        self["default_title_message"] = ( "cyan", "Default color of title")
        self["table_heading"] = ( "blue", "Color of the column headings to tables (e.g. vmmap)")
        self["old_context"] = ( "gray", "Color to use to show things such as code that is not immediately relevant")
        self["disassemble_current_instruction"] = ( "green", "Color to use to highlight the current $pc when disassembling")
        self["dereference_string"] = ( "yellow", "Color of dereferenced string")
        self["dereference_code"] = ( "gray", "Color of dereferenced code")
        self["dereference_base_address"] = ( "cyan", "Color of dereferenced address")
        self["dereference_register_value"] = ( "bold blue", "Color of dereferenced register")
        self["registers_register_name"] = ( "blue", "Color of the register name in the register window")
        self["registers_value_changed"] = ( "bold red", "Color of the changed register in the register window")
        self["address_stack"] = ( "pink", "Color to use when a stack address is found")
        self["address_heap"] = ( "green", "Color to use when a heap address is found")
        self["address_code"] = ( "red", "Color to use when a code address is found")
        self["source_current_line"] = ( "green", "Color to use for the current code line in the source window")
        return

    def do_invoke(self, args):
        self.dont_repeat()
        argc = len(args)

        if argc == 0:
            for key in self.settings:
                setting = self[key]
                value = Color.colorify(setting, setting)
                gef_print("{:40s}: {:s}".format(key, value))
            return

        setting = args[0]
        if not setting in self:
            err("Invalid key")
            return

        if argc == 1:
            value = self[setting]
            value = Color.colorify(value, value)
            gef_print("{:40s}: {:s}".format(setting, value))
            return

        val = [x for x in args[1:] if x in Color.colors]
        self[setting] = " ".join(val)
        return


@register_command
class PCustomCommand(GenericCommand):
    """Dump user defined structure.
    This command attempts to reproduce WinDBG awesome `dt` command for GDB and allows
    to apply structures (from symbols or custom) directly to an address.
    Custom structures can be defined in pure Python using ctypes, and should be stored
    in a specific directory, whose path must be stored in the `pcustom.struct_path`
    configuration setting."""

    _cmdline_ = "pcustom"
    _syntax_  = "{:s} [list|edit <StructureName>|show <StructureName>]|<StructureName> 0xADDRESS]".format(_cmdline_)

    def __init__(self):
        super().__init__(prefix=True)
        self["struct_path"] = ( os.sep.join( (gef.config["gef.tempdir"], "structs")), "Path to store/load the structure ctypes files")
        self["max_depth"] = ( 4, "Maximum level of recursion supported")
        self["structure_name"] = ( "bold blue", "Color of the structure name")
        self["structure_type"] = ( "bold red", "Color of the attribute type")
        self["structure_size"] = ( "green", "Color of the attribute size")
        return


    def do_invoke(self, argv):
        argc = len(argv)
        if argc == 0:
            gdb.execute("pcustom list")
            return

        modname, structname = self.get_modulename_structname_from_arg(argv[0])

        if argc == 1:
            gdb.execute("pcustom show {}".format(structname))
        else:
            try:
                address = parse_address(argv[1])
            except gdb.error:
                err("Failed to parse '{:s}'".format(argv[1]))
                return

            self.apply_structure_to_address(modname, structname, address)
        return

    def get_pcustom_absolute_root_path(self):
        path = os.path.expanduser(gef.config["pcustom.struct_path"])
        path = os.path.realpath(path)
        if not os.path.isdir(path):
            raise RuntimeError("setting `struct_path` must be set correctly")
        return path

    def get_pcustom_filepath_for_structure(self, structure_name):
        structure_files = self.enumerate_structures()
        fpath = None
        for fname in structure_files:
            if structure_name in structure_files[fname]:
                fpath = fname
                break
        if not fpath:
            raise FileNotFoundError("no file for structure '{}'".format(structure_name))
        return fpath

    def is_valid_struct(self, structure_name):
        structure_files = self.enumerate_structures()
        all_structures = set()
        for fname in structure_files:
            all_structures |= structure_files[fname]
        return structure_name in all_structures

    def get_modulename_structname_from_arg(self, arg):
        modname, structname = arg.split(":", 1) if ":" in arg else (arg, arg)
        structname = structname.split(".", 1)[0] if "." in structname else structname
        return (modname, structname)

    def deserialize(self, struct, data):
        length = min(len(data), ctypes.sizeof(struct))
        ctypes.memmove(ctypes.addressof(struct), data, length)
        return

    def get_structure_class(self, modname, classname):
        """
        Returns a tuple of (class, instance) if modname!classname exists
        """
        _fpath = self.get_pcustom_filepath_for_structure(modname)
        _mod = self.load_module(_fpath)
        _class = getattr(_mod, classname)
        return _class, _class()


    @only_if_gdb_running
    def apply_structure_to_address(self, mod_name, struct_name, addr, depth=0):
        if not self.is_valid_struct(mod_name):
            err("Invalid structure name '{:s}'".format(struct_name))
            return

        if depth >= self["max_depth"]:
            warn("maximum recursion level reached")
            return

        try:
            _class, _struct = self.get_structure_class(mod_name, struct_name)
            data = gef.memory.read(addr, ctypes.sizeof(_struct))
        except gdb.MemoryError:
            err("{}Cannot reach memory {:#x}".format(" " * depth, addr))
            return

        self.deserialize(_struct, data)

        _regsize = get_memory_alignment()

        for field in _struct._fields_:
            _name, _type = field
            _value = getattr(_struct, _name)
            _offset = getattr(_class, _name).offset

            if (_regsize == 4 and _type is ctypes.c_uint32) \
               or (_regsize == 8 and _type is ctypes.c_uint64) \
               or (_regsize == ctypes.sizeof(ctypes.c_void_p) and _type is ctypes.c_void_p):
                # try to dereference pointers
                _value = RIGHT_ARROW.join(dereference_from(_value))

            line = []
            line += "  " * depth
            line += ("{:#x}+0x{:04x} {} : ".format(addr, _offset, _name)).ljust(40)
            line += "{} ({})".format(_value, _type.__name__)
            parsed_value = self.get_ctypes_value(_struct, _name, _value)
            if parsed_value:
                line += " {} {}".format(RIGHT_ARROW, parsed_value)
            gef_print("".join(line))

            if issubclass(_type, ctypes.Structure):
                self.apply_structure_to_address(mod_name, _type.__name__, addr + _offset, depth + 1)
            elif _type.__name__.startswith("LP_"): # hack
                __sub_type_name = _type.__name__.replace("LP_", "")
                __deref = u64( gef.memory.read(addr + _offset, 8) )
                self.apply_structure_to_address(mod_name, __sub_type_name, __deref, depth + 1)
        return

    def get_ctypes_value(self, struct, item, value):
        if not hasattr(struct, "_values_"): return ""
        values_list = getattr(struct, "_values_")
        default = ""
        for name, values in values_list:
            if name != item: continue
            if callable(values):
                return values(value)
            try:
                for val, desc in values:
                    if value == val: return desc
                    if val is None: default = desc
            except:
                err("Error while trying to obtain values from _values_[\"{}\"]".format(name))

        return default

    def enumerate_structure_files(self):
        """
        Return a list of all the files in the pcustom directory
        """
        module_files = []
        root = self.get_pcustom_absolute_root_path()
        for filen in os.listdir(root):
            name, ext = os.path.splitext(filen)
            if ext != ".py": continue
            if name == "__init__": continue
            fpath = os.sep.join([root, filen])
            module_files.append( os.path.realpath(fpath) )
        return module_files

    def enumerate_structures(self):
        """
        Return a hash of all the structures, with the key set the to filepath
        """
        structures = {}
        files = self.enumerate_structure_files()
        for module_path in files:
            module = self.load_module(module_path)
            structures[module_path] = self.enumerate_structures_from_module(module)
        return structures

    def load_module(self, file_path):
        """Load a custom module, and return it"""
        module_name = file_path.split(os.sep)[-1].replace(".py", "")
        spec = importlib.util.spec_from_file_location(module_name, file_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module

    def enumerate_structures_from_module(self, module):
        _invalid = {"BigEndianStructure", "LittleEndianStructure", "Structure"}
        _structs = {x for x in dir(module) \
                         if inspect.isclass(getattr(module, x)) \
                         and issubclass(getattr(module, x), ctypes.Structure)}
        return _structs - _invalid


@register_command
class PCustomListCommand(PCustomCommand):
    """PCustom: list available structures"""

    _cmdline_ = "pcustom list"
    _syntax_ = "{:s}".format(_cmdline_)

    def __init__(self):
        super().__init__()
        return

    def do_invoke(self, argv):
        self.__list_custom_structures()
        return

    def __list_custom_structures(self):
        """Dump the list of all the structures and their respective."""
        path = self.get_pcustom_absolute_root_path()
        info("Listing custom structures from '{:s}'".format(path))
        structures = self.enumerate_structures()
        struct_color = gef.config["pcustom.structure_type"]
        filename_color = gef.config["pcustom.structure_name"]
        for filename in structures:
            __modules = ", ".join([Color.colorify(x, struct_color) for x in structures[filename]])
            __filename = Color.colorify(filename, filename_color)
            gef_print("{:s} {:s} ({:s})".format(RIGHT_ARROW, __filename, __modules))
        return


@register_command
class PCustomShowCommand(PCustomCommand):
    """PCustom: show the content of a given structure"""

    _cmdline_ = "pcustom show"
    _syntax_ = "{:s} StructureName".format(_cmdline_)
    __aliases__ = ["pcustom create", "pcustom update"]

    def __init__(self):
        super().__init__()
        return

    def do_invoke(self, argv):
        if len(argv) == 0:
            self.usage()
            return

        modname, structname = self.get_modulename_structname_from_arg(argv[0])
        self.__dump_structure(modname, structname)
        return

    def __dump_structure(self, mod_name, struct_name):
        # If it's a builtin or defined in the ELF use gdb's `ptype`
        try:
            gdb.execute("ptype struct {:s}".format(struct_name))
            return
        except gdb.error:
            pass

        self.__dump_custom_structure(mod_name, struct_name)
        return

    def __dump_custom_structure(self, mod_name, struct_name):
        if not self.is_valid_struct(mod_name):
            err("Invalid structure name '{:s}'".format(struct_name))
            return

        _class, _struct = self.get_structure_class(mod_name, struct_name)

        for _name, _type in _struct._fields_:
            _size = ctypes.sizeof(_type)
            __name = Color.colorify(_name, gef.config["pcustom.structure_name"])
            __type = Color.colorify(_type.__name__, gef.config["pcustom.structure_type"])
            __size = Color.colorify(hex(_size), gef.config["pcustom.structure_size"])
            __offset = Color.boldify("{:04x}".format(getattr(_class, _name).offset))
            gef_print("{:s}   {:32s}   {:16s}  /* size={:s} */".format(__offset, __name, __type, __size))
        return


@register_command
class PCustomEditCommand(PCustomCommand):
    """PCustom: edit the content of a given structure"""

    _cmdline_ = "pcustom edit"
    _syntax_ = "{:s} StructureName".format(_cmdline_)
    __aliases__ = ["pcustom create", "pcustom new", "pcustom update"]

    def __init__(self):
        super().__init__()
        return

    def do_invoke(self, argv):
        if len(argv) == 0:
            self.usage()
            return

        modname, structname = self.get_modulename_structname_from_arg(argv[0])
        self.__create_or_edit_structure(modname, structname)
        return

    def __create_or_edit_structure(self, mod_name, struct_name):
        root = self.get_pcustom_absolute_root_path()
        if root is None:
            err("Invalid struct path")
            return

        try:
            fullname = self.get_pcustom_filepath_for_structure(mod_name)
            info("Editing '{:s}'".format(fullname))
        except FileNotFoundError:
            fullname = os.sep.join([root, struct_name + ".py"])
            ok("Creating '{:s}' from template".format(fullname))
            self.__create_new_structure_template(struct_name, fullname)

        cmd = (os.getenv("EDITOR") or "nano").split()
        cmd.append(fullname)
        return subprocess.call(cmd)

    def __create_new_structure_template(self, structname, fullname):
        template = [
            "from ctypes import *",
            "",
            "class ", structname, "(Structure):",
            "    _fields_ = []",
            "",
            "    _values_ = []",
            ""
        ]
        with open(fullname, "w") as f:
            f.write(os.sep.join(template))
        return


@register_command
class ChangeFdCommand(GenericCommand):
    """ChangeFdCommand: redirect file descriptor during runtime."""

    _cmdline_ = "hijack-fd"
    _syntax_ = "{:s} FD_NUM NEW_OUTPUT".format(_cmdline_)
    _example_ = "{:s} 2 /tmp/stderr_output.txt".format(_cmdline_)

    @only_if_gdb_running
    @only_if_gdb_target_local
    def do_invoke(self, argv):
        if len(argv) != 2:
            self.usage()
            return

        if not os.access("/proc/{:d}/fd/{:s}".format(get_pid(), argv[0]), os.R_OK):
            self.usage()
            return

        old_fd = int(argv[0])
        new_output = argv[1]

        if ":" in new_output:
            address = socket.gethostbyname(new_output.split(":")[0])
            port = int(new_output.split(":")[1])

            AF_INET = 2
            SOCK_STREAM = 1
            res = gdb.execute("""call (int)socket({}, {}, 0)""".format(AF_INET, SOCK_STREAM), to_string=True)
            new_fd = self.get_fd_from_result(res)

            # fill in memory with sockaddr_in struct contents
            # we will do this in the stack, since connect() wants a pointer to a struct
            vmmap = get_process_maps()
            stack_addr = [entry.page_start for entry in vmmap if entry.path == "[stack]"][0]
            original_contents = gef.memory.read(stack_addr, 8)

            gef.memory.write(stack_addr, "\x02\x00", 2)
            gef.memory.write(stack_addr + 0x2, struct.pack("<H", socket.htons(port)), 2)
            gef.memory.write(stack_addr + 0x4, socket.inet_aton(address), 4)

            info("Trying to connect to {}".format(new_output))
            res = gdb.execute("""call (int)connect({}, {}, {})""".format(new_fd, stack_addr, 16), to_string=True)

            # recover stack state
            gef.memory.write(stack_addr, original_contents, 8)

            res = self.get_fd_from_result(res)
            if res == -1:
                err("Failed to connect to {}:{}".format(address, port))
                return

            info("Connected to {}".format(new_output))
        else:
            res = gdb.execute("""call (int)open("{:s}", 66, 0666)""".format(new_output), to_string=True)
            new_fd = self.get_fd_from_result(res)

        info("Opened '{:s}' as fd #{:d}".format(new_output, new_fd))
        gdb.execute("""call (int)dup2({:d}, {:d})""".format(new_fd, old_fd), to_string=True)
        info("Duplicated fd #{:d}{:s}#{:d}".format(new_fd, RIGHT_ARROW, old_fd))
        gdb.execute("""call (int)close({:d})""".format(new_fd), to_string=True)
        info("Closed extra fd #{:d}".format(new_fd))
        ok("Success")
        return

    def get_fd_from_result(self, res):
        # Output example: $1 = 3
        res = int(res.split()[2], 0)
        res = gdb.execute("""p/d {}""".format(res), to_string=True)
        res = int(res.split()[2], 0)
        return res

@register_command
class IdaInteractCommand(GenericCommand):
    """IDA Interact: set of commands to interact with IDA via a XML RPC service
    deployed via the IDA script `ida_gef.py`. It should be noted that this command
    can also be used to interact with Binary Ninja (using the script `binja_gef.py`)
    using the same interface."""

    _cmdline_ = "ida-interact"
    _syntax_ = "{:s} METHOD [ARGS]".format(_cmdline_)
    _aliases_ = ["binaryninja-interact", "bn", "binja"]
    _example_ = "\n{0:s} Jump $pc\n{0:s} SetColor $pc ff00ff".format(_cmdline_)

    def __init__(self):
        super().__init__(prefix=False)
        host, port = "127.0.0.1", 1337
        self["host"] = ( host, "IP address to use connect to IDA/Binary Ninja script")
        self["port"] = ( port, "Port to use connect to IDA/Binary Ninja script")
        self["sync_cursor"] = ( False, "Enable real-time $pc synchronisation")

        self.sock = None
        self.version = ("", "")
        self.old_bps = set()
        return

    def is_target_alive(self, host, port):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(1)
            s.connect((host, port))
            s.close()
        except socket.error:
            return False
        return True

    def connect(self, host=None, port=None):
        """Connect to the XML-RPC service."""
        host = host or self["host"]
        port = port or self["port"]

        try:
            sock = xmlrpclib.ServerProxy("http://{:s}:{:d}".format(host, port))
            gef_on_stop_hook(ida_synchronize_handler)
            gef_on_continue_hook(ida_synchronize_handler)
            self.version = sock.version()
        except ConnectionRefusedError:
            err("Failed to connect to '{:s}:{:d}'".format(host, port))
            sock = None
        self.sock = sock
        return

    def disconnect(self):
        gef_on_stop_unhook(ida_synchronize_handler)
        gef_on_continue_unhook(ida_synchronize_handler)
        self.sock = None
        return

    @deprecated("")
    def do_invoke(self, argv):
        def parsed_arglist(arglist):
            args = []
            for arg in arglist:
                try:
                    # try to solve the argument using gdb
                    argval = gdb.parse_and_eval(arg)
                    argval.fetch_lazy()
                    # check if value is addressable
                    argval = int(argval) if argval.address is None else int(argval.address)
                    # if the bin is PIE, we need to subtract the base address
                    if is_pie(get_filepath()) and main_base_address <= argval < main_end_address:
                        argval -= main_base_address
                    args.append("{:#x}".format(argval,))
                except Exception:
                    # if gdb can't parse the value, let ida deal with it
                    args.append(arg)
            return args

        if self.sock is None:
            # trying to reconnect
            self.connect()
            if self.sock is None:
                self.disconnect()
                return

        if len(argv) == 0 or argv[0] in ("-h", "--help"):
            method_name = argv[1] if len(argv) > 1 else None
            self.usage(method_name)
            return

        method_name = argv[0].lower()
        if method_name == "version":
            self.version = self.sock.version()
            info("Enhancing {:s} with {:s} (SDK {:s})".format(Color.greenify("gef"),
                                                            Color.redify(self.version[0]),
                                                            Color.yellowify(self.version[1])))
            return

        if not is_alive():
            main_base_address = main_end_address = 0
        else:
            vmmap = get_process_maps()
            main_base_address = min([x.page_start for x in vmmap if x.realpath == get_filepath()])
            main_end_address = max([x.page_end for x in vmmap if x.realpath == get_filepath()])

        try:
            if method_name == "sync":
                self.synchronize()
            else:
                method = getattr(self.sock, method_name)
                if len(argv) > 1:
                    args = parsed_arglist(argv[1:])
                    res = method(*args)
                else:
                    res = method()

                if method_name == "importstruct":
                    self.import_structures(res)
                else:
                    gef_print(str(res))

            if self["sync_cursor"] is True:
                jump = getattr(self.sock, "jump")
                jump(hex(gef.arch.pc-main_base_address),)

        except socket.error:
            self.disconnect()
        return

    def synchronize(self):
        """Submit all active breakpoint addresses to IDA/BN."""
        pc = gef.arch.pc
        vmmap = get_process_maps()
        base_address = min([x.page_start for x in vmmap if x.path == get_filepath()])
        end_address = max([x.page_end for x in vmmap if x.path == get_filepath()])
        if not (base_address <= pc < end_address):
            # do not sync in library
            return

        breakpoints = gdb.breakpoints() or []
        gdb_bps = set()
        for bp in breakpoints:
            if bp.enabled and not bp.temporary:
                if bp.location[0] == "*": # if it's an address i.e. location starts with "*"
                    addr = parse_address(bp.location[1:])
                else:  # it is a symbol
                    addr = int(gdb.parse_and_eval(bp.location).address)
                if not (base_address <= addr < end_address):
                    continue
                gdb_bps.add(addr - base_address)

        added = gdb_bps - self.old_bps
        removed = self.old_bps - gdb_bps
        self.old_bps = gdb_bps

        try:
            # it is possible that the server was stopped between now and the last sync
            rc = self.sock.sync("{:#x}".format(pc-base_address), list(added), list(removed))
        except ConnectionRefusedError:
            self.disconnect()
            return

        ida_added, ida_removed = rc

        # add new bp from IDA
        for new_bp in ida_added:
            location = base_address + new_bp
            gdb.Breakpoint("*{:#x}".format(location), type=gdb.BP_BREAKPOINT)
            self.old_bps.add(location)

        # and remove the old ones
        breakpoints = gdb.breakpoints() or []
        for bp in breakpoints:
            if bp.enabled and not bp.temporary:
                if bp.location[0] == "*": # if it's an address i.e. location starts with "*"
                    addr = parse_address(bp.location[1:])
                else:  # it is a symbol
                    addr = int(gdb.parse_and_eval(bp.location).address)

                if not (base_address <= addr < end_address):
                    continue

                if (addr - base_address) in ida_removed:
                    if (addr - base_address) in self.old_bps:
                        self.old_bps.remove((addr - base_address))
                    bp.delete()
        return

    def usage(self, meth=None):
        if self.sock is None:
            return

        if meth is not None:
            gef_print(titlify(meth))
            gef_print(self.sock.system.methodHelp(meth))
            return

        info("Listing available methods and syntax examples: ")
        for m in self.sock.system.listMethods():
            if m.startswith("system."): continue
            gef_print(titlify(m))
            gef_print(self.sock.system.methodHelp(m))
        return

    def import_structures(self, structs):
        if self.version[0] != "IDA Pro":
            return

        path = gef.config["pcustom.struct_path"]
        if path is None:
            return

        if not os.path.isdir(path):
            gef_makedirs(path)

        for struct_name in structs:
            fullpath = os.path.join(path, "{}.py".format(struct_name))
            with open(fullpath, "w") as f:
                f.write("from ctypes import *\n\n")
                f.write("class ")
                f.write(struct_name)
                f.write("(Structure):\n")
                f.write("    _fields_ = [\n")
                for _, name, size in structs[struct_name]:
                    name = bytes(name, encoding="utf-8")
                    if size == 1: csize = "c_uint8"
                    elif size == 2: csize = "c_uint16"
                    elif size == 4: csize = "c_uint32"
                    elif size == 8: csize = "c_uint64"
                    else:           csize = "c_byte * {}".format(size)
                    m = '        (\"{}\", {}),\n'.format(name, csize)
                    f.write(m)
                f.write("]\n")
        ok("Success, {:d} structure{:s} imported".format(len(structs), "s" if len(structs)>1 else ""))
        return


@register_command
class ScanSectionCommand(GenericCommand):
    """Search for addresses that are located in a memory mapping (haystack) that belonging
    to another (needle)."""

    _cmdline_ = "scan"
    _syntax_  = "{:s} HAYSTACK NEEDLE".format(_cmdline_)
    _aliases_ = ["lookup",]
    _example_ = "\n{0:s} stack libc".format(_cmdline_)

    @only_if_gdb_running
    def do_invoke(self, argv):
        if len(argv) != 2:
            self.usage()
            return

        haystack = argv[0]
        needle = argv[1]

        info("Searching for addresses in '{:s}' that point to '{:s}'"
             .format(Color.yellowify(haystack), Color.yellowify(needle)))

        if haystack == "binary":
            haystack = get_filepath()

        if needle == "binary":
            needle = get_filepath()

        needle_sections = []
        haystack_sections = []

        if "0x" in haystack:
            start, end = parse_string_range(haystack)
            haystack_sections.append((start, end, ""))

        if "0x" in needle:
            start, end = parse_string_range(needle)
            needle_sections.append((start, end))

        for sect in get_process_maps():
            if haystack in sect.path:
                haystack_sections.append((sect.page_start, sect.page_end, os.path.basename(sect.path)))
            if needle in sect.path:
                needle_sections.append((sect.page_start, sect.page_end))

        step = gef.arch.ptrsize
        unpack = u32 if step == 4 else u64

        for hstart, hend, hname in haystack_sections:
            try:
                mem = gef.memory.read(hstart, hend - hstart)
            except gdb.MemoryError:
                continue

            for i in range(0, len(mem), step):
                target = unpack(mem[i:i+step])
                for nstart, nend in needle_sections:
                    if target >= nstart and target < nend:
                        deref = DereferenceCommand.pprint_dereferenced(hstart, int(i / step))
                        if hname != "":
                            name = Color.colorify(hname, "yellow")
                            gef_print("{:s}: {:s}".format(name, deref))
                        else:
                            gef_print(" {:s}".format(deref))

        return


@register_command
class SearchPatternCommand(GenericCommand):
    """SearchPatternCommand: search a pattern in memory. If given an hex value (starting with 0x)
    the command will also try to look for upwards cross-references to this address."""

    _cmdline_ = "search-pattern"
    _syntax_ = "{:s} PATTERN [little|big] [section]".format(_cmdline_)
    _aliases_ = ["grep", "xref"]
    _example_ = "\n{0:s} AAAAAAAA\n{0:s} 0x555555554000 little stack\n{0:s} AAAA 0x600000-0x601000".format(_cmdline_)

    def print_section(self, section):
        title = "In "
        if section.path:
            title += "'{}'".format(Color.blueify(section.path))

        title += "({:#x}-{:#x})".format(section.page_start, section.page_end)
        title += ", permission={}".format(section.permission)
        ok(title)
        return

    def print_loc(self, loc):
        gef_print("""  {:#x} - {:#x} {}  "{}" """.format(loc[0], loc[1], RIGHT_ARROW, Color.pinkify(loc[2]),))
        return

    def search_pattern_by_address(self, pattern, start_address, end_address):
        """Search a pattern within a range defined by arguments."""
        pattern = gef_pybytes(pattern)
        step = 0x400 * 0x1000
        locations = []

        for chunk_addr in range(start_address, end_address, step):
            if chunk_addr + step > end_address:
                chunk_size = end_address - chunk_addr
            else:
                chunk_size = step

            try:
                mem = gef.memory.read(chunk_addr, chunk_size)
            except gdb.error as e:
                estr = str(e)
                if estr.startswith("Cannot access memory "):
                    #
                    # This is a special case where /proc/$pid/maps
                    # shows virtual memory address with a read bit,
                    # but it cannot be read directly from userspace.
                    #
                    # See: https://github.com/hugsy/gef/issues/674
                    #
                    err(estr)
                    return []
                else:
                    raise e

            for match in re.finditer(pattern, mem):
                start = chunk_addr + match.start()
                if is_ascii_string(start):
                    ustr = gef.memory.read_ascii_string(start)
                    end = start + len(ustr)
                else:
                    ustr = gef_pystring(pattern) + "[...]"
                    end = start + len(pattern)
                locations.append((start, end, ustr))

            del mem

        return locations

    def search_pattern(self, pattern, section_name):
        """Search a pattern within the whole userland memory."""
        for section in get_process_maps():
            if not section.permission & Permission.READ: continue
            if section.path == "[vvar]": continue
            if not section_name in section.path: continue

            start = section.page_start
            end = section.page_end - 1
            old_section = None

            for loc in self.search_pattern_by_address(pattern, start, end):
                addr_loc_start = lookup_address(loc[0])
                if addr_loc_start and addr_loc_start.section:
                    if old_section != addr_loc_start.section:
                        self.print_section(addr_loc_start.section)
                        old_section = addr_loc_start.section

                self.print_loc(loc)
        return

    @only_if_gdb_running
    def do_invoke(self, argv):
        argc = len(argv)
        if argc < 1:
            self.usage()
            return

        pattern = argv[0]
        endian = get_endian()

        if argc >= 2:
            if argv[1].lower() == "big": endian = Elf.BIG_ENDIAN
            elif argv[1].lower() == "little": endian = Elf.LITTLE_ENDIAN

        if is_hex(pattern):
            if endian == Elf.BIG_ENDIAN:
                pattern = "".join(["\\x" + pattern[i:i + 2] for i in range(2, len(pattern), 2)])
            else:
                pattern = "".join(["\\x" + pattern[i:i + 2] for i in range(len(pattern) - 2, 0, -2)])

        if argc == 3:
            info("Searching '{:s}' in {:s}".format(Color.yellowify(pattern), argv[2]))

            if "0x" in argv[2]:
                start, end = parse_string_range(argv[2])

                loc = lookup_address(start)
                if loc.valid:
                    self.print_section(loc.section)

                for loc in self.search_pattern_by_address(pattern, start, end):
                    self.print_loc(loc)
            else:
                section_name = argv[2]
                if section_name == "binary":
                    section_name = get_filepath()

                self.search_pattern(pattern, section_name)
        else:
            info("Searching '{:s}' in memory".format(Color.yellowify(pattern)))
            self.search_pattern(pattern, "")
        return


@register_command
class FlagsCommand(GenericCommand):
    """Edit flags in a human friendly way."""

    _cmdline_ = "edit-flags"
    _syntax_  = "{:s} [(+|-|~)FLAGNAME ...]".format(_cmdline_)
    _aliases_ = ["flags",]
    _example_ = "\n{0:s}\n{0:s} +zero # sets ZERO flag".format(_cmdline_)

    def do_invoke(self, argv):
        for flag in argv:
            if len(flag) < 2:
                continue

            action = flag[0]
            name = flag[1:].lower()

            if action not in ("+", "-", "~"):
                err("Invalid action for flag '{:s}'".format(flag))
                continue

            if name not in gef.arch.flags_table.values():
                err("Invalid flag name '{:s}'".format(flag[1:]))
                continue

            for off in gef.arch.flags_table:
                if gef.arch.flags_table[off] == name:
                    old_flag = get_register(gef.arch.flag_register)
                    if action == "+":
                        new_flags = old_flag | (1 << off)
                    elif action == "-":
                        new_flags = old_flag & ~(1 << off)
                    else:
                        new_flags = old_flag ^ (1 << off)

                    gdb.execute("set ({:s}) = {:#x}".format(gef.arch.flag_register, new_flags))

        gef_print(gef.arch.flag_register_to_human())
        return


@register_command
class ChangePermissionCommand(GenericCommand):
    """Change a page permission. By default, it will change it to 7 (RWX)."""

    _cmdline_ = "set-permission"
    _syntax_  = "{:s} address [permission]\n"\
                "\taddress\t\tan address within the memory page for which the permissions should be changed\n"\
                "\tpermission\ta 3-bit bitmask with read=1, write=2 and execute=4 as integer".format(_cmdline_)
    _aliases_ = ["mprotect"]
    _example_ = "{:s} $sp 7".format(_cmdline_)

    def __init__(self):
        super().__init__(complete=gdb.COMPLETE_LOCATION)
        return

    def pre_load(self):
        try:
            __import__("keystone")
        except ImportError:
            msg = "Missing `keystone-engine` package, install with: `pip install keystone-engine`."
            raise ImportWarning(msg)
        return

    @only_if_gdb_running
    def do_invoke(self, argv):
        if len(argv) not in (1, 2):
            err("Incorrect syntax")
            self.usage()
            return

        if len(argv) == 2:
            perm = int(argv[1])
        else:
            perm = Permission.READ | Permission.WRITE | Permission.EXECUTE

        loc = safe_parse_and_eval(argv[0])
        if loc is None:
            err("Invalid address")
            return

        loc = int(loc)
        sect = process_lookup_address(loc)
        if sect is None:
            err("Unmapped address")
            return

        size = sect.page_end - sect.page_start
        original_pc = gef.arch.pc

        info("Generating sys_mprotect({:#x}, {:#x}, '{:s}') stub for arch {:s}"
             .format(sect.page_start, size, str(Permission(value=perm)), get_arch()))
        stub = self.get_stub_by_arch(sect.page_start, size, perm)
        if stub is None:
            err("Failed to generate mprotect opcodes")
            return

        info("Saving original code")
        original_code = gef.memory.read(original_pc, len(stub))

        bp_loc = "*{:#x}".format(original_pc + len(stub))
        info("Setting a restore breakpoint at {:s}".format(bp_loc))
        ChangePermissionBreakpoint(bp_loc, original_code, original_pc)

        info("Overwriting current memory at {:#x} ({:d} bytes)".format(loc, len(stub)))
        gef.memory.write(original_pc, stub, len(stub))

        info("Resuming execution")
        gdb.execute("continue")
        return

    def get_stub_by_arch(self, addr, size, perm):
        code = gef.arch.mprotect_asm(addr, size, perm)
        arch, mode = get_keystone_arch()
        raw_insns = keystone_assemble(code, arch, mode, raw=True)
        return raw_insns


@register_command
class UnicornEmulateCommand(GenericCommand):
    """Use Unicorn-Engine to emulate the behavior of the binary, without affecting the GDB runtime.
    By default the command will emulate only the next instruction, but location and number of
    instruction can be changed via arguments to the command line. By default, it will emulate
    the next instruction from current PC."""

    _cmdline_ = "unicorn-emulate"
    _syntax_  = """{:s} [--start LOCATION] [--until LOCATION] [--skip-emulation] [--output-file PATH] [NB_INSTRUCTION]
\n\t--start LOCATION specifies the start address of the emulated run (default $pc).
\t--until LOCATION specifies the end address of the emulated run.
\t--skip-emulation\t do not execute the script once generated.
\t--output-file /PATH/TO/SCRIPT.py writes the persistent Unicorn script into this file.
\tNB_INSTRUCTION indicates the number of instructions to execute
\nAdditional options can be setup via `gef config unicorn-emulate`
""".format(_cmdline_)
    _aliases_ = ["emulate",]
    _example_ = "{0:s} --start $pc 10 --output-file /tmp/my-gef-emulation.py".format(_cmdline_)

    def __init__(self):
        super().__init__(complete=gdb.COMPLETE_LOCATION)
        self["verbose"] = ( False, "Set unicorn-engine in verbose mode")
        self["show_disassembly"] = ( False, "Show every instruction executed")
        return

    def pre_load(self):
        try:
            __import__("unicorn")
        except ImportError:
            msg = "Missing `unicorn` package for Python. Install with `pip install unicorn`."
            raise ImportWarning(msg)

        try:
            __import__("capstone")
        except ImportError:
            msg = "Missing `capstone` package for Python. Install with `pip install capstone`."
            raise ImportWarning(msg)
        return

    @only_if_gdb_running
    @parse_arguments({"nb": 1}, {"--start": "", "--until": "", "--skip-emulation": True, "--output-file": ""})
    def do_invoke(self, *args, **kwargs):
        args = kwargs["arguments"]
        start_address = parse_address(str(args.start or gef.arch.pc))
        end_address = parse_address(str(args.until or self.get_unicorn_end_addr(start_address, args.nb)))
        self.run_unicorn(start_address, end_address, skip_emulation=args.skip_emulation, to_file=args.output_file)
        return

    def get_unicorn_end_addr(self, start_addr, nb):
        dis = list(gef_disassemble(start_addr, nb + 1))
        last_insn = dis[-1]
        return last_insn.address

    def run_unicorn(self, start_insn_addr, end_insn_addr, *args, **kwargs):
        verbose = self["verbose"] or False
        skip_emulation = kwargs.get("skip_emulation", False)
        arch, mode = get_unicorn_arch(to_string=True)
        unicorn_registers = get_unicorn_registers(to_string=True)
        cs_arch, cs_mode = get_capstone_arch(to_string=True)
        fname = get_filename()
        to_file = kwargs.get("to_file", None)
        emulate_segmentation_block = ""
        context_segmentation_block = ""

        if to_file:
            tmp_filename = to_file
            to_file = open(to_file, "w")
            tmp_fd = to_file.fileno()
        else:
            tmp_fd, tmp_filename = tempfile.mkstemp(suffix=".py", prefix="gef-uc-")

        if is_x86():
            # need to handle segmentation (and pagination) via MSR
            emulate_segmentation_block = """
# from https://github.com/unicorn-engine/unicorn/blob/master/tests/regress/x86_64_msr.py
SCRATCH_ADDR = 0xf000
SEGMENT_FS_ADDR = 0x5000
SEGMENT_GS_ADDR = 0x6000
FSMSR = 0xC0000100
GSMSR = 0xC0000101

def set_msr(uc, msr, value, scratch=SCRATCH_ADDR):
    buf = b"\\x0f\\x30"  # x86: wrmsr
    uc.mem_map(scratch, 0x1000)
    uc.mem_write(scratch, buf)
    uc.reg_write(unicorn.x86_const.UC_X86_REG_RAX, value & 0xFFFFFFFF)
    uc.reg_write(unicorn.x86_const.UC_X86_REG_RDX, (value >> 32) & 0xFFFFFFFF)
    uc.reg_write(unicorn.x86_const.UC_X86_REG_RCX, msr & 0xFFFFFFFF)
    uc.emu_start(scratch, scratch+len(buf), count=1)
    uc.mem_unmap(scratch, 0x1000)
    return

def set_gs(uc, addr):    return set_msr(uc, GSMSR, addr)
def set_fs(uc, addr):    return set_msr(uc, FSMSR, addr)

"""

            context_segmentation_block = """
    emu.mem_map(SEGMENT_FS_ADDR-0x1000, 0x3000)
    set_fs(emu, SEGMENT_FS_ADDR)
    set_gs(emu, SEGMENT_GS_ADDR)
"""

        content = """#!{pythonbin} -i
#
# Emulation script for "{fname}" from {start:#x} to {end:#x}
#
# Powered by gef, unicorn-engine, and capstone-engine
#
# @_hugsy_
#
import collections
import capstone, unicorn

registers = collections.OrderedDict(sorted({{{regs}}}.items(), key=lambda t: t[0]))
uc = None
verbose = {verbose}
syscall_register = "{syscall_reg}"

def disassemble(code, addr):
    cs = capstone.Cs({cs_arch}, {cs_mode})
    for i in cs.disasm(code, addr):
        return i

def hook_code(emu, address, size, user_data):
    code = emu.mem_read(address, size)
    insn = disassemble(code, address)
    print(">>> {{:#x}}: {{:s}} {{:s}}".format(insn.address, insn.mnemonic, insn.op_str))
    return

def code_hook(emu, address, size, user_data):
    code = emu.mem_read(address, size)
    insn = disassemble(code, address)
    print(">>> {{:#x}}: {{:s}} {{:s}}".format(insn.address, insn.mnemonic, insn.op_str))
    return

def intr_hook(emu, intno, data):
    print(" \\-> interrupt={{:d}}".format(intno))
    return

def syscall_hook(emu, user_data):
    sysno = emu.reg_read(registers[syscall_register])
    print(" \\-> syscall={{:d}}".format(sysno))
    return

def print_regs(emu, regs):
    for i, r in enumerate(regs):
        print("{{:7s}} = {{:#0{ptrsize}x}}  ".format(r, emu.reg_read(regs[r])), end="")
        if (i % 4 == 3) or (i == len(regs)-1): print("")
    return

{emu_block}

def reset():
    emu = unicorn.Uc({arch}, {mode})

{context_block}
""".format(pythonbin=PYTHONBIN, fname=fname, start=start_insn_addr, end=end_insn_addr,
           regs=",".join(["'%s': %s" % (k.strip(), unicorn_registers[k]) for k in unicorn_registers]),
           verbose="True" if verbose else "False",
           syscall_reg=gef.arch.syscall_register,
           cs_arch=cs_arch, cs_mode=cs_mode,
           ptrsize=gef.arch.ptrsize * 2 + 2,  # two hex chars per byte plus "0x" prefix
           emu_block=emulate_segmentation_block if is_x86() else "",
           arch=arch, mode=mode,
           context_block=context_segmentation_block if is_x86() else "")

        if verbose:
            info("Duplicating registers")

        for r in gef.arch.all_registers:
            gregval = get_register(r)
            content += "    emu.reg_write({}, {:#x})\n".format(unicorn_registers[r], gregval)

        vmmap = get_process_maps()
        if not vmmap:
            warn("An error occurred when reading memory map.")
            return

        if verbose:
            info("Duplicating memory map")

        for sect in vmmap:
            if sect.path == "[vvar]":
                # this section is for GDB only, skip it
                continue

            page_start = sect.page_start
            page_end   = sect.page_end
            size       = sect.size
            perm       = sect.permission

            content += "    # Mapping {}: {:#x}-{:#x}\n".format(sect.path, page_start, page_end)
            content += "    emu.mem_map({:#x}, {:#x}, {})\n".format(page_start, size, oct(perm.value))

            if perm & Permission.READ:
                code = gef.memory.read(page_start, size)
                loc = "/tmp/gef-{}-{:#x}.raw".format(fname, page_start)
                with open(loc, "wb") as f:
                    f.write(bytes(code))

                content += "    emu.mem_write({:#x}, open('{}', 'rb').read())\n".format(page_start, loc)
                content += "\n"

        content += "    emu.hook_add(unicorn.UC_HOOK_CODE, code_hook)\n"
        content += "    emu.hook_add(unicorn.UC_HOOK_INTR, intr_hook)\n"
        if is_x86_64():
            content += "    emu.hook_add(unicorn.UC_HOOK_INSN, syscall_hook, None, 1, 0, unicorn.x86_const.UC_X86_INS_SYSCALL)\n"
        content += "    return emu\n"

        content += """
def emulate(emu, start_addr, end_addr):
    print("========================= Initial registers =========================")
    print_regs(emu, registers)

    try:
        print("========================= Starting emulation =========================")
        emu.emu_start(start_addr, end_addr)
    except Exception as e:
        emu.emu_stop()
        print("========================= Emulation failed =========================")
        print("[!] Error: {{}}".format(e))

    print("========================= Final registers =========================")
    print_regs(emu, registers)
    return


uc = reset()
emulate(uc, {start:#x}, {end:#x})

# unicorn-engine script generated by gef
""".format(start=start_insn_addr, end=end_insn_addr)

        os.write(tmp_fd, gef_pybytes(content))
        os.close(tmp_fd)

        if kwargs.get("to_file", None):
            info("Unicorn script generated as '{}'".format(tmp_filename))
            os.chmod(tmp_filename, 0o700)

        if skip_emulation:
            return

        ok("Starting emulation: {:#x} {} {:#x}".format(start_insn_addr, RIGHT_ARROW, end_insn_addr))

        res = gef_execute_external([PYTHONBIN, tmp_filename], as_list=True)
        gef_print("\n".join(res))

        if not kwargs.get("to_file", None):
            os.unlink(tmp_filename)
        return


@register_command
class RemoteCommand(GenericCommand):
    """gef wrapper for the `target remote` command. This command will automatically
    download the target binary in the local temporary directory (defaut /tmp) and then
    source it. Additionally, it will fetch all the /proc/PID/maps and loads all its
    information."""

    _cmdline_ = "gef-remote"
    _syntax_  = "{:s} [OPTIONS] TARGET".format(_cmdline_)
    _example_  = "\n{0:s} --pid 6789 localhost:1234"\
        "\n{0:s} --qemu-mode localhost:4444 # when using qemu-user".format(_cmdline_)

    def __init__(self):
        super().__init__(prefix=False)
        self.handler_connected = False
        self["clean_on_exit"] = ( False, "Clean the temporary data downloaded when the session exits.")
        return

    @parse_arguments(
        {"target": ""},
        {"--update-solib": True,
         "--download-everything": True,
         "--download-lib": "",
         "--is-extended-remote": True,
         "--pid": 0,
         "--qemu-mode": True})
    def do_invoke(self, argv, *args, **kwargs):
        global __gef_remote__

        if __gef_remote__ is not None:
            err("You already are in remote session. Close it first before opening a new one...")
            return

        # argument check
        args = kwargs["arguments"]
        if not args.target or ":" not in args.target:
            err("A target (HOST:PORT) must always be provided.")
            return

        if args.is_extended_remote and not args.pid:
            err("A PID (--pid) is required for extended remote debugging")
            return

        target = args.target
        self.download_all_libs = args.download_everything

        if args.qemu_mode:
            # compat layer for qemu-user
            self.prepare_qemu_stub(target)
            return

        # lazily install handler on first use
        if not self.handler_connected:
            gef_on_new_hook(self.new_objfile_handler)
            self.handler_connected = True

        if not self.connect_target(target, args.is_extended_remote):
            return

        pid = args.pid if args.is_extended_remote and args.pid else get_pid()
        if args.is_extended_remote:
            ok("Attaching to {:d}".format(pid))
            hide_context()
            gdb.execute("attach {:d}".format(pid))
            unhide_context()

        self.setup_remote_environment(pid, args.update_solib)

        if not is_remote_debug():
            err("Failed to establish remote target environment.")
            return

        if self.download_all_libs:
            vmmap = get_process_maps()
            success = 0
            for sect in vmmap:
                if sect.path.startswith("/"):
                    _file = download_file(sect.path)
                    if _file is None:
                        err("Failed to download {:s}".format(sect.path))
                    else:
                        success += 1

            ok("Downloaded {:d} files".format(success))

        elif args.download_lib:
            _file = download_file(args.download_lib)
            if _file is None:
                err("Failed to download remote file")
                return

            ok("Download success: {:s} {:s} {:s}".format(args.download_lib, RIGHT_ARROW, _file))

        if args.update_solib:
            self.refresh_shared_library_path()


        # refresh the architecture setting
        set_arch()
        __gef_remote__ = pid
        return

    def new_objfile_handler(self, event):
        """Hook that handles new_objfile events, will update remote environment accordingly."""
        if not is_remote_debug():
            return

        if self.download_all_libs and event.new_objfile.filename.startswith("target:"):
            remote_lib = event.new_objfile.filename[len("target:"):]
            local_lib = download_file(remote_lib, use_cache=True)
            if local_lib:
                ok("Download success: {:s} {:s} {:s}".format(remote_lib, RIGHT_ARROW, local_lib))
        return

    def setup_remote_environment(self, pid, update_solib=False):
        """Clone the remote environment locally in the temporary directory.
        The command will duplicate the entries in the /proc/<pid> locally and then
        source those information into the current gdb context to allow gef to use
        all the extra commands as it was local debugging."""
        gdb.execute("reset-cache")

        infos = {}
        for i in ("maps", "environ", "cmdline",):
            infos[i] = self.load_from_remote_proc(pid, i)
            if infos[i] is None:
                err("Failed to load memory map of '{:s}'".format(i))
                return

        exepath = get_path_from_info_proc()
        infos["exe"] = download_file("/proc/{:d}/exe".format(pid), use_cache=False, local_name=exepath)
        if not os.access(infos["exe"], os.R_OK):
            err("Source binary is not readable")
            return

        directory  = os.path.sep.join([gef.config["gef.tempdir"], str(get_pid())])
        # gdb.execute("file {:s}".format(infos["exe"]))
        self["root"] = ( directory, "Path to store the remote data")
        ok("Remote information loaded to temporary path '{:s}'".format(directory))
        return

    def connect_target(self, target, is_extended_remote):
        """Connect to remote target and get symbols. To prevent `gef` from requesting information
        not fetched just yet, we disable the context disable when connection was successful."""
        hide_context()
        try:
            cmd = "target {} {}".format("extended-remote" if is_extended_remote else "remote", target)
            gdb.execute(cmd)
            ok("Connected to '{}'".format(target))
            ret = True
        except Exception as e:
            err("Failed to connect to {:s}: {:s}".format(target, str(e)))
            ret = False
        unhide_context()
        return ret

    def load_from_remote_proc(self, pid, info):
        """Download one item from /proc/pid."""
        remote_name = "/proc/{:d}/{:s}".format(pid, info)
        return download_file(remote_name, use_cache=False)

    def refresh_shared_library_path(self):
        dirs = [r for r, d, f in os.walk(self["root"])]
        path = ":".join(dirs)
        gdb.execute("set solib-search-path {:s}".format(path,))
        return

    def usage(self):
        h = self._syntax_
        h += "\n\t   TARGET (mandatory) specifies the host:port, serial port or tty to connect to.\n"
        h += "\t-U will update gdb `solib-search-path` attribute to include the files downloaded from server (default: False).\n"
        h += "\t-A will download *ALL* the remote shared libraries and store them in the new environment. " \
             "This command can take a few minutes to complete (default: False).\n"
        h += "\t-D LIB will download the remote library called LIB.\n"
        h += "\t-E Use 'extended-remote' to connect to the target.\n"
        h += "\t-p PID (mandatory if -E is used) specifies PID of the debugged process on gdbserver's end.\n"
        h += "\t-q Uses this option when connecting to a Qemu GDBserver.\n"
        info(h)
        return

    def prepare_qemu_stub(self, target):
        global gef, __gef_qemu_mode__

        reset_all_caches()
        arch = get_arch()
        gef.binary = Elf(minimalist=True)
        if arch.startswith("arm"):
            gef.binary.e_machine = Elf.ARM
            gef.arch = ARM()
        elif arch.startswith("aarch64"):
            gef.binary.e_machine = Elf.AARCH64
            gef.arch = AARCH64()
        elif arch.startswith("i386:intel"):
            gef.binary.e_machine = Elf.X86_32
            gef.arch = X86()
        elif arch.startswith("i386:x86-64"):
            gef.binary.e_machine = Elf.X86_64
            gef.binary.e_class = Elf.ELF_64_BITS
            gef.arch = X86_64()
        elif arch.startswith("mips"):
            gef.binary.e_machine = Elf.MIPS
            gef.arch = MIPS()
        elif arch.startswith("powerpc"):
            gef.binary.e_machine = Elf.POWERPC
            gef.arch = PowerPC()
        elif arch.startswith("sparc"):
            gef.binary.e_machine = Elf.SPARC
            gef.arch = SPARC()
        else:
            raise RuntimeError("unsupported architecture: {}".format(arch))

        ok("Setting Qemu-user stub for '{}' (memory mapping may be wrong)".format(gef.arch.arch))
        hide_context()
        gdb.execute("target remote {}".format(target))
        unhide_context()

        if get_pid() == 1 and "ENABLE=1" in gdb.execute("maintenance packet Qqemu.sstepbits", to_string=True, from_tty=False):
            __gef_qemu_mode__ = True
            reset_all_caches()
            info("Note: By using Qemu mode, GEF will display the memory mapping of the Qemu process where the emulated binary resides")
            get_process_maps()
            gdb.execute("context")
        return


@register_command
class NopCommand(GenericCommand):
    """Patch the instruction(s) pointed by parameters with NOP. Note: this command is architecture
    aware."""

    _cmdline_ = "nop"
    _syntax_  = """{:s} [LOCATION] [--nb NUM_BYTES]
  LOCATION\taddress/symbol to patch
    --nb NUM_BYTES\tInstead of writing one instruction, patch the specified number of bytes""".format(_cmdline_)
    _example_ = "{:s} $pc".format(_cmdline_)

    def __init__(self):
        super().__init__(complete=gdb.COMPLETE_LOCATION)
        return

    def get_insn_size(self, addr):
        cur_insn = gef_current_instruction(addr)
        next_insn = gef_instruction_n(addr, 2)
        return next_insn.address - cur_insn.address

    @parse_arguments({"address": "$pc"}, {"--nb": 0, })
    def do_invoke(self, argv, *args, **kwargs):
        args = kwargs["arguments"]
        address = parse_address(args.address) if args.address else gef.arch.pc
        number_of_bytes = args.nb or 1
        self.nop_bytes(address, number_of_bytes)
        return

    @only_if_gdb_running
    def nop_bytes(self, loc, num_bytes):
        size = self.get_insn_size(loc) if num_bytes == 0 else num_bytes
        nops = gef.arch.nop_insn

        if len(nops) > size:
            m = "Cannot patch instruction at {:#x} (nop_size is:{:d},insn_size is:{:d})".format(loc, len(nops), size)
            err(m)
            return

        while len(nops) < size:
            nops += gef.arch.nop_insn

        if len(nops) != size:
            err("Cannot patch instruction at {:#x} (nop instruction does not evenly fit in requested size)"
                .format(loc))
            return

        ok("Patching {:d} bytes from {:s}".format(size, format_address(loc)))
        gef.memory.write(loc, nops, size)

        return


@register_command
class StubCommand(GenericCommand):
    """Stub out the specified function. This function is useful when needing to skip one
    function to be called and disrupt your runtime flow (ex. fork)."""

    _cmdline_ = "stub"
    _syntax_  = """{:s} [--retval RETVAL] [address]
\taddress\taddress/symbol to stub out
\t--retval RETVAL\tSet the return value""".format(_cmdline_)
    _example_ = "{:s} --retval 0 fork".format(_cmdline_)

    def __init__(self):
        super().__init__(complete=gdb.COMPLETE_LOCATION)
        return

    @only_if_gdb_running
    @parse_arguments({"address": ""}, {("-r", "--retval"): 0})
    def do_invoke(self, argv, *args, **kwargs):
        args = kwargs["arguments"]
        loc = args.address if args.address else "*{:#x}".format(gef.arch.pc)
        StubBreakpoint(loc, args.retval)
        return


@register_command
class CapstoneDisassembleCommand(GenericCommand):
    """Use capstone disassembly framework to disassemble code."""

    _cmdline_ = "capstone-disassemble"
    _syntax_  = "{:s} [-h] [--show-opcodes] [--length LENGTH] [LOCATION]".format(_cmdline_)
    _aliases_ = ["cs-dis"]
    _example_ = "{:s} --length 50 $pc".format(_cmdline_)

    def pre_load(self):
        try:
            __import__("capstone")
        except ImportError:
            msg = "Missing `capstone` package for Python. Install with `pip install capstone`."
            raise ImportWarning(msg)
        return

    def __init__(self):
        super().__init__(complete=gdb.COMPLETE_LOCATION)
        return

    @only_if_gdb_running
    @parse_arguments({("location"): "$pc"}, {("--show-opcodes", "-s"): True, "--length": 0})
    def do_invoke(self, argv, *args, **kwargs):
        args = kwargs["arguments"]
        show_opcodes = args.show_opcodes
        length = args.length or gef.config["context.nb_lines_code"]
        location = parse_address(args.location)
        if not location:
            info("Can't find address for {}".format(args.location))
            return

        insns = []
        opcodes_len = 0
        for insn in capstone_disassemble(location, length, skip=length * self.repeat_count, **kwargs):
            insns.append(insn)
            opcodes_len = max(opcodes_len, len(insn.opcodes))

        for insn in insns:
            insn_fmt = "{{:{}o}}".format(opcodes_len) if show_opcodes else "{}"
            text_insn = insn_fmt.format(insn)
            msg = ""

            if insn.address == gef.arch.pc:
                msg = Color.colorify("{}   {}".format(RIGHT_ARROW, text_insn), "bold red")
                reason = self.capstone_analyze_pc(insn, length)[0]
                if reason:
                    gef_print(msg)
                    gef_print(reason)
                    break
            else:
                msg = "{} {}".format(" " * 5, text_insn)

            gef_print(msg)
        return

    def capstone_analyze_pc(self, insn, nb_insn):
        if gef.arch.is_conditional_branch(insn):
            is_taken, reason = gef.arch.is_branch_taken(insn)
            if is_taken:
                reason = "[Reason: {:s}]".format(reason) if reason else ""
                msg = Color.colorify("\tTAKEN {:s}".format(reason), "bold green")
            else:
                reason = "[Reason: !({:s})]".format(reason) if reason else ""
                msg = Color.colorify("\tNOT taken {:s}".format(reason), "bold red")
            return (is_taken, msg)

        if gef.arch.is_call(insn):
            target_address = int(insn.operands[-1].split()[0], 16)
            msg = []
            for i, new_insn in enumerate(capstone_disassemble(target_address, nb_insn)):
                msg.append("   {}  {}".format(DOWN_ARROW if i == 0 else " ", str(new_insn)))
            return (True, "\n".join(msg))

        return (False, "")


@register_command
class GlibcHeapCommand(GenericCommand):
    """Base command to get information about the Glibc heap structure."""

    _cmdline_ = "heap"
    _syntax_  = "{:s} (chunk|chunks|bins|arenas|set-arena)".format(_cmdline_)

    def __init__(self):
        super().__init__(prefix=True)
        return

    @only_if_gdb_running
    def do_invoke(self, argv):
        self.usage()
        return


@register_command
class GlibcHeapSetArenaCommand(GenericCommand):
    """Display information on a heap chunk."""

    _cmdline_ = "heap set-arena"
    _syntax_  = "{:s} [address|symbol]".format(_cmdline_)
    _example_ = "{:s} 0x001337001337".format(_cmdline_)

    def __init__(self):
        super().__init__(complete=gdb.COMPLETE_LOCATION)
        return

    @only_if_gdb_running
    def do_invoke(self, argv):
        global gef

        if not argv:
            ok("Current arena set to: '{}'".format(gef.heap.selected_arena))
            return

        if is_hex(argv[0]):
            new_arena_address = argv[0]
        else:
            new_arena_symbol = safe_parse_and_eval(argv[0])
            if not new_arena_symbol:
                err("Invalid symbol for arena")
                return

        new_arena_address = Address(value=to_unsigned_long(new_arena_symbol))
        if not new_arena_address or not new_arena_address.valid:
            err("Invalid address")
            return

        new_arena = GlibcArena(f"*{new_arena_address:#x}")
        if new_arena not in gef.heap.arenas:
            err("Invalid arena")
            return

        gef.heap.selected_arena = new_arena
        return


@register_command
class GlibcHeapArenaCommand(GenericCommand):
    """Display information on a heap chunk."""

    _cmdline_ = "heap arenas"
    _syntax_  = _cmdline_

    @only_if_gdb_running
    def do_invoke(self, argv):
        # arenas = get_glibc_arenas()
        for arena in gef.heap.arenas:
            print("foo")
            gef_print(str(arena))
        return


@register_command
class GlibcHeapChunkCommand(GenericCommand):
    """Display information on a heap chunk.
    See https://github.com/sploitfun/lsploits/blob/master/glibc/malloc/malloc.c#L1123."""

    _cmdline_ = "heap chunk"
    _syntax_  = "{:s} [-h] [--allow-unaligned] [--number] address".format(_cmdline_)

    def __init__(self):
        super().__init__(complete=gdb.COMPLETE_LOCATION)
        return

    @parse_arguments({"address": ""}, {"--allow-unaligned": True, "--number": 1})
    @only_if_gdb_running
    def do_invoke(self, *args, **kwargs):
        args = kwargs["arguments"]
        if not args.address:
            err("Missing chunk address")
            self.usage()
            return

        addr = parse_address(args.address)
        current_chunk = GlibcChunk(addr, allow_unaligned=args.allow_unaligned)

        if args.number > 1:
            for _ in range(args.number):
                if current_chunk.size == 0:
                    break

                gef_print(str(current_chunk))
                next_chunk_addr = current_chunk.get_next_chunk_addr()
                if not Address(value=next_chunk_addr).valid:
                    break

                next_chunk = current_chunk.get_next_chunk()
                if next_chunk is None:
                    break

                current_chunk = next_chunk
        else:
            gef_print(current_chunk.psprint())
        return


@register_command
class GlibcHeapChunksCommand(GenericCommand):
    """Display all heap chunks for the current arena. As an optional argument
    the base address of a different arena can be passed"""

    _cmdline_ = "heap chunks"
    _syntax_  = "{0} [-h] [--all] [--allow-unaligned] [arena_address]".format(_cmdline_)
    _example_ = "\n{0}\n{0} 0x555555775000".format(_cmdline_)

    def __init__(self):
        super().__init__(complete=gdb.COMPLETE_LOCATION)
        self["peek_nb_byte"] = ( 16, "Hexdump N first byte(s) inside the chunk data (0 to disable)")
        return

    @parse_arguments({"arena_address": ""}, {("--all", "-a"): True, "--allow-unaligned": True})
    @only_if_gdb_running
    def do_invoke(self, *args, **kwargs):
        args = kwargs["arguments"]

        # arenas = get_glibc_arenas(addr=args.arena_address, get_all=args.all)
        arenas = gef.heap.arenas
        for arena in arenas:
            self.dump_chunks_arena(arena, print_arena=args.all, allow_unaligned=args.allow_unaligned)
            if not args.all:
                break

    def dump_chunks_arena(self, arena, print_arena=False, allow_unaligned=False):
        top_chunk_addr = arena.top
        heap_addr = arena.heap_addr(allow_unaligned=allow_unaligned)
        if heap_addr is None:
            err("Could not find heap for arena")
            return
        if print_arena:
            gef_print(str(arena))
        if arena.is_main_arena():
            self.dump_chunks_heap(heap_addr, top=top_chunk_addr, allow_unaligned=allow_unaligned)
        else:
            heap_info_structs = arena.get_heap_info_list()
            first_heap_info = heap_info_structs.pop(0)
            heap_info_t_size = int(arena) - first_heap_info.addr
            until = first_heap_info.addr + first_heap_info.size
            self.dump_chunks_heap(heap_addr, until=until, top=top_chunk_addr, allow_unaligned=allow_unaligned)
            for heap_info in heap_info_structs:
                start = heap_info.addr + heap_info_t_size
                until = heap_info.addr + heap_info.size
                self.dump_chunks_heap(start, until=until, top=top_chunk_addr, allow_unaligned=allow_unaligned)
        return

    def dump_chunks_heap(self, start, until=None, top=None, allow_unaligned=False):
        nb = self["peek_nb_byte"]
        current_chunk = GlibcChunk(start, from_base=True, allow_unaligned=allow_unaligned)
        while True:
            if current_chunk.base_address == top:
                gef_print("{} {} {}".format(str(current_chunk), LEFT_ARROW, Color.greenify("top chunk")))
                break
            if current_chunk.size == 0:
                break
            line = str(current_chunk)
            if nb:
                line += "\n    [{}]".format(hexdump(gef.memory.read(current_chunk.data_address, nb), nb, base=current_chunk.data_address))
            gef_print(line)

            next_chunk_addr = current_chunk.get_next_chunk_addr()
            if until and next_chunk_addr >= until:
                break
            if not Address(value=next_chunk_addr).valid:
                break

            next_chunk = current_chunk.get_next_chunk()
            if next_chunk is None:
                break

            current_chunk = next_chunk
        return


@register_command
class GlibcHeapBinsCommand(GenericCommand):
    """Display information on the bins on an arena (default: main_arena).
    See https://github.com/sploitfun/lsploits/blob/master/glibc/malloc/malloc.c#L1123."""

    _bin_types_ = ["tcache", "fast", "unsorted", "small", "large"]
    _cmdline_ = "heap bins"
    _syntax_ = "{:s} [{:s}]".format(_cmdline_, "|".join(_bin_types_))

    def __init__(self):
        super().__init__(prefix=True, complete=gdb.COMPLETE_LOCATION)
        return

    @only_if_gdb_running
    def do_invoke(self, argv):
        if not argv:
            for bin_t in GlibcHeapBinsCommand._bin_types_:
                gdb.execute("heap bins {:s}".format(bin_t))
            return

        bin_t = argv[0]
        if bin_t not in GlibcHeapBinsCommand._bin_types_:
            self.usage()
            return

        gdb.execute("heap bins {}".format(bin_t))
        return

    @staticmethod
    def pprint_bin(arena_addr, index, _type=""):
        arena = GlibcArena(arena_addr)
        fw, bk = arena.bin(index)

        if bk == 0x00 and fw == 0x00:
            warn("Invalid backward and forward bin pointers(fw==bk==NULL)")
            return -1

        nb_chunk = 0
        head = GlibcChunk(bk, from_base=True).fwd
        if fw == head:
            return nb_chunk

        ok("{}bins[{:d}]: fw={:#x}, bk={:#x}".format(_type, index, fw, bk))

        m = []
        while fw != head:
            chunk = GlibcChunk(fw, from_base=True)
            m.append("{:s}  {:s}".format(RIGHT_ARROW, str(chunk)))
            fw = chunk.fwd
            nb_chunk += 1

        if m:
            gef_print("  ".join(m))
        return nb_chunk


@register_command
class GlibcHeapTcachebinsCommand(GenericCommand):
    """Display information on the Tcachebins on an arena (default: main_arena).
    See https://sourceware.org/git/?p=glibc.git;a=commitdiff;h=d5c3fafc4307c9b7a4c7d5cb381fcdbfad340bcc."""

    _cmdline_ = "heap bins tcache"
    _syntax_  = "{:s} [all] [thread_ids...]".format(_cmdline_)

    TCACHE_MAX_BINS = 0x40

    def __init__(self):
        super().__init__(complete=gdb.COMPLETE_LOCATION)
        return

    @only_if_gdb_running
    def do_invoke(self, argv):
        # Determine if we are using libc with tcache built in (2.26+)
        if get_libc_version() < (2, 26):
            info("No Tcache in this version of libc")
            return

        current_thread = gdb.selected_thread()
        if current_thread is None:
            err("Couldn't find current thread")
            return

        # As a nicety, we want to display threads in ascending order by gdb number
        threads = sorted(gdb.selected_inferior().threads(), key=lambda t: t.num)
        if argv:
            if "all" in argv:
                tids = [t.num for t in threads]
            else:
                tids = self.check_thread_ids(argv)
        else:
            tids = [current_thread.num]

        for thread in threads:
            if thread.num not in tids:
                continue

            thread.switch()

            tcache_addr = self.find_tcache()
            if tcache_addr == 0:
                info("Uninitialized tcache for thread {:d}".format(thread.num))
                continue

            gef_print(titlify("Tcachebins for thread {:d}".format(thread.num)))
            tcache_empty = True
            for i in range(self.TCACHE_MAX_BINS):
                chunk, count = self.tcachebin(tcache_addr, i)
                chunks = set()
                msg = []

                # Only print the entry if there are valid chunks. Don't trust count
                while True:
                    if chunk is None:
                        break

                    try:
                        msg.append("{:s} {:s} ".format(LEFT_ARROW, str(chunk)))
                        if chunk.data_address in chunks:
                            msg.append("{:s} [loop detected]".format(RIGHT_ARROW))
                            break

                        chunks.add(chunk.data_address)

                        next_chunk = chunk.get_fwd_ptr(True)
                        if next_chunk == 0:
                            break

                        chunk = GlibcChunk(next_chunk)
                    except gdb.MemoryError:
                        msg.append("{:s} [Corrupted chunk at {:#x}]".format(LEFT_ARROW, chunk.data_address))
                        break

                if msg:
                    tcache_empty = False
                    gef_print("Tcachebins[idx={:d}, size={:#x}] count={:d} ".format(i, (i+2)*(gef.arch.ptrsize)*2, count), end="")
                    gef_print("".join(msg))

            if tcache_empty:
                gef_print("All tcachebins are empty")

        current_thread.switch()
        return

    @staticmethod
    def find_tcache():
        """Return the location of the current thread's tcache."""
        try:
            # For multithreaded binaries, the tcache symbol (in thread local
            # storage) will give us the correct address.
            tcache_addr = parse_address("(void *) tcache")
        except gdb.error:
            # In binaries not linked with pthread (and therefore there is only
            # one thread), we can't use the tcache symbol, but we can guess the
            # correct address because the tcache is consistently the first
            # allocation in the main arena.
            heap_base = HeapBaseFunction.heap_base()
            if heap_base is None:
                err("No heap section")
                return 0x0
            tcache_addr = heap_base + 0x10
        return tcache_addr

    @staticmethod
    def check_thread_ids(tids):
        """Check the validity, dedup, and return all valid tids."""
        existing_tids = [t.num for t in gdb.selected_inferior().threads()]
        valid_tids = set()
        for tid in tids:
            try:
                tid = int(tid)
            except ValueError:
                err("Invalid thread id {:s}".format(tid))
                continue
            if tid in existing_tids:
                valid_tids.add(tid)
            else:
                err("Unknown thread {}".format(tid))

        return list(valid_tids)

    @staticmethod
    def tcachebin(tcache_base, i):
        """Return the head chunk in tcache[i] and the number of chunks in the bin."""
        assert i <  GlibcHeapTcachebinsCommand.TCACHE_MAX_BINS, "index should be less then TCACHE_MAX_BINS"
        tcache_chunk = GlibcChunk(tcache_base)

        # Glibc changed the size of the tcache in version 2.30; this fix has
        # been backported inconsistently between distributions. We detect the
        # difference by checking the size of the allocated chunk for the
        # tcache.
        # Minimum usable size of allocated tcache chunk = ?
        #   For new tcache:
        #   TCACHE_MAX_BINS * _2_ + TCACHE_MAX_BINS * ptrsize
        #   For old tcache:
        #   TCACHE_MAX_BINS * _1_ + TCACHE_MAX_BINS * ptrsize
        new_tcache_min_size = (
                GlibcHeapTcachebinsCommand.TCACHE_MAX_BINS * 2 +
                GlibcHeapTcachebinsCommand.TCACHE_MAX_BINS * gef.arch.ptrsize)

        if tcache_chunk.usable_size < new_tcache_min_size:
            tcache_count_size = 1
            count = ord(gef.memory.read(tcache_base + tcache_count_size*i, 1))
        else:
            tcache_count_size = 2
            count = u16(gef.memory.read(tcache_base + tcache_count_size*i, 2))

        chunk = dereference(tcache_base + tcache_count_size*GlibcHeapTcachebinsCommand.TCACHE_MAX_BINS + i*gef.arch.ptrsize)
        chunk = GlibcChunk(int(chunk)) if chunk else None
        return chunk, count


@register_command
class GlibcHeapFastbinsYCommand(GenericCommand):
    """Display information on the fastbinsY on an arena (default: main_arena).
    See https://github.com/sploitfun/lsploits/blob/master/glibc/malloc/malloc.c#L1123."""

    _cmdline_ = "heap bins fast"
    _syntax_  = "{:s} [ARENA_ADDRESS]".format(_cmdline_)

    def __init__(self):
        super().__init__(complete=gdb.COMPLETE_LOCATION)
        return

    @only_if_gdb_running
    def do_invoke(self, argv):
        def fastbin_index(sz):
            return (sz >> 4) - 2 if SIZE_SZ == 8 else (sz >> 3) - 2

        SIZE_SZ = gef.arch.ptrsize
        MAX_FAST_SIZE = 80 * SIZE_SZ // 4
        NFASTBINS = fastbin_index(MAX_FAST_SIZE) - 1

        arena = GlibcArena("*{:s}".format(argv[0])) if len(argv) == 1 else get_glibc_arena()

        if arena is None:
            err("Invalid Glibc arena")
            return

        gef_print(titlify("Fastbins for arena {:#x}".format(int(arena))))
        for i in range(NFASTBINS):
            gef_print("Fastbins[idx={:d}, size={:#x}] ".format(i, (i+2)*SIZE_SZ*2), end="")
            chunk = arena.fastbin(i)
            chunks = set()

            while True:
                if chunk is None:
                    gef_print("0x00", end="")
                    break

                try:
                    gef_print("{:s} {:s} ".format(LEFT_ARROW, str(chunk)), end="")
                    if chunk.data_address in chunks:
                        gef_print("{:s} [loop detected]".format(RIGHT_ARROW), end="")
                        break

                    if fastbin_index(chunk.get_chunk_size()) != i:
                        gef_print("[incorrect fastbin_index] ", end="")

                    chunks.add(chunk.data_address)

                    next_chunk = chunk.get_fwd_ptr(True)
                    if next_chunk == 0:
                        break

                    chunk = GlibcChunk(next_chunk, from_base=True)
                except gdb.MemoryError:
                    gef_print("{:s} [Corrupted chunk at {:#x}]".format(LEFT_ARROW, chunk.data_address), end="")
                    break
            gef_print()
        return

@register_command
class GlibcHeapUnsortedBinsCommand(GenericCommand):
    """Display information on the Unsorted Bins of an arena (default: main_arena).
    See: https://github.com/sploitfun/lsploits/blob/master/glibc/malloc/malloc.c#L1689."""

    _cmdline_ = "heap bins unsorted"
    _syntax_  = "{:s} [ARENA_ADDRESS]".format(_cmdline_)

    def __init__(self):
        super().__init__(complete=gdb.COMPLETE_LOCATION)
        return

    @only_if_gdb_running
    def do_invoke(self, argv):
        if get_glibc_arena() is None:
            err("Invalid Glibc arena")
            return

        arena_addr = "*{:s}".format(argv[0]) if len(argv) == 1 else __gef_current_arena__
        gef_print(titlify("Unsorted Bin for arena '{:s}'".format(arena_addr)))
        nb_chunk = GlibcHeapBinsCommand.pprint_bin(arena_addr, 0, "unsorted_")
        if nb_chunk >= 0:
            info("Found {:d} chunks in unsorted bin.".format(nb_chunk))
        return

@register_command
class GlibcHeapSmallBinsCommand(GenericCommand):
    """Convenience command for viewing small bins."""

    _cmdline_ = "heap bins small"
    _syntax_  = "{:s} [ARENA_ADDRESS]".format(_cmdline_)

    def __init__(self):
        super().__init__(complete=gdb.COMPLETE_LOCATION)
        return

    @only_if_gdb_running
    def do_invoke(self, argv):
        if not gef.heap.main_arena:
            err("Heap not initialized")
            return

        arena = GlibcArena(f"*{argv[0]:s}") if len(argv) == 1 else gef.heap.selected_arena
        gef_print(titlify("Small Bins for arena '{:s}'".format(arena_addr)))
        bins = {}
        for i in range(1, 63):
            nb_chunk = GlibcHeapBinsCommand.pprint_bin(arena_addr, i, "small_")
            if nb_chunk < 0:
                break
            if nb_chunk > 0:
                bins[i] = nb_chunk
        info("Found {:d} chunks in {:d} small non-empty bins.".format(sum(bins.values()), len(bins)))
        return

@register_command
class GlibcHeapLargeBinsCommand(GenericCommand):
    """Convenience command for viewing large bins."""

    _cmdline_ = "heap bins large"
    _syntax_  = "{:s} [ARENA_ADDRESS]".format(_cmdline_)

    def __init__(self):
        super().__init__(complete=gdb.COMPLETE_LOCATION)
        return

    @only_if_gdb_running
    def do_invoke(self, argv):
        if get_glibc_arena() is None:
            err("Invalid Glibc arena")
            return

        arena_addr = "*{:s}".format(argv[0]) if len(argv) == 1 else __gef_current_arena__
        gef_print(titlify("Large Bins for arena '{:s}'".format(arena_addr)))
        bins = {}
        for i in range(63, 126):
            nb_chunk = GlibcHeapBinsCommand.pprint_bin(arena_addr, i, "large_")
            if nb_chunk < 0:
                break
            if nb_chunk > 0:
                bins[i] = nb_chunk
        info("Found {:d} chunks in {:d} large non-empty bins.".format(sum(bins.values()), len(bins)))
        return


@register_command
class SolveKernelSymbolCommand(GenericCommand):
    """Solve kernel symbols from kallsyms table."""

    _cmdline_ = "ksymaddr"
    _syntax_  = "{:s} SymbolToSearch".format(_cmdline_)
    _example_ = "{:s} prepare_creds".format(_cmdline_)

    @parse_arguments({"symbol": ""}, {})
    def do_invoke(self, *args, **kwargs):
        def hex_to_int(num):
            try:
                return int(num, 16)
            except ValueError:
                return 0
        args = kwargs["arguments"]
        if not args.symbol:
            self.usage()
            return
        sym = args.symbol
        with open("/proc/kallsyms", "r") as f:
            syms = [line.strip().split(" ", 2) for line in f]
        matches = [(hex_to_int(addr), sym_t, " ".join(name.split())) for addr, sym_t, name in syms if sym in name]
        for addr, sym_t, name in matches:
            if sym == name.split()[0]:
                ok("Found matching symbol for '{:s}' at {:#x} (type={:s})".format(name, addr, sym_t))
            else:
                warn("Found partial match for '{:s}' at {:#x} (type={:s}): {:s}".format(sym, addr, sym_t, name))
        if not matches:
            err("No match for '{:s}'".format(sym))
        elif matches[0][0] == 0:
            err("Check that you have the correct permissions to view kernel symbol addresses")
        return


@register_command
class DetailRegistersCommand(GenericCommand):
    """Display full details on one, many or all registers value from current architecture."""

    _cmdline_ = "registers"
    _syntax_  = "{:s} [[Register1][Register2] ... [RegisterN]]".format(_cmdline_)
    _example_ = "\n{0:s}\n{0:s} $eax $eip $esp".format(_cmdline_)

    @only_if_gdb_running
    @parse_arguments({"registers": [""]}, {})
    def do_invoke(self, argv, *args, **kwargs):
        unchanged_color = gef.config["theme.registers_register_name"]
        changed_color = gef.config["theme.registers_value_changed"]
        string_color = gef.config["theme.dereference_string"]
        regs = gef.arch.all_registers

        args = kwargs["arguments"]
        if args.registers and args.registers[0]:
            required_regs = set(args.registers)
            valid_regs = [reg for reg in gef.arch.all_registers if reg in required_regs]
            if valid_regs:
                regs = valid_regs
            invalid_regs = [reg for reg in required_regs if reg not in valid_regs]
            if invalid_regs:
                err("invalid registers for architecture: {}".format(", ".join(invalid_regs)))

        memsize = gef.arch.ptrsize
        endian = endian_str()
        charset = string.printable
        widest = max(map(len, gef.arch.all_registers))
        special_line = ""

        for regname in regs:
            reg = gdb.parse_and_eval(regname)
            if reg.type.code == gdb.TYPE_CODE_VOID:
                continue

            padreg = regname.ljust(widest, " ")

            if str(reg) == "<unavailable>":
                line = "{}: ".format(Color.colorify(padreg, unchanged_color))
                line += Color.colorify("no value", "yellow underline")
                gef_print(line)
                continue

            value = align_address(int(reg))
            old_value = ContextCommand.old_registers.get(regname, 0)
            if value == old_value:
                color = unchanged_color
            else:
                color = changed_color

            # Special (e.g. segment) registers go on their own line
            if regname in gef.arch.special_registers:
                special_line += "{}: ".format(Color.colorify(regname, color))
                special_line += "0x{:04x} ".format(get_register(regname))
                continue

            line = "{}: ".format(Color.colorify(padreg, color))

            if regname == gef.arch.flag_register:
                line += gef.arch.flag_register_to_human()
                gef_print(line)
                continue

            addr = lookup_address(align_address(int(value)))
            if addr.valid:
                line += str(addr)
            else:
                line += format_address_spaces(value)
            addrs = dereference_from(value)

            if len(addrs) > 1:
                sep = " {:s} ".format(RIGHT_ARROW)
                line += sep
                line += sep.join(addrs[1:])

            # check to see if reg value is ascii
            try:
                fmt = "{}{}".format(endian, "I" if memsize == 4 else "Q")
                last_addr = int(addrs[-1], 16)
                val = gef_pystring(struct.pack(fmt, last_addr))
                if all([_ in charset for _ in val]):
                    line += ' ("{:s}"?)'.format(Color.colorify(val, string_color))
            except ValueError:
                pass

            gef_print(line)

        if special_line:
            gef_print(special_line)
        return


@register_command
class ShellcodeCommand(GenericCommand):
    """ShellcodeCommand uses @JonathanSalwan simple-yet-awesome shellcode API to
    download shellcodes."""

    _cmdline_ = "shellcode"
    _syntax_  = "{:s} (search|get)".format(_cmdline_)

    def __init__(self):
        super().__init__(prefix=True)
        return

    def do_invoke(self, argv):
        err("Missing sub-command (search|get)")
        self.usage()
        return


@register_command
class ShellcodeSearchCommand(GenericCommand):
    """Search pattern in shell-storm's shellcode database."""

    _cmdline_ = "shellcode search"
    _syntax_  = "{:s} PATTERN1 PATTERN2".format(_cmdline_)
    _aliases_ = ["sc-search",]

    api_base = "http://shell-storm.org"
    search_url = "{}/api/?s=".format(api_base)

    def do_invoke(self, argv):
        if not argv:
            err("Missing pattern to search")
            self.usage()
            return

        self.search_shellcode(argv)
        return

    def search_shellcode(self, search_options):
        # API : http://shell-storm.org/shellcode/
        args = "*".join(search_options)

        res = http_get(self.search_url + args)
        if res is None:
            err("Could not query search page")
            return

        ret = gef_pystring(res)

        # format: [author, OS/arch, cmd, id, link]
        lines = ret.split("\\n")
        refs = [line.split("::::") for line in lines]

        if refs:
            info("Showing matching shellcodes")
            info("\t".join(["Id", "Platform", "Description"]))
            for ref in refs:
                try:
                    _, arch, cmd, sid, _ = ref
                    gef_print("\t".join([sid, arch, cmd]))
                except ValueError:
                    continue

            info("Use `shellcode get <id>` to fetch shellcode")
        return


@register_command
class ShellcodeGetCommand(GenericCommand):
    """Download shellcode from shell-storm's shellcode database."""

    _cmdline_ = "shellcode get"
    _syntax_  = "{:s} SHELLCODE_ID".format(_cmdline_)
    _aliases_ = ["sc-get",]

    api_base = "http://shell-storm.org"
    get_url = "{}/shellcode/files/shellcode-{{:d}}.php".format(api_base)

    def do_invoke(self, argv):
        if len(argv) != 1:
            err("Missing ID to download")
            self.usage()
            return

        if not argv[0].isdigit():
            err("ID is not a number")
            self.usage()
            return

        self.get_shellcode(int(argv[0]))
        return

    def get_shellcode(self, sid):
        info("Downloading shellcode id={:d}".format(sid))
        res = http_get(self.get_url.format(sid))
        if res is None:
            err("Failed to fetch shellcode #{:d}".format(sid))
            return

        ok("Downloaded, written to disk...")
        tempdir = gef.config["gef.tempdir"]
        fd, fname = tempfile.mkstemp(suffix=".txt", prefix="sc-", text=True, dir=tempdir)
        shellcode = res.splitlines()[7:-11]
        shellcode = b"\n".join(shellcode).replace(b"&quot;", b'"')
        os.write(fd, shellcode)
        os.close(fd)
        ok("Shellcode written to '{:s}'".format(fname))
        return


@register_command
class RopperCommand(GenericCommand):
    """Ropper (http://scoding.de/ropper) plugin."""

    _cmdline_ = "ropper"
    _syntax_  = "{:s} [ROPPER_OPTIONS]".format(_cmdline_)

    def __init__(self):
        super().__init__(complete=gdb.COMPLETE_NONE)
        return

    def pre_load(self):
        try:
            __import__("ropper")
        except ImportError:
            msg = "Missing `ropper` package for Python, install with: `pip install ropper`."
            raise ImportWarning(msg)
        return

    @only_if_gdb_running
    def do_invoke(self, argv):
        ropper = sys.modules["ropper"]
        if "--file" not in argv:
            path = get_filepath()
            sect = next(filter(lambda x: x.path == path, get_process_maps()))
            argv.append("--file")
            argv.append(path)
            argv.append("-I")
            argv.append("{:#x}".format(sect.page_start))

        import readline
        # ropper set up own autocompleter after which gdb/gef autocomplete don't work
        old_completer_delims = readline.get_completer_delims()
        old_completer = readline.get_completer()
        try:
            ropper.start(argv)
        except RuntimeWarning:
            return
        readline.set_completer(old_completer)
        readline.set_completer_delims(old_completer_delims)
        return


@register_command
class AssembleCommand(GenericCommand):
    """Inline code assemble. Architecture can be set in GEF runtime config. """

    _cmdline_ = "assemble"
    _syntax_  = "{:s} [-h] [--list-archs] [--mode MODE] [--arch ARCH] [--overwrite-location LOCATION] [--endian ENDIAN] [--as-shellcode] instruction;[instruction;...instruction;])".format(_cmdline_)
    _aliases_ = ["asm",]
    _example_ = "\n{0:s} -a x86 -m 32 nop ; nop ; inc eax ; int3\n{0:s} -a arm -m arm add r0, r0, 1".format(_cmdline_)

    valid_arch_modes = {
            # Format: ARCH = [MODES] with MODE = (NAME, HAS_LITTLE_ENDIAN, HAS_BIG_ENDIAN)
            "ARM":     [("ARM",     True,  True),  ("THUMB",   True,  True),
                        ("ARMV8",   True,  True),  ("THUMBV8", True,  True)],
            "ARM64":   [("0", True,  False)],
            "MIPS":    [("MIPS32",  True,  True),  ("MIPS64",  True,  True)],
            "PPC":     [("PPC32",   False, True),  ("PPC64",   True,  True)],
            "SPARC":   [("SPARC32", True,  True),  ("SPARC64", False, True)],
            "SYSTEMZ": [("SYSTEMZ", True,  True)],
            "X86":     [("16",      True,  False), ("32",      True,  False),
                        ("64",      True,  False)]
        }
    valid_archs = valid_arch_modes.keys()
    valid_modes = [_ for sublist in valid_arch_modes.values() for _ in sublist]

    def __init__(self):
        super().__init__()
        self["default_architecture"] = ( "X86", "Specify the default architecture to use when assembling")
        self["default_mode"] = ( "64", "Specify the default architecture to use when assembling")
        return

    def pre_load(self):
        try:
            __import__("keystone")
        except ImportError:
            msg = "Missing `keystone-engine` package for Python, install with: `pip install keystone-engine`."
            raise ImportWarning(msg)
        return

    def usage(self):
        super().usage()
        gef_print("")
        self.list_archs()
        return

    def list_archs(self):
        gef_print("Available architectures/modes (with endianness):")
        # for updates, see https://github.com/keystone-engine/keystone/blob/master/include/keystone/keystone.h
        for arch in self.valid_arch_modes:
            gef_print("- {}".format(arch))
            for mode, le, be in self.valid_arch_modes[arch]:
                if le and be:
                    endianness = "little, big"
                elif le:
                    endianness = "little"
                elif be:
                    endianness = "big"
                gef_print("  * {:<7} ({})".format(mode, endianness))
        return

    @parse_arguments({"instructions": [""]}, {"--mode": "", "--arch": "", "--overwrite-location": 0, "--endian": "little", "--list-archs": True, "--as-shellcode": True})
    def do_invoke(self, argv, *args, **kwargs):
        arch_s, mode_s, endian_s = self["default_architecture"], self["default_mode"], ""

        args = kwargs["arguments"]
        if args.list_archs:
            self.list_archs()
            return

        if not args.instructions:
            err("No instruction given.")
            return

        if is_alive():
            arch_s, mode_s = gef.arch.arch, gef.arch.mode
            endian_s = "big" if is_big_endian() else ""

        if args.arch:
            arch_s = args.arch
        arch_s = arch_s.upper()

        if args.mode:
            mode_s = args.mode
        mode_s = mode_s.upper()

        if args.endian == "big":
            endian_s = "big"
        endian_s = endian_s.upper()

        if arch_s not in self.valid_arch_modes:
            raise AttributeError("invalid arch '{}'".format(arch_s))

        valid_modes = self.valid_arch_modes[arch_s]
        try:
            mode_idx = [m[0] for m in valid_modes].index(mode_s)
        except ValueError:
            raise AttributeError("invalid mode '{}' for arch '{}'".format(mode_s, arch_s))

        if endian_s == "little" and not valid_modes[mode_idx][1] or endian_s == "big" and not valid_modes[mode_idx][2]:
            raise AttributeError("invalid endianness '{}' for arch/mode '{}:{}'".format(endian_s, arch_s, mode_s))

        arch, mode = get_keystone_arch(arch=arch_s, mode=mode_s, endian=endian_s)
        insns = [x.strip() for x in " ".join(args.instructions).split(";") if x]
        info("Assembling {} instruction(s) for {}:{}".format(len(insns), arch_s, mode_s))

        if args.as_shellcode:
            gef_print("""sc="" """)

        raw = b""
        for insn in insns:
            res = keystone_assemble(insn, arch, mode, raw=True)
            if res is None:
                gef_print("(Invalid)")
                continue

            if args.overwrite_location:
                raw += res
                continue

            s = binascii.hexlify(res)
            res = b"\\x" + b"\\x".join([s[i:i + 2] for i in range(0, len(s), 2)])
            res = res.decode("utf-8")

            if args.as_shellcode:
                res = """sc+="{0:s}" """.format(res)

            gef_print("{0:60s} # {1}".format(res, insn))

        if args.overwrite_location:
            l = len(raw)
            info("Overwriting {:d} bytes at {:s}".format(l, format_address(args.overwrite_location)))
            gef.memory.write(args.overwrite_location, raw, l)
        return


@register_command
class ProcessListingCommand(GenericCommand):
    """List and filter process. If a PATTERN is given as argument, results shown will be grepped
    by this pattern."""

    _cmdline_ = "process-search"
    _syntax_  = "{:s} [-h] [--attach] [--smart-scan] [REGEX_PATTERN]".format(_cmdline_)
    _aliases_ = ["ps"]
    _example_ = "{:s} gdb.*".format(_cmdline_)

    def __init__(self):
        super().__init__(complete=gdb.COMPLETE_LOCATION)
        ps = which("ps")
        self["ps_command"] = ( "{:s} auxww".format(ps), "`ps` command to get process information")
        return

    @parse_arguments({"pattern": ""}, {"--attach": True, "--smart-scan": True})
    def do_invoke(self, argv, *args, **kwargs):
        args = kwargs["arguments"]
        do_attach = args.attach
        smart_scan = args.smart_scan
        pattern = args.pattern
        pattern = re.compile("^.*$") if not args else re.compile(pattern)

        for process in self.get_processes():
            pid = int(process["pid"])
            command = process["command"]

            if not re.search(pattern, command):
                continue

            if smart_scan:
                if command.startswith("[") and command.endswith("]"): continue
                if command.startswith("socat "): continue
                if command.startswith("grep "): continue
                if command.startswith("gdb "): continue

            if args and do_attach:
                ok("Attaching to process='{:s}' pid={:d}".format(process["command"], pid))
                gdb.execute("attach {:d}".format(pid))
                return None

            line = [process[i] for i in ("pid", "user", "cpu", "mem", "tty", "command")]
            gef_print("\t\t".join(line))

        return None

    def get_processes(self):
        output = gef_execute_external(self["ps_command"].split(), True)
        names = [x.lower().replace("%", "") for x in output[0].split()]

        for line in output[1:]:
            fields = line.split()
            t = {}

            for i, name in enumerate(names):
                if i == len(names) - 1:
                    t[name] = " ".join(fields[i:])
                else:
                    t[name] = fields[i]

            yield t

        return


@register_command
class ElfInfoCommand(GenericCommand):
    """Display a limited subset of ELF header information. If no argument is provided, the command will
    show information about the current ELF being debugged."""

    _cmdline_ = "elf-info"
    _syntax_  = "{:s} [FILE]".format(_cmdline_)
    _example_  = "{:s} /bin/ls".format(_cmdline_)

    def __init__(self, *args, **kwargs):
        super().__init__(complete=gdb.COMPLETE_LOCATION)
        return

    @parse_arguments({}, {"--filename": ""})
    def do_invoke(self, argv, *args, **kwargs):
        args = kwargs["arguments"]

        if is_qemu_system():
            err("Unsupported")
            return

        # http://www.sco.com/developers/gabi/latest/ch4.eheader.html
        classes = {
            Elf.ELF_32_BITS     : "32-bit",
            Elf.ELF_64_BITS     : "64-bit",
        }

        endianness = {
            Elf.LITTLE_ENDIAN   : "Little-Endian",
            Elf.BIG_ENDIAN      : "Big-Endian",
        }

        osabi = {
            Elf.OSABI_SYSTEMV      : "System V",
            Elf.OSABI_HPUX         : "HP-UX",
            Elf.OSABI_NETBSD       : "NetBSD",
            Elf.OSABI_LINUX        : "Linux",
            Elf.OSABI_SOLARIS      : "Solaris",
            Elf.OSABI_AIX          : "AIX",
            Elf.OSABI_IRIX         : "IRIX",
            Elf.OSABI_FREEBSD      : "FreeBSD",
            Elf.OSABI_OPENBSD      : "OpenBSD",
        }

        types = {
            Elf.ET_RELOC           : "Relocatable",
            Elf.ET_EXEC            : "Executable",
            Elf.ET_DYN             : "Shared",
            Elf.ET_CORE            : "Core"
        }

        machines = {
            Elf.X86_64            : "x86-64",
            Elf.X86_32            : "x86",
            Elf.ARM               : "ARM",
            Elf.MIPS              : "MIPS",
            Elf.POWERPC           : "PowerPC",
            Elf.POWERPC64         : "PowerPC64",
            Elf.SPARC             : "SPARC",
            Elf.SPARC64           : "SPARC64",
            Elf.AARCH64           : "AArch64",
            Elf.RISCV             : "RISC-V",
            Elf.IA64              : "IA-64",
        }

        filename = args.filename or get_filepath()
        if filename is None:
            return

        elf = get_elf_headers(filename)
        if elf is None:
            return

        data = [
            ("Magic", "{0!s}".format(hexdump(struct.pack(">I", elf.e_magic), show_raw=True))),
            ("Class", "{0:#x} - {1}".format(elf.e_class, classes[elf.e_class])),
            ("Endianness", "{0:#x} - {1}".format(elf.e_endianness, endianness[elf.e_endianness])),
            ("Version", "{:#x}".format(elf.e_eiversion)),
            ("OS ABI", "{0:#x} - {1}".format(elf.e_osabi, osabi[elf.e_osabi])),
            ("ABI Version", "{:#x}".format(elf.e_abiversion)),
            ("Type", "{0:#x} - {1}".format(elf.e_type, types[elf.e_type])),
            ("Machine", "{0:#x} - {1}".format(elf.e_machine, machines[elf.e_machine])),
            ("Program Header Table", "{}".format(format_address(elf.e_phoff))),
            ("Section Header Table", "{}".format(format_address(elf.e_shoff))),
            ("Header Table", "{}".format(format_address(elf.e_phoff))),
            ("ELF Version", "{:#x}".format(elf.e_version)),
            ("Header size", "{0} ({0:#x})".format(elf.e_ehsize)),
            ("Entry point", "{}".format(format_address(elf.e_entry))),
        ]

        for title, content in data:
            gef_print("{}: {}".format(Color.boldify("{:<22}".format(title)), content))

        ptype = {
            Phdr.PT_NULL:         "NULL",
            Phdr.PT_LOAD:         "LOAD",
            Phdr.PT_DYNAMIC:      "DYNAMIC",
            Phdr.PT_INTERP:       "INTERP",
            Phdr.PT_NOTE:         "NOTE",
            Phdr.PT_SHLIB:        "SHLIB",
            Phdr.PT_PHDR:         "PHDR",
            Phdr.PT_TLS:          "TLS",
            Phdr.PT_LOOS:         "LOOS",
            Phdr.PT_GNU_EH_FRAME: "GNU_EH_FLAME",
            Phdr.PT_GNU_STACK:    "GNU_STACK",
            Phdr.PT_GNU_RELRO:    "GNU_RELRO",
            Phdr.PT_LOSUNW:       "LOSUNW",
            Phdr.PT_SUNWBSS:      "SUNWBSS",
            Phdr.PT_SUNWSTACK:    "SUNWSTACK",
            Phdr.PT_HISUNW:       "HISUNW",
            Phdr.PT_HIOS:         "HIOS",
            Phdr.PT_LOPROC:       "LOPROC",
            Phdr.PT_HIPROC:       "HIPROC",
        }

        pflags = {
            0:                             Permission.NONE,
            Phdr.PF_X:                     Permission.EXECUTE,
            Phdr.PF_W:                     Permission.WRITE,
            Phdr.PF_R:                     Permission.READ,
            Phdr.PF_W|Phdr.PF_X:           Permission.WRITE|Permission.EXECUTE,
            Phdr.PF_R|Phdr.PF_X:           Permission.READ|Permission.EXECUTE,
            Phdr.PF_R|Phdr.PF_W:           Permission.READ|Permission.WRITE,
            Phdr.PF_R|Phdr.PF_W|Phdr.PF_X: Permission.ALL,
        }

        gef_print("")
        gef_print(titlify("Program Header"))

        gef_print("  [{:>2s}] {:12s} {:>8s} {:>10s} {:>10s} {:>8s} {:>8s} {:5s} {:>8s}".format(
            "#", "Type", "Offset", "Virtaddr", "Physaddr", "FileSiz", "MemSiz", "Flags", "Align"))

        for i, p in enumerate(elf.phdrs):
            p_type = ptype[p.p_type] if p.p_type in ptype else "UNKNOWN"
            p_flags = Permission(value=pflags[p.p_flags]) if p.p_flags in pflags else "???"

            gef_print("  [{:2d}] {:12s} {:#8x} {:#10x} {:#10x} {:#8x} {:#8x} {:5s} {:#8x}".format(
                i, p_type, p.p_offset, p.p_vaddr, p.p_paddr, p.p_filesz, p.p_memsz, str(p_flags), p.p_align))

        stype = {
            Shdr.SHT_NULL:          "NULL",
            Shdr.SHT_PROGBITS:      "PROGBITS",
            Shdr.SHT_SYMTAB:        "SYMTAB",
            Shdr.SHT_STRTAB:        "STRTAB",
            Shdr.SHT_RELA:          "RELA",
            Shdr.SHT_HASH:          "HASH",
            Shdr.SHT_DYNAMIC:       "DYNAMIC",
            Shdr.SHT_NOTE:          "NOTE",
            Shdr.SHT_NOBITS:        "NOBITS",
            Shdr.SHT_REL:           "REL",
            Shdr.SHT_SHLIB:         "SHLIB",
            Shdr.SHT_DYNSYM:        "DYNSYM",
            Shdr.SHT_NUM:           "NUM",
            Shdr.SHT_INIT_ARRAY:    "INIT_ARRAY",
            Shdr.SHT_FINI_ARRAY:    "FINI_ARRAY",
            Shdr.SHT_PREINIT_ARRAY: "PREINIT_ARRAY",
            Shdr.SHT_GROUP:         "GROUP",
            Shdr.SHT_SYMTAB_SHNDX:  "SYMTAB_SHNDX",
            Shdr.SHT_LOOS:          "LOOS",
            Shdr.SHT_GNU_ATTRIBUTES:"GNU_ATTRIBUTES",
            Shdr.SHT_GNU_HASH:      "GNU_HASH",
            Shdr.SHT_GNU_LIBLIST:   "GNU_LIBLIST",
            Shdr.SHT_CHECKSUM:      "CHECKSUM",
            Shdr.SHT_LOSUNW:        "LOSUNW",
            Shdr.SHT_SUNW_move:     "SUNW_move",
            Shdr.SHT_SUNW_COMDAT:   "SUNW_COMDAT",
            Shdr.SHT_SUNW_syminfo:  "SUNW_syminfo",
            Shdr.SHT_GNU_verdef:    "GNU_verdef",
            Shdr.SHT_GNU_verneed:   "GNU_verneed",
            Shdr.SHT_GNU_versym:    "GNU_versym",
            Shdr.SHT_HISUNW:        "HISUNW",
            Shdr.SHT_HIOS:          "HIOS",
            Shdr.SHT_LOPROC:        "LOPROC",
            Shdr.SHT_HIPROC:        "HIPROC",
            Shdr.SHT_LOUSER:        "LOUSER",
            Shdr.SHT_HIUSER:        "HIUSER",
        }

        gef_print("")
        gef_print(titlify("Section Header"))
        gef_print("  [{:>2s}] {:20s} {:>15s} {:>10s} {:>8s} {:>8s} {:>8s} {:5s} {:4s} {:4s} {:>8s}".format(
            "#", "Name", "Type", "Address", "Offset", "Size", "EntSiz", "Flags", "Link", "Info", "Align"))

        for i, s in enumerate(elf.shdrs):
            sh_type = stype[s.sh_type] if s.sh_type in stype else "UNKNOWN"
            sh_flags = ""
            if s.sh_flags & Shdr.SHF_WRITE:            sh_flags += "W"
            if s.sh_flags & Shdr.SHF_ALLOC:            sh_flags += "A"
            if s.sh_flags & Shdr.SHF_EXECINSTR:        sh_flags += "X"
            if s.sh_flags & Shdr.SHF_MERGE:            sh_flags += "M"
            if s.sh_flags & Shdr.SHF_STRINGS:          sh_flags += "S"
            if s.sh_flags & Shdr.SHF_INFO_LINK:        sh_flags += "I"
            if s.sh_flags & Shdr.SHF_LINK_ORDER:       sh_flags += "L"
            if s.sh_flags & Shdr.SHF_OS_NONCONFORMING: sh_flags += "O"
            if s.sh_flags & Shdr.SHF_GROUP:            sh_flags += "G"
            if s.sh_flags & Shdr.SHF_TLS:              sh_flags += "T"
            if s.sh_flags & Shdr.SHF_EXCLUDE:          sh_flags += "E"
            if s.sh_flags & Shdr.SHF_COMPRESSED:       sh_flags += "C"

            gef_print("  [{:2d}] {:20s} {:>15s} {:#10x} {:#8x} {:#8x} {:#8x} {:5s} {:#4x} {:#4x} {:#8x}".format(
                i, s.sh_name, sh_type, s.sh_addr, s.sh_offset, s.sh_size, s.sh_entsize, sh_flags, s.sh_link, s.sh_info, s.sh_addralign))
        return


@register_command
class EntryPointBreakCommand(GenericCommand):
    """Tries to find best entry point and sets a temporary breakpoint on it. The command will test for
    well-known symbols for entry points, such as `main`, `_main`, `__libc_start_main`, etc. defined by
    the setting `entrypoint_symbols`."""

    _cmdline_ = "entry-break"
    _syntax_  = _cmdline_
    _aliases_ = ["start",]

    def __init__(self, *args, **kwargs):
        super().__init__()
        self["entrypoint_symbols"] = ( "main _main __libc_start_main __uClibc_main start _start", "Possible symbols for entry points")
        return

    def do_invoke(self, argv):
        fpath = get_filepath()
        if fpath is None:
            warn("No executable to debug, use `file` to load a binary")
            return

        if not os.access(fpath, os.X_OK):
            warn("The file '{}' is not executable.".format(fpath))
            return

        if is_alive() and not __gef_qemu_mode__:
            warn("gdb is already running")
            return

        bp = None
        entrypoints = self["entrypoint_symbols"].split()

        for sym in entrypoints:
            try:
                value = parse_address(sym)
                info("Breaking at '{:s}'".format(str(value)))
                bp = EntryBreakBreakpoint(sym)
                gdb.execute("run {}".format(" ".join(argv)))
                return

            except gdb.error as gdb_error:
                if 'The "remote" target does not support "run".' in str(gdb_error):
                    # this case can happen when doing remote debugging
                    gdb.execute("continue")
                    return
                continue

        # if here, clear the breakpoint if any set
        if bp:
            bp.delete()

        # break at entry point
        entry = get_entry_point()
        if entry is None:
            return

        if is_pie(fpath):
            self.set_init_tbreak_pie(entry, argv)
            gdb.execute("continue")
            return

        self.set_init_tbreak(entry)
        gdb.execute("run {}".format(" ".join(argv)))
        return

    def set_init_tbreak(self, addr):
        info("Breaking at entry-point: {:#x}".format(addr))
        bp = EntryBreakBreakpoint("*{:#x}".format(addr))
        return bp

    def set_init_tbreak_pie(self, addr, argv):
        warn("PIC binary detected, retrieving text base address")
        gdb.execute("set stop-on-solib-events 1")
        hide_context()
        gdb.execute("run {}".format(" ".join(argv)))
        unhide_context()
        gdb.execute("set stop-on-solib-events 0")
        vmmap = get_process_maps()
        base_address = [x.page_start for x in vmmap if x.path == get_filepath()][0]
        return self.set_init_tbreak(base_address + addr)


@register_command
class NamedBreakpointCommand(GenericCommand):
    """Sets a breakpoint and assigns a name to it, which will be shown, when it's hit."""

    _cmdline_ = "name-break"
    _syntax_  = "{:s} name [address]".format(_cmdline_)
    _aliases_ = ["nb",]
    _example  = "{:s} main *0x4008a9"

    def __init__(self, *args, **kwargs):
        super().__init__()
        return

    @parse_arguments({"name": "", "address": "*$pc"}, {})
    def do_invoke(self, *args, **kwargs):
        args = kwargs["arguments"]
        if not args.name:
            err("Missing name for breakpoint")
            self.usage()
            return

        NamedBreakpoint(args.address, args.name)
        return


@register_command
class ContextCommand(GenericCommand):
    """Displays a comprehensive and modular summary of runtime context. Unless setting `enable` is
    set to False, this command will be spawned automatically every time GDB hits a breakpoint, a
    watchpoint, or any kind of interrupt. By default, it will show panes that contain the register
    states, the stack, and the disassembly code around $pc."""

    _cmdline_ = "context"
    _syntax_  = "{:s} [legend|regs|stack|code|args|memory|source|trace|threads|extra]".format(_cmdline_)
    _aliases_ = ["ctx",]

    old_registers = {}

    def __init__(self):
        super().__init__()
        self["enable"] = ( True, "Enable/disable printing the context when breaking")
        self["show_source_code_variable_values"] = ( True, "Show extra PC context info in the source code")
        self["show_stack_raw"] = ( False, "Show the stack pane as raw hexdump (no dereference)")
        self["show_registers_raw"] = ( False, "Show the registers pane with raw values (no dereference)")
        self["show_opcodes_size"] = ( 0, "Number of bytes of opcodes to display next to the disassembly")
        self["peek_calls"] = ( True, "Peek into calls")
        self["peek_ret"] = ( True, "Peek at return address")
        self["nb_lines_stack"] = ( 8, "Number of line in the stack pane")
        self["grow_stack_down"] = ( False, "Order of stack downward starts at largest down to stack pointer")
        self["nb_lines_backtrace"] = ( 10, "Number of line in the backtrace pane")
        self["nb_lines_backtrace_before"] = ( 2, "Number of line in the backtrace pane before selected frame")
        self["nb_lines_threads"] = ( -1, "Number of line in the threads pane")
        self["nb_lines_code"] = ( 6, "Number of instruction after $pc")
        self["nb_lines_code_prev"] = ( 3, "Number of instruction before $pc")
        self["ignore_registers"] = ( "", "Space-separated list of registers not to display (e.g. '$cs $ds $gs')")
        self["clear_screen"] = ( True, "Clear the screen before printing the context")
        self["layout"] = ( "legend regs stack code args source memory threads trace extra", "Change the order/presence of the context sections")
        self["redirect"] = ("", "Redirect the context information to another TTY")
        self["libc_args"] = ( False, "Show libc function call args description")
        self["libc_args_path"] = ( "", "Path to libc function call args json files, provided via gef-extras")

        if "capstone" in list(sys.modules.keys()):
            self["use_capstone"] = ( False, "Use capstone as disassembler in the code pane (instead of GDB)")

        self.layout_mapping = {
            "legend": (self.show_legend, None),
            "regs": (self.context_regs, None),
            "stack": (self.context_stack, None),
            "code": (self.context_code, None),
            "args": (self.context_args, None),
            "memory": (self.context_memory, None),
            "source": (self.context_source, None),
            "trace": (self.context_trace, None),
            "threads": (self.context_threads, None),
            "extra": (self.context_additional_information, None),
        }
        return

    def post_load(self):
        gef_on_continue_hook(self.update_registers)
        gef_on_continue_hook(self.empty_extra_messages)
        return

    def show_legend(self):
        if gef.config["gef.disable_color"] is True:
            return
        str_color = gef.config["theme.dereference_string"]
        code_addr_color = gef.config["theme.address_code"]
        stack_addr_color = gef.config["theme.address_stack"]
        heap_addr_color = gef.config["theme.address_heap"]
        changed_register_color = gef.config["theme.registers_value_changed"]

        gef_print("[ Legend: {} | {} | {} | {} | {} ]".format(Color.colorify("Modified register", changed_register_color),
                                                              Color.colorify("Code", code_addr_color),
                                                              Color.colorify("Heap", heap_addr_color),
                                                              Color.colorify("Stack", stack_addr_color),
                                                              Color.colorify("String", str_color)
        ))
        return

    @only_if_gdb_running
    def do_invoke(self, argv):
        if not self["enable"] or context_hidden:
            return

        if not all(_ in self.layout_mapping for _ in argv):
            self.usage()
            return

        if len(argv) > 0:
            current_layout = argv
        else:
            current_layout = self["layout"].strip().split()

        if not current_layout:
            return

        self.tty_rows, self.tty_columns = get_terminal_size()

        redirect = self["redirect"]
        if redirect and os.access(redirect, os.W_OK):
            enable_redirect_output(to_file=redirect)

        for section in current_layout:
            if section[0] == "-":
                continue

            try:
                display_pane_function, pane_title_function = self.layout_mapping[section]
                if pane_title_function:
                    self.context_title(pane_title_function())
                display_pane_function()
            except gdb.MemoryError as e:
                # a MemoryError will happen when $pc is corrupted (invalid address)
                err(str(e))

        self.context_title("")

        if self["clear_screen"] and len(argv) == 0:
            clear_screen(redirect)

        if redirect and os.access(redirect, os.W_OK):
            disable_redirect_output()
        return

    def context_title(self, m):
        # allow for not displaying a title line
        if m is None:
            return

        line_color = gef.config["theme.context_title_line"]
        msg_color = gef.config["theme.context_title_message"]

        # print an empty line in case of ""
        if not m:
            gef_print(Color.colorify(HORIZONTAL_LINE * self.tty_columns, line_color))
            return

        trail_len = len(m) + 6
        title = ""
        title += Color.colorify("{:{padd}<{width}} ".format("",
                                                            width=max(self.tty_columns - trail_len, 0),
                                                            padd=HORIZONTAL_LINE),
                                line_color)
        title += Color.colorify(m, msg_color)
        title += Color.colorify(" {:{padd}<4}".format("", padd=HORIZONTAL_LINE),
                                line_color)
        gef_print(title)
        return

    def context_regs(self):
        self.context_title("registers")
        ignored_registers = set(self["ignore_registers"].split())

        if self["show_registers_raw"] is False:
            regs = set(gef.arch.all_registers)
            printable_registers = " ".join(list(regs - ignored_registers))
            gdb.execute("registers {}".format(printable_registers))
            return

        widest = l = max(map(len, gef.arch.all_registers))
        l += 5
        l += gef.arch.ptrsize * 2
        nb = get_terminal_size()[1] // l
        i = 1
        line = ""
        changed_color = gef.config["theme.registers_value_changed"]
        regname_color = gef.config["theme.registers_register_name"]

        for reg in gef.arch.all_registers:
            if reg in ignored_registers:
                continue

            try:
                r = gdb.parse_and_eval(reg)
                if r.type.code == gdb.TYPE_CODE_VOID:
                    continue

                new_value_type_flag = r.type.code == gdb.TYPE_CODE_FLAGS
                new_value = int(r)

            except (gdb.MemoryError, gdb.error):
                # If this exception is triggered, it means that the current register
                # is corrupted. Just use the register "raw" value (not eval-ed)
                new_value = get_register(reg)
                new_value_type_flag = False

            except Exception:
                new_value = 0
                new_value_type_flag = False

            old_value = self.old_registers.get(reg, 0)

            padreg = reg.ljust(widest, " ")
            value = align_address(new_value)
            old_value = align_address(old_value)
            if value == old_value:
                line += "{}: ".format(Color.colorify(padreg, regname_color))
            else:
                line += "{}: ".format(Color.colorify(padreg, changed_color))
            if new_value_type_flag:
                line += "{:s} ".format(format_address_spaces(value))
            else:
                addr = lookup_address(align_address(int(value)))
                if addr.valid:
                    line += "{:s} ".format(str(addr))
                else:
                    line += "{:s} ".format(format_address_spaces(value))

            if i % nb == 0:
                gef_print(line)
                line = ""
            i += 1

        if line:
            gef_print(line)

        gef_print("Flags: {:s}".format(gef.arch.flag_register_to_human()))
        return

    def context_stack(self):
        self.context_title("stack")

        show_raw = self["show_stack_raw"]
        nb_lines = self["nb_lines_stack"]

        try:
            sp = gef.arch.sp
            if show_raw is True:
                mem = gef.memory.read(sp, 0x10 * nb_lines)
                gef_print(hexdump(mem, base=sp))
            else:
                gdb.execute("dereference -l {:d} {:#x}".format(nb_lines, sp))

        except gdb.MemoryError:
            err("Cannot read memory from $SP (corrupted stack pointer?)")

        return

    def addr_has_breakpoint(self, address, bp_locations):
        return any(hex(address) in b for b in bp_locations)

    def context_code(self):
        nb_insn = self["nb_lines_code"]
        nb_insn_prev = self["nb_lines_code_prev"]
        use_capstone = "use_capstone" in self and self["use_capstone"]
        show_opcodes_size = "show_opcodes_size" in self and self["show_opcodes_size"]
        past_insns_color = gef.config["theme.old_context"]
        cur_insn_color = gef.config["theme.disassemble_current_instruction"]
        pc = gef.arch.pc
        breakpoints = gdb.breakpoints() or []
        bp_locations = [b.location for b in breakpoints if b.location and b.location.startswith("*")]

        frame = gdb.selected_frame()
        arch_name = "{}:{}".format(gef.arch.arch.lower(), gef.arch.mode)

        self.context_title("code:{}".format(arch_name))

        try:
            instruction_iterator = capstone_disassemble if use_capstone else gef_disassemble

            for insn in instruction_iterator(pc, nb_insn, nb_prev=nb_insn_prev):
                line = []
                is_taken  = False
                target    = None
                bp_prefix = Color.redify(BP_GLYPH) if self.addr_has_breakpoint(insn.address, bp_locations) else " "

                if show_opcodes_size == 0:
                    text = str(insn)
                else:
                    insn_fmt = "{{:{}o}}".format(show_opcodes_size)
                    text = insn_fmt.format(insn)

                if insn.address < pc:
                    line += "{}  {}".format(bp_prefix, Color.colorify(text, past_insns_color))

                elif insn.address == pc:
                    line += "{}{}".format(bp_prefix, Color.colorify("{:s}{:s}".format(RIGHT_ARROW[1:], text), cur_insn_color))

                    if gef.arch.is_conditional_branch(insn):
                        is_taken, reason = gef.arch.is_branch_taken(insn)
                        if is_taken:
                            target = insn.operands[-1].split()[0]
                            reason = "[Reason: {:s}]".format(reason) if reason else ""
                            line += Color.colorify("\tTAKEN {:s}".format(reason), "bold green")
                        else:
                            reason = "[Reason: !({:s})]".format(reason) if reason else ""
                            line += Color.colorify("\tNOT taken {:s}".format(reason), "bold red")
                    elif gef.arch.is_call(insn) and self["peek_calls"] is True:
                        target = insn.operands[-1].split()[0]
                    elif gef.arch.is_ret(insn) and self["peek_ret"] is True:
                        target = gef.arch.get_ra(insn, frame)

                else:
                    line += "{}  {}".format(bp_prefix, text)

                gef_print("".join(line))

                if target:
                    try:
                        target = int(target, 0)
                    except TypeError:  # Already an int
                        pass
                    except ValueError:
                        # If the operand isn't an address right now we can't parse it
                        continue
                    for i, tinsn in enumerate(instruction_iterator(target, nb_insn)):
                        text= "   {}  {}".format (DOWN_ARROW if i == 0 else " ", str(tinsn))
                        gef_print(text)
                    break

        except gdb.MemoryError:
            err("Cannot disassemble from $PC")
        return

    def context_args(self):
        insn = gef_current_instruction(gef.arch.pc)
        if not gef.arch.is_call(insn):
            return

        self.size2type = {
            1: "BYTE",
            2: "WORD",
            4: "DWORD",
            8: "QWORD",
        }

        if insn.operands[-1].startswith(self.size2type[gef.arch.ptrsize]+" PTR"):
            target = "*" + insn.operands[-1].split()[-1]
        elif "$"+insn.operands[0] in gef.arch.all_registers:
            target = "*{:#x}".format(get_register("$"+insn.operands[0]))
        else:
            # is there a symbol?
            ops = " ".join(insn.operands)
            if "<" in ops and ">" in ops:
                # extract it
                target = re.sub(r".*<([^\(> ]*).*", r"\1", ops)
            else:
                # it's an address, just use as is
                target = re.sub(r".*(0x[a-fA-F0-9]*).*", r"\1", ops)

        sym = gdb.lookup_global_symbol(target)
        if sym is None:
            self.print_guessed_arguments(target)
            return

        if sym.type.code != gdb.TYPE_CODE_FUNC:
            err("Symbol '{}' is not a function: type={}".format(target, sym.type.code))
            return

        self.print_arguments_from_symbol(target, sym)
        return

    def print_arguments_from_symbol(self, function_name, symbol):
        """If symbols were found, parse them and print the argument adequately."""
        args = []

        for i, f in enumerate(symbol.type.fields()):
            _value = gef.arch.get_ith_parameter(i, in_func=False)[1]
            _value = RIGHT_ARROW.join(dereference_from(_value))
            _name = f.name or "var_{}".format(i)
            _type = f.type.name or self.size2type[f.type.sizeof]
            args.append("{} {} = {}".format(_type, _name, _value))

        self.context_title("arguments")

        if not args:
            gef_print("{} (<void>)".format(function_name))
            return

        gef_print("{} (".format(function_name))
        gef_print("   " + ",\n   ".join(args))
        gef_print(")")
        return

    def print_guessed_arguments(self, function_name):
        """When no symbol, read the current basic block and look for "interesting" instructions."""

        def __get_current_block_start_address():
            pc = gef.arch.pc
            try:
                block = gdb.block_for_pc(pc)
                block_start = block.start if block else gdb_get_nth_previous_instruction_address(pc, 5)
            except RuntimeError:
                block_start = gdb_get_nth_previous_instruction_address(pc, 5)
            return block_start

        parameter_set = set()
        pc = gef.arch.pc
        block_start = __get_current_block_start_address()
        if not block_start:
            return
        use_capstone = "use_capstone" in self and self["use_capstone"]
        instruction_iterator = capstone_disassemble if use_capstone else gef_disassemble
        function_parameters = gef.arch.function_parameters
        arg_key_color = gef.config["theme.registers_register_name"]

        for insn in instruction_iterator(block_start, pc - block_start):
            if not insn.operands:
                continue

            if is_x86_32():
                if insn.mnemonic == "push":
                    parameter_set.add(insn.operands[0])
            else:
                op = "$" + insn.operands[0]
                if op in function_parameters:
                    parameter_set.add(op)

                if is_x86_64():
                    # also consider extended registers
                    extended_registers = {"$rdi": ["$edi", "$di"],
                                          "$rsi": ["$esi", "$si"],
                                          "$rdx": ["$edx", "$dx"],
                                          "$rcx": ["$ecx", "$cx"],
                                         }
                    for exreg in extended_registers:
                        if op in extended_registers[exreg]:
                            parameter_set.add(exreg)

        nb_argument = None
        _arch_mode = "{}_{}".format(gef.arch.arch.lower(), gef.arch.mode)
        _function_name = None
        if function_name.endswith("@plt"):
            _function_name = function_name.split("@")[0]
            try:
                nb_argument = len(libc_args_definitions[_arch_mode][_function_name])
            except KeyError:
                pass

        if not nb_argument:
            if not parameter_set:
                nb_argument = 0
            elif is_x86_32():
                nb_argument = len(parameter_set)
            else:
                nb_argument = max(function_parameters.index(p)+1 for p in parameter_set)

        args = []
        for i in range(nb_argument):
            _key, _values = gef.arch.get_ith_parameter(i, in_func=False)
            _values = RIGHT_ARROW.join(dereference_from(_values))
            try:
                args.append("{} = {} (def: {})".format(Color.colorify(_key, arg_key_color), _values,
                                                       libc_args_definitions[_arch_mode][_function_name][_key]))
            except KeyError:
                args.append("{} = {}".format(Color.colorify(_key, arg_key_color), _values))

        self.context_title("arguments (guessed)")
        gef_print("{} (".format(function_name))
        if args:
            gef_print("   " + ",\n   ".join(args))
        gef_print(")")
        return

    def line_has_breakpoint(self, file_name, line_number, bp_locations):
        filename_line = "{}:{}".format(file_name, line_number)
        return any(filename_line in loc for loc in bp_locations)

    def context_source(self):
        try:
            pc = gef.arch.pc
            symtabline = gdb.find_pc_line(pc)
            symtab = symtabline.symtab
            # we subtract one because the line number returned by gdb start at 1
            line_num = symtabline.line - 1
            if not symtab.is_valid():
                return

            fpath = symtab.fullname()
            with open(fpath, "r") as f:
                lines = [l.rstrip() for l in f.readlines()]

        except Exception:
            return

        file_base_name = os.path.basename(symtab.filename)
        breakpoints = gdb.breakpoints() or []
        bp_locations = [b.location for b in breakpoints if b.location and file_base_name in b.location]
        past_lines_color = gef.config["theme.old_context"]

        nb_line = self["nb_lines_code"]
        fn = symtab.filename
        if len(fn) > 20:
            fn = "{}[...]{}".format(fn[:15], os.path.splitext(fn)[1])
        title = "source:{}+{}".format(fn, line_num + 1)
        cur_line_color = gef.config["theme.source_current_line"]
        self.context_title(title)
        show_extra_info = self["show_source_code_variable_values"]

        for i in range(line_num - nb_line + 1, line_num + nb_line):
            if i < 0:
                continue

            bp_prefix = Color.redify(BP_GLYPH) if self.line_has_breakpoint(file_base_name, i + 1, bp_locations) else " "

            if i < line_num:
                gef_print("{}{}".format(bp_prefix, Color.colorify("  {:4d}\t {:s}".format(i + 1, lines[i],), past_lines_color)))

            if i == line_num:
                prefix = "{}{}{:4d}\t ".format(bp_prefix, RIGHT_ARROW[1:], i + 1)
                leading = len(lines[i]) - len(lines[i].lstrip())
                if show_extra_info:
                    extra_info = self.get_pc_context_info(pc, lines[i])
                    if extra_info:
                        gef_print("{}{}".format(" "*(len(prefix) + leading), extra_info))
                gef_print(Color.colorify("{}{:s}".format(prefix, lines[i]), cur_line_color))

            if i > line_num:
                try:
                    gef_print("{}  {:4d}\t {:s}".format(bp_prefix, i + 1, lines[i],))
                except IndexError:
                    break
        return

    def get_pc_context_info(self, pc, line):
        try:
            current_block = gdb.block_for_pc(pc)
            if not current_block or not current_block.is_valid(): return ""
            m = collections.OrderedDict()
            while current_block and not current_block.is_static:
                for sym in current_block:
                    symbol = sym.name
                    if not sym.is_function and re.search(r"\W{}\W".format(symbol), line):
                        val = gdb.parse_and_eval(symbol)
                        if val.type.code in (gdb.TYPE_CODE_PTR, gdb.TYPE_CODE_ARRAY):
                            addr = int(val.address)
                            addrs = dereference_from(addr)
                            if len(addrs) > 2:
                                addrs = [addrs[0], "[...]", addrs[-1]]

                            f = " {:s} ".format(RIGHT_ARROW)
                            val = f.join(addrs)
                        elif val.type.code == gdb.TYPE_CODE_INT:
                            val = hex(int(val))
                        else:
                            continue

                        if symbol not in m:
                            m[symbol] = val
                current_block = current_block.superblock

            if m:
                return "// " + ", ".join(["{}={}".format(Color.yellowify(a), b) for a, b in m.items()])
        except Exception:
            pass
        return ""

    def context_trace(self):
        self.context_title("trace")

        nb_backtrace = self["nb_lines_backtrace"]
        if nb_backtrace <= 0:
            return

        # backward compat for gdb (gdb < 7.10)
        if not hasattr(gdb, "FrameDecorator"):
            gdb.execute("backtrace {:d}".format(nb_backtrace))
            return

        orig_frame = gdb.selected_frame()
        current_frame = gdb.newest_frame()
        frames = [current_frame]
        while current_frame != orig_frame:
            current_frame = current_frame.older()
            frames.append(current_frame)

        nb_backtrace_before = self["nb_lines_backtrace_before"]
        level = max(len(frames) - nb_backtrace_before - 1, 0)
        current_frame = frames[level]

        while current_frame:
            current_frame.select()
            if not current_frame.is_valid():
                continue

            pc = current_frame.pc()
            name = current_frame.name()
            items = []
            items.append("{:#x}".format(pc))
            if name:
                frame_args = gdb.FrameDecorator.FrameDecorator(current_frame).frame_args() or []
                m = "{}({})".format(Color.greenify(name),
                                    ", ".join(["{}={!s}".format(Color.yellowify(x.sym),
                                                                x.sym.value(current_frame)) for x in frame_args]))
                items.append(m)
            else:
                try:
                    insn = next(gef_disassemble(pc, 1))
                except gdb.MemoryError:
                    break

                # check if the gdb symbol table may know the address
                sym_found = gdb_get_location_from_symbol(pc)
                symbol = ""
                if sym_found:
                    sym_name, offset = sym_found
                    symbol = " <{}+{:x}> ".format(sym_name, offset)

                items.append(Color.redify("{}{} {}".format(symbol, insn.mnemonic, ", ".join(insn.operands))))

            gef_print("[{}] {}".format(Color.colorify("#{}".format(level), "bold green" if current_frame == orig_frame else "bold pink"),
                                       RIGHT_ARROW.join(items)))
            current_frame = current_frame.older()
            level += 1
            nb_backtrace -= 1
            if nb_backtrace == 0:
                break

        orig_frame.select()
        return

    def context_threads(self):
        def reason():
            res = gdb.execute("info program", to_string=True).splitlines()
            if not res:
                return "NOT RUNNING"

            for line in res:
                line = line.strip()
                if line.startswith("It stopped with signal "):
                    return line.replace("It stopped with signal ", "").split(",", 1)[0]
                if line == "The program being debugged is not being run.":
                    return "NOT RUNNING"
                if line == "It stopped at a breakpoint that has since been deleted.":
                    return "TEMPORARY BREAKPOINT"
                if line.startswith("It stopped at breakpoint "):
                    return "BREAKPOINT"
                if line == "It stopped after being stepped.":
                    return "SINGLE STEP"

            return "STOPPED"

        self.context_title("threads")

        threads = gdb.selected_inferior().threads()[::-1]
        idx = self["nb_lines_threads"]
        if idx > 0:
            threads = threads[0:idx]

        if idx == 0:
            return

        if not threads:
            err("No thread selected")
            return

        selected_thread = gdb.selected_thread()
        selected_frame = gdb.selected_frame()

        for i, thread in enumerate(threads):
            line = """[{:s}] Id {:d}, """.format(Color.colorify("#{:d}".format(i), "bold green" if thread == selected_thread  else "bold pink"), thread.num)
            if thread.name:
                line += """Name: "{:s}", """.format(thread.name)
            if thread.is_running():
                line += Color.colorify("running", "bold green")
            elif thread.is_stopped():
                line += Color.colorify("stopped", "bold red")
                thread.switch()
                frame = gdb.selected_frame()
                frame_name = frame.name()

                # check if the gdb symbol table may know the address
                if not frame_name:
                    sym_found = gdb_get_location_from_symbol(frame.pc())
                    if sym_found:
                        sym_name, offset = sym_found
                        frame_name = "<{}+{:x}>".format(sym_name, offset)

                line += " {:s} in {:s} ()".format(Color.colorify("{:#x}".format(frame.pc()), "blue"), Color.colorify(frame_name or "??", "bold yellow"))
                line += ", reason: {}".format(Color.colorify(reason(), "bold pink"))
            elif thread.is_exited():
                line += Color.colorify("exited", "bold yellow")
            gef_print(line)
            i += 1

        selected_thread.switch()
        selected_frame.select()
        return

    def context_additional_information(self):
        if not __context_messages__:
            return

        self.context_title("extra")
        for level, text in __context_messages__:
            if level == "error": err(text)
            elif level == "warn": warn(text)
            elif level == "success": ok(text)
            else: info(text)
        return

    def context_memory(self):
        global __watches__
        for address, opt in sorted(__watches__.items()):
            sz, fmt = opt[0:2]
            self.context_title("memory:{:#x}".format(address))
            if fmt == "pointers":
                gdb.execute("dereference -l {size:d} {address:#x}".format(
                    address=address,
                    size=sz,
                ))
            else:
                gdb.execute("hexdump {fmt:s} -s {size:d} {address:#x}".format(
                    address=address,
                    size=sz,
                    fmt=fmt,
                ))

    @classmethod
    def update_registers(cls, event):
        for reg in gef.arch.all_registers:
            try:
                cls.old_registers[reg] = get_register(reg)
            except Exception:
                cls.old_registers[reg] = 0
        return

    def empty_extra_messages(self, event):
        global __context_messages__
        __context_messages__ = []
        return


@register_command
class MemoryCommand(GenericCommand):
    """Add or remove address ranges to the memory view."""
    _cmdline_ = "memory"
    _syntax_  = "{:s} (watch|unwatch|reset|list)".format(_cmdline_)

    def __init__(self):
        super().__init__(prefix=True)
        return

    @only_if_gdb_running
    def do_invoke(self, argv):
        self.usage()
        return


@register_command
class MemoryWatchCommand(GenericCommand):
    """Adds address ranges to the memory view."""
    _cmdline_ = "memory watch"
    _syntax_  = "{:s} ADDRESS [SIZE] [(qword|dword|word|byte|pointers)]".format(_cmdline_)
    _example_ = "\n\t{0:s} 0x603000 0x100 byte\n\t{0:s} $sp".format(_cmdline_)

    def __init__(self):
        super().__init__(complete=gdb.COMPLETE_LOCATION)
        return

    @only_if_gdb_running
    def do_invoke(self, argv):
        global __watches__

        if len(argv) not in (1, 2, 3):
            self.usage()
            return

        address = parse_address(argv[0])
        size    = parse_address(argv[1]) if len(argv) > 1 else 0x10
        group   = "byte"

        if len(argv) == 3:
            group = argv[2].lower()
            if group not in ("qword", "dword", "word", "byte", "pointers"):
                warn("Unexpected grouping '{}'".format(group))
                self.usage()
                return
        else:
            if gef.arch.ptrsize == 4:
                group = "dword"
            elif gef.arch.ptrsize == 8:
                group = "qword"

        __watches__[address] = (size, group)
        ok("Adding memwatch to {:#x}".format(address))
        return


@register_command
class MemoryUnwatchCommand(GenericCommand):
    """Removes address ranges to the memory view."""
    _cmdline_ = "memory unwatch"
    _syntax_  = "{:s} ADDRESS".format(_cmdline_)
    _example_ = "\n\t{0:s} 0x603000\n\t{0:s} $sp".format(_cmdline_)

    def __init__(self):
        super().__init__(complete=gdb.COMPLETE_LOCATION)
        return

    @only_if_gdb_running
    def do_invoke(self, argv):
        global __watches__
        if not argv:
            self.usage()
            return

        address = parse_address(argv[0])
        res = __watches__.pop(address, None)
        if not res:
            warn("You weren't watching {:#x}".format(address))
        else:
            ok("Removed memwatch of {:#x}".format(address))
        return


@register_command
class MemoryWatchResetCommand(GenericCommand):
    """Removes all watchpoints."""
    _cmdline_ = "memory reset"
    _syntax_  = "{:s}".format(_cmdline_)

    @only_if_gdb_running
    def do_invoke(self, argv):
        global __watches__
        __watches__.clear()
        ok("Memory watches cleared")
        return


@register_command
class MemoryWatchListCommand(GenericCommand):
    """Lists all watchpoints to display in context layout."""
    _cmdline_ = "memory list"
    _syntax_  = "{:s}".format(_cmdline_)

    @only_if_gdb_running
    def do_invoke(self, argv):
        global __watches__

        if not __watches__:
            info("No memory watches")
            return

        info("Memory watches:")
        for address, opt in sorted(__watches__.items()):
            gef_print("- {:#x} ({}, {})".format(address, opt[0], opt[1]))
        return


@register_command
class HexdumpCommand(GenericCommand):
    """Display SIZE lines of hexdump from the memory location pointed by LOCATION."""

    _cmdline_ = "hexdump"
    _syntax_  = "{:s} (qword|dword|word|byte) [LOCATION] [--size SIZE] [--reverse]".format(_cmdline_)
    _example_ = "{:s} byte $rsp --size 16 --reverse".format(_cmdline_)

    def __init__(self):
        super().__init__(complete=gdb.COMPLETE_LOCATION, prefix=True)
        self["always_show_ascii"] = ( False, "If true, hexdump will always display the ASCII dump")
        self.format = None
        self.__last_target = "$sp"
        return

    @only_if_gdb_running
    @parse_arguments({"address": "",}, {("--reverse", "-r"): True, ("--size", "-s"): 0})
    def do_invoke(self, argv, *args, **kwargs):
        valid_formats = ["byte", "word", "dword", "qword"]
        if not self.format or self.format not in valid_formats:
            err("Invalid command")
            return

        args = kwargs["arguments"]
        target = args.address or self.__last_target
        start_addr = parse_address(target)
        read_from = align_address(start_addr)

        if self.format == "byte":
            read_len = args.size or 0x40
            read_from += self.repeat_count * read_len
            mem = gef.memory.read(read_from, read_len)
            lines = hexdump(mem, base=read_from).splitlines()
        else:
            read_len = args.size or 0x10
            lines = self._hexdump(read_from, read_len, self.format, self.repeat_count * read_len)

        if args.reverse:
            lines.reverse()

        self.__last_target = target
        gef_print("\n".join(lines))
        return

    def _hexdump(self, start_addr, length, arrange_as, offset=0):
        endianness = endian_str()

        base_address_color = gef.config["theme.dereference_base_address"]
        show_ascii = gef.config["hexdump.always_show_ascii"]

        formats = {
            "qword": ("Q", 8),
            "dword": ("I", 4),
            "word": ("H", 2),
        }

        r, l = formats[arrange_as]
        fmt_str = "{{base}}{v}+{{offset:#06x}}   {{sym}}{{val:#0{prec}x}}   {{text}}".format(v=VERTICAL_LINE, prec=l*2+2)
        fmt_pack = endianness + r
        lines = []

        i = 0
        text = ""
        while i < length:
            cur_addr = start_addr + (i + offset) * l
            sym = gdb_get_location_from_symbol(cur_addr)
            sym = "<{:s}+{:04x}> ".format(*sym) if sym else ""
            mem = gef.memory.read(cur_addr, l)
            val = struct.unpack(fmt_pack, mem)[0]
            if show_ascii:
                text = "".join([chr(b) if 0x20 <= b < 0x7F else "." for b in mem])
            lines.append(fmt_str.format(base=Color.colorify(format_address(cur_addr), base_address_color),
                                        offset=(i + offset) * l, sym=sym, val=val, text=text))
            i += 1

        return lines


@register_command
class HexdumpQwordCommand(HexdumpCommand):
    """Display SIZE lines of hexdump as QWORD from the memory location pointed by ADDRESS."""

    _cmdline_ = "hexdump qword"
    _syntax_  = "{:s} [ADDRESS] [[L][SIZE]] [REVERSE]".format(_cmdline_)
    _example_ = "{:s} qword $rsp L16 REVERSE".format(_cmdline_)

    def __init__(self):
        super().__init__()
        self.format = "qword"
        return


@register_command
class HexdumpDwordCommand(HexdumpCommand):
    """Display SIZE lines of hexdump as DWORD from the memory location pointed by ADDRESS."""

    _cmdline_ = "hexdump dword"
    _syntax_  = "{:s} [ADDRESS] [[L][SIZE]] [REVERSE]".format(_cmdline_)
    _example_ = "{:s} $esp L16 REVERSE".format(_cmdline_)

    def __init__(self):
        super().__init__()
        self.format = "dword"
        return


@register_command
class HexdumpWordCommand(HexdumpCommand):
    """Display SIZE lines of hexdump as WORD from the memory location pointed by ADDRESS."""

    _cmdline_ = "hexdump word"
    _syntax_  = "{:s} [ADDRESS] [[L][SIZE]] [REVERSE]".format(_cmdline_)
    _example_ = "{:s} $esp L16 REVERSE".format(_cmdline_)

    def __init__(self):
        super().__init__()
        self.format = "word"
        return


@register_command
class HexdumpByteCommand(HexdumpCommand):
    """Display SIZE lines of hexdump as BYTE from the memory location pointed by ADDRESS."""

    _cmdline_ = "hexdump byte"
    _syntax_  = "{:s} [ADDRESS] [[L][SIZE]] [REVERSE]".format(_cmdline_)
    _example_ = "{:s} $rsp L16".format(_cmdline_)

    def __init__(self):
        super().__init__()
        self.format = "byte"
        return


@register_command
class PatchCommand(GenericCommand):
    """Write specified values to the specified address."""

    _cmdline_ = "patch"
    _syntax_  = ("{0:s} (qword|dword|word|byte) LOCATION VALUES\n"
                 "{0:s} string LOCATION \"double-escaped string\"".format(_cmdline_))
    SUPPORTED_SIZES = {
        "qword": (8, "Q"),
        "dword": (4, "L"),
        "word": (2, "H"),
        "byte": (1, "B"),
    }

    def __init__(self):
        super().__init__(prefix=True, complete=gdb.COMPLETE_LOCATION)
        self.format = None
        return

    @only_if_gdb_running
    @parse_arguments({"location": "", "values": ["", ]}, {})
    def do_invoke(self, argv, *args, **kwargs):
        args = kwargs["arguments"]
        if not self.format or self.format not in self.SUPPORTED_SIZES:
            self.usage()
            return

        if not args.location or not args.values:
            self.usage()
            return

        addr = align_address(parse_address(args.location))
        size, fcode = self.SUPPORTED_SIZES[self.format]

        d = endian_str()
        for value in args.values:
            value = parse_address(value) & ((1 << size * 8) - 1)
            vstr = struct.pack(d + fcode, value)
            gef.memory.write(addr, vstr, length=size)
            addr += size
        return


@register_command
class PatchQwordCommand(PatchCommand):
    """Write specified QWORD to the specified address."""

    _cmdline_ = "patch qword"
    _syntax_  = "{0:s} LOCATION QWORD1 [QWORD2 [QWORD3..]]".format(_cmdline_)
    _example_ = "{:s} $rip 0x4141414141414141".format(_cmdline_)

    def __init__(self):
        super().__init__()
        self.format = "qword"
        return


@register_command
class PatchDwordCommand(PatchCommand):
    """Write specified DWORD to the specified address."""

    _cmdline_ = "patch dword"
    _syntax_  = "{0:s} LOCATION DWORD1 [DWORD2 [DWORD3..]]".format(_cmdline_)
    _example_ = "{:s} $rip 0x41414141".format(_cmdline_)

    def __init__(self):
        super().__init__()
        self.format = "dword"
        return


@register_command
class PatchWordCommand(PatchCommand):
    """Write specified WORD to the specified address."""

    _cmdline_ = "patch word"
    _syntax_  = "{0:s} LOCATION WORD1 [WORD2 [WORD3..]]".format(_cmdline_)
    _example_ = "{:s} $rip 0x4141".format(_cmdline_)

    def __init__(self):
        super().__init__()
        self.format = "word"
        return


@register_command
class PatchByteCommand(PatchCommand):
    """Write specified WORD to the specified address."""

    _cmdline_ = "patch byte"
    _syntax_  = "{0:s} LOCATION BYTE1 [BYTE2 [BYTE3..]]".format(_cmdline_)
    _example_ = "{:s} $rip 0x41 0x41 0x41 0x41 0x41".format(_cmdline_)

    def __init__(self):
        super().__init__()
        self.format = "byte"
        return


@register_command
class PatchStringCommand(GenericCommand):
    """Write specified string to the specified memory location pointed by ADDRESS."""

    _cmdline_ = "patch string"
    _syntax_  = "{:s} ADDRESS \"double backslash-escaped string\"".format(_cmdline_)
    _example_ = "{:s} $sp \"GEFROCKS\"".format(_cmdline_)

    @only_if_gdb_running
    def do_invoke(self, argv):
        argc = len(argv)
        if argc != 2:
            self.usage()
            return

        location, s = argv[0:2]
        addr = align_address(parse_address(location))

        try:
            s = codecs.escape_decode(s)[0]
        except binascii.Error:
            gef_print("Could not decode '\\xXX' encoded string \"{}\"".format(s))
            return

        gef.memory.write(addr, s, len(s))
        return

@lru_cache()
def dereference_from(addr):
    if not is_alive():
        return [format_address(addr),]

    code_color = gef.config["theme.dereference_code"]
    string_color = gef.config["theme.dereference_string"]
    max_recursion = gef.config["dereference.max_recursion"] or 10
    addr = lookup_address(align_address(int(addr)))
    msg = [format_address(addr.value),]
    seen_addrs = set()

    while addr.section and max_recursion:
        if addr.value in seen_addrs:
            msg.append("[loop detected]")
            break
        seen_addrs.add(addr.value)

        max_recursion -= 1

        # Is this value a pointer or a value?
        # -- If it's a pointer, dereference
        deref = addr.dereference()
        if deref is None:
            # if here, dereferencing addr has triggered a MemoryError, no need to go further
            msg.append(str(addr))
            break

        new_addr = lookup_address(deref)
        if new_addr.valid:
            addr = new_addr
            msg.append(str(addr))
            continue

        # -- Otherwise try to parse the value
        if addr.section:
            if addr.section.is_executable() and addr.is_in_text_segment() and not is_ascii_string(addr.value):
                insn = gef_current_instruction(addr.value)
                insn_str = "{} {} {}".format(insn.location, insn.mnemonic, ", ".join(insn.operands))
                msg.append(Color.colorify(insn_str, code_color))
                break

            elif addr.section.permission.value & Permission.READ:
                if is_ascii_string(addr.value):
                    s = gef.memory.read_cstring(addr.value)
                    if len(s) < get_memory_alignment():
                        txt = '{:s} ("{:s}"?)'.format(format_address(deref), Color.colorify(s, string_color))
                    elif len(s) > 50:
                        txt = Color.colorify('"{:s}[...]"'.format(s[:50]), string_color)
                    else:
                        txt = Color.colorify('"{:s}"'.format(s), string_color)

                    msg.append(txt)
                    break

        # if not able to parse cleanly, simply display and break
        val = "{:#0{ma}x}".format(int(deref & 0xFFFFFFFFFFFFFFFF), ma=(gef.arch.ptrsize * 2 + 2))
        msg.append(val)
        break

    return msg


@register_command
class DereferenceCommand(GenericCommand):
    """Dereference recursively from an address and display information. This acts like WinDBG `dps`
    command."""

    _cmdline_ = "dereference"
    _syntax_  = "{:s} [-h] [--length LENGTH] [--reference REFERENCE] [address]".format(_cmdline_)
    _aliases_ = ["telescope", ]
    _example_ = "{:s} --length 20 --reference $sp+0x10 $sp".format(_cmdline_)

    def __init__(self):
        super().__init__(complete=gdb.COMPLETE_LOCATION)
        self["max_recursion"] = ( 7, "Maximum level of pointer recursion")
        return

    @staticmethod
    def pprint_dereferenced(addr, idx, base_offset=0):
        base_address_color = gef.config["theme.dereference_base_address"]
        registers_color = gef.config["theme.dereference_register_value"]

        sep = " {:s} ".format(RIGHT_ARROW)
        memalign = gef.arch.ptrsize

        offset = idx * memalign
        current_address = align_address(addr + offset)
        addrs = dereference_from(current_address)
        l = ""
        addr_l = format_address(int(addrs[0], 16))
        l += "{:s}{:s}{:+#07x}: {:{ma}s}".format(Color.colorify(addr_l, base_address_color),
                                                 VERTICAL_LINE, base_offset+offset,
                                                 sep.join(addrs[1:]), ma=(memalign*2 + 2))

        register_hints = []

        for regname in gef.arch.all_registers:
            regvalue = get_register(regname)
            if current_address == regvalue:
                register_hints.append(regname)

        if register_hints:
            m = "\t{:s}{:s}".format(LEFT_ARROW, ", ".join(list(register_hints)))
            l += Color.colorify(m, registers_color)

        offset += memalign
        return l

    @only_if_gdb_running
    @parse_arguments({"address": "$sp"}, {("-r", "--reference"): "", ("-l", "--length"): 10})
    def do_invoke(self, *args, **kwargs):
        args = kwargs["arguments"]
        nb = args.length

        target = args.address
        target_addr = parse_address(target)

        reference = args.reference or target
        ref_addr = parse_address(reference)

        if process_lookup_address(target_addr) is None:
            err("Unmapped address: '{}'".format(target))
            return

        if process_lookup_address(ref_addr) is None:
            err("Unmapped address: '{}'".format(reference))
            return

        if gef.config["context.grow_stack_down"] is True:
            from_insnum = nb * (self.repeat_count + 1) - 1
            to_insnum = self.repeat_count * nb - 1
            insnum_step = -1
        else:
            from_insnum = 0 + self.repeat_count * nb
            to_insnum = nb * (self.repeat_count + 1)
            insnum_step = 1

        start_address = align_address(target_addr)
        base_offset = start_address - align_address(ref_addr)

        for i in range(from_insnum, to_insnum, insnum_step):
            gef_print(DereferenceCommand.pprint_dereferenced(start_address, i, base_offset))

        return


@register_command
class ASLRCommand(GenericCommand):
    """View/modify the ASLR setting of GDB. By default, GDB will disable ASLR when it starts the process. (i.e. not
    attached). This command allows to change that setting."""

    _cmdline_ = "aslr"
    _syntax_  = "{:s} [(on|off)]".format(_cmdline_)

    def do_invoke(self, argv):
        argc = len(argv)

        if argc == 0:
            ret = gdb.execute("show disable-randomization", to_string=True)
            i = ret.find("virtual address space is ")
            if i < 0:
                return

            msg = "ASLR is currently "
            if ret[i + 25:].strip() == "on.":
                msg += Color.redify("disabled")
            else:
                msg += Color.greenify("enabled")

            gef_print(msg)
            return

        elif argc == 1:
            if argv[0] == "on":
                info("Enabling ASLR")
                gdb.execute("set disable-randomization off")
                return
            elif argv[0] == "off":
                info("Disabling ASLR")
                gdb.execute("set disable-randomization on")
                return

            warn("Invalid command")

        self.usage()
        return


@register_command
class ResetCacheCommand(GenericCommand):
    """Reset cache of all stored data. This command is here for debugging and test purposes, GEF
    handles properly the cache reset under "normal" scenario."""

    _cmdline_ = "reset-cache"
    _syntax_  = _cmdline_

    def do_invoke(self, argv):
        reset_all_caches()
        return


@register_command
class VMMapCommand(GenericCommand):
    """Display a comprehensive layout of the virtual memory mapping. If a filter argument, GEF will
    filter out the mapping whose pathname do not match that filter."""

    _cmdline_ = "vmmap"
    _syntax_  = "{:s} [FILTER]".format(_cmdline_)
    _example_ = "{:s} libc".format(_cmdline_)

    @only_if_gdb_running
    def do_invoke(self, argv):
        vmmap = get_process_maps()
        if not vmmap:
            err("No address mapping information found")
            return

        if not gef.config["gef.disable_color"]:
            self.show_legend()

        color = gef.config["theme.table_heading"]

        headers = ["Start", "End", "Offset", "Perm", "Path"]
        gef_print(Color.colorify("{:<{w}s}{:<{w}s}{:<{w}s}{:<4s} {:s}".format(*headers, w=get_memory_alignment()*2+3), color))

        for entry in vmmap:
            if not argv:
                self.print_entry(entry)
                continue
            if argv[0] in entry.path:
                self.print_entry(entry)
            elif self.is_integer(argv[0]):
                addr = int(argv[0], 0)
                if addr >= entry.page_start and addr < entry.page_end:
                    self.print_entry(entry)
        return

    def print_entry(self, entry):
        line_color = ""
        if entry.path == "[stack]":
            line_color = gef.config["theme.address_stack"]
        elif entry.path == "[heap]":
            line_color = gef.config["theme.address_heap"]
        elif entry.permission.value & Permission.READ and entry.permission.value & Permission.EXECUTE:
            line_color = gef.config["theme.address_code"]

        l = []
        l.append(Color.colorify(format_address(entry.page_start), line_color))
        l.append(Color.colorify(format_address(entry.page_end), line_color))
        l.append(Color.colorify(format_address(entry.offset), line_color))

        if entry.permission.value == (Permission.READ|Permission.WRITE|Permission.EXECUTE):
            l.append(Color.colorify(str(entry.permission), "underline " + line_color))
        else:
            l.append(Color.colorify(str(entry.permission), line_color))

        l.append(Color.colorify(entry.path, line_color))
        line = " ".join(l)

        gef_print(line)
        return

    def show_legend(self):
        code_addr_color = gef.config["theme.address_code"]
        stack_addr_color = gef.config["theme.address_stack"]
        heap_addr_color = gef.config["theme.address_heap"]

        gef_print("[ Legend:  {} | {} | {} ]".format(Color.colorify("Code", code_addr_color),
                                                     Color.colorify("Heap", heap_addr_color),
                                                     Color.colorify("Stack", stack_addr_color)
        ))
        return

    def is_integer(self, n):
        try:
            int(n, 0)
        except ValueError:
            return False
        return True


@register_command
class XFilesCommand(GenericCommand):
    """Shows all libraries (and sections) loaded by binary. This command extends the GDB command
    `info files`, by retrieving more information from extra sources, and providing a better
    display. If an argument FILE is given, the output will grep information related to only that file.
    If an argument name is also given, the output will grep to the name within FILE."""

    _cmdline_ = "xfiles"
    _syntax_  = "{:s} [FILE [NAME]]".format(_cmdline_)
    _example_ = "\n{0:s} libc\n{0:s} libc IO_vtables".format(_cmdline_)

    @only_if_gdb_running
    def do_invoke(self, argv):
        color = gef.config["theme.table_heading"]
        headers = ["Start", "End", "Name", "File"]
        gef_print(Color.colorify("{:<{w}s}{:<{w}s}{:<21s} {:s}".format(*headers, w=get_memory_alignment()*2+3), color))

        filter_by_file = argv[0] if argv and argv[0] else None
        filter_by_name = argv[1] if len(argv) > 1 and argv[1] else None

        for xfile in get_info_files():
            if filter_by_file:
                if filter_by_file not in xfile.filename:
                    continue
                if filter_by_name and filter_by_name not in xfile.name:
                    continue

            l = []
            l.append(format_address(xfile.zone_start))
            l.append(format_address(xfile.zone_end))
            l.append("{:<21s}".format(xfile.name))
            l.append(xfile.filename)
            gef_print(" ".join(l))
        return


@register_command
class XAddressInfoCommand(GenericCommand):
    """Retrieve and display runtime information for the location(s) given as parameter."""

    _cmdline_ = "xinfo"
    _syntax_  = "{:s} LOCATION".format(_cmdline_)
    _example_ = "{:s} $pc".format(_cmdline_)

    def __init__(self):
        super().__init__(complete=gdb.COMPLETE_LOCATION)
        return

    @only_if_gdb_running
    def do_invoke(self, argv):
        if not argv:
            err("At least one valid address must be specified")
            self.usage()
            return

        for sym in argv:
            try:
                addr = align_address(parse_address(sym))
                gef_print(titlify("xinfo: {:#x}".format(addr)))
                self.infos(addr)

            except gdb.error as gdb_err:
                err("{:s}".format(str(gdb_err)))
        return

    def infos(self, address):
        addr = lookup_address(address)
        if not addr.valid:
            warn("Cannot reach {:#x} in memory space".format(address))
            return

        sect = addr.section
        info = addr.info

        if sect:
            gef_print("Page: {:s} {:s} {:s} (size={:#x})".format(format_address(sect.page_start),
                                                                 RIGHT_ARROW,
                                                                 format_address(sect.page_end),
                                                                 sect.page_end-sect.page_start))
            gef_print("Permissions: {}".format(sect.permission))
            gef_print("Pathname: {:s}".format(sect.path))
            gef_print("Offset (from page): {:#x}".format(addr.value-sect.page_start))
            gef_print("Inode: {:s}".format(sect.inode))

        if info:
            gef_print("Segment: {:s} ({:s}-{:s})".format(info.name,
                                                         format_address(info.zone_start),
                                                         format_address(info.zone_end)))
            gef_print("Offset (from segment): {:#x}".format(addr.value-info.zone_start))

        sym = gdb_get_location_from_symbol(address)
        if sym:
            name, offset = sym
            msg = "Symbol: {:s}".format(name)
            if offset:
                msg+= "+{:d}".format(offset)
            gef_print(msg)

        return


@register_command
class XorMemoryCommand(GenericCommand):
    """XOR a block of memory. The command allows to simply display the result, or patch it
    runtime at runtime."""

    _cmdline_ = "xor-memory"
    _syntax_  = "{:s} (display|patch) ADDRESS SIZE KEY".format(_cmdline_)

    def __init__(self):
        super().__init__(prefix=True)
        return

    def do_invoke(self, argv):
        self.usage()
        return


@register_command
class XorMemoryDisplayCommand(GenericCommand):
    """Display a block of memory pointed by ADDRESS by xor-ing each byte with KEY. The key must be
    provided in hexadecimal format."""

    _cmdline_ = "xor-memory display"
    _syntax_  = "{:s} ADDRESS SIZE KEY".format(_cmdline_)
    _example_ = "{:s} $sp 16 41414141".format(_cmdline_)

    @only_if_gdb_running
    def do_invoke(self, argv):
        if len(argv) != 3:
            self.usage()
            return

        address = parse_address(argv[0])
        length = int(argv[1], 0)
        key = argv[2]
        block = gef.memory.read(address, length)
        info("Displaying XOR-ing {:#x}-{:#x} with {:s}".format(address, address + len(block), repr(key)))

        gef_print(titlify("Original block"))
        gef_print(hexdump(block, base=address))

        gef_print(titlify("XOR-ed block"))
        gef_print(hexdump(xor(block, key), base=address))
        return


@register_command
class XorMemoryPatchCommand(GenericCommand):
    """Patch a block of memory pointed by ADDRESS by xor-ing each byte with KEY. The key must be
    provided in hexadecimal format."""

    _cmdline_ = "xor-memory patch"
    _syntax_  = "{:s} ADDRESS SIZE KEY".format(_cmdline_)
    _example_ = "{:s} $sp 16 41414141".format(_cmdline_)

    @only_if_gdb_running
    def do_invoke(self, argv):
        if len(argv) != 3:
            self.usage()
            return

        address = parse_address(argv[0])
        length = int(argv[1], 0)
        key = argv[2]
        block = gef.memory.read(address, length)
        info("Patching XOR-ing {:#x}-{:#x} with '{:s}'".format(address, address + len(block), key))
        xored_block = xor(block, key)
        gef.memory.write(address, xored_block, length)
        return


@register_command
class TraceRunCommand(GenericCommand):
    """Create a runtime trace of all instructions executed from $pc to LOCATION specified. The
    trace is stored in a text file that can be next imported in IDA Pro to visualize the runtime
    path."""

    _cmdline_ = "trace-run"
    _syntax_  = "{:s} LOCATION [MAX_CALL_DEPTH]".format(_cmdline_)
    _example_ = "{:s} 0x555555554610".format(_cmdline_)

    def __init__(self):
        super().__init__(self._cmdline_, complete=gdb.COMPLETE_LOCATION)
        self["max_tracing_recursion"] = ( 1, "Maximum depth of tracing")
        self["tracefile_prefix"] = ( "./gef-trace-", "Specify the tracing output file prefix")
        return

    @only_if_gdb_running
    def do_invoke(self, argv):
        if len(argv) not in (1, 2):
            self.usage()
            return

        if len(argv) == 2 and argv[1].isdigit():
            depth = int(argv[1])
        else:
            depth = 1

        try:
            loc_start   = gef.arch.pc
            loc_end     = parse_address(argv[0])
        except gdb.error as e:
            err("Invalid location: {:s}".format(e))
            return

        self.trace(loc_start, loc_end, depth)
        return

    def get_frames_size(self):
        n = 0
        f = gdb.newest_frame()
        while f:
            n += 1
            f = f.older()
        return n

    def trace(self, loc_start, loc_end, depth):
        info("Tracing from {:#x} to {:#x} (max depth={:d})".format(loc_start, loc_end, depth))
        logfile = "{:s}{:#x}-{:#x}.txt".format(self["tracefile_prefix"], loc_start, loc_end)
        enable_redirect_output(to_file=logfile)
        hide_context()
        self.start_tracing(loc_start, loc_end, depth)
        unhide_context()
        disable_redirect_output()
        ok("Done, logfile stored as '{:s}'".format(logfile))
        info("Hint: import logfile with `ida_color_gdb_trace.py` script in IDA to visualize path")
        return

    def start_tracing(self, loc_start, loc_end, depth):
        loc_cur = loc_start
        frame_count_init = self.get_frames_size()

        gef_print("#")
        gef_print("# Execution tracing of {:s}".format(get_filepath()))
        gef_print("# Start address: {:s}".format(format_address(loc_start)))
        gef_print("# End address: {:s}".format(format_address(loc_end)))
        gef_print("# Recursion level: {:d}".format(depth))
        gef_print("# automatically generated by gef.py")
        gef_print("#\n")

        while loc_cur != loc_end:
            try:
                delta = self.get_frames_size() - frame_count_init

                if delta <= depth:
                    gdb.execute("stepi")
                else:
                    gdb.execute("finish")

                loc_cur = gef.arch.pc
                gdb.flush()

            except gdb.error as e:
                gef_print("#")
                gef_print("# Execution interrupted at address {:s}".format(format_address(loc_cur)))
                gef_print("# Exception: {:s}".format(e))
                gef_print("#\n")
                break

        return


@register_command
class PatternCommand(GenericCommand):
    """Generate or Search a De Bruijn Sequence of unique substrings of length N
    and a total length of LENGTH. The default value of N is set to match the
    currently loaded architecture."""

    _cmdline_ = "pattern"
    _syntax_  = "{:s} (create|search) ARGS".format(_cmdline_)

    def __init__(self):
        super().__init__(prefix=True)
        self["length"] = ( 1024, "Default length of a cyclic buffer to generate")
        return

    def do_invoke(self, argv):
        self.usage()
        return


@register_command
class PatternCreateCommand(GenericCommand):
    """Generate a De Bruijn Sequence of unique substrings of length N and a
    total length of LENGTH. The default value of N is set to match the currently
    loaded architecture."""

    _cmdline_ = "pattern create"
    _syntax_  = "{:s} [-h] [-n N] [length]".format(_cmdline_)
    _example_ = "{:s} 4096".format(_cmdline_)

    @parse_arguments({"length": 0}, {("-n", "--n"): 0})
    def do_invoke(self, *args, **kwargs):
        args = kwargs["arguments"]
        length = args.length or gef.config["pattern.length"]
        n = args.n or gef.arch.ptrsize
        info("Generating a pattern of {:d} bytes (n={:d})".format(length, n))
        pattern_str = gef_pystring(generate_cyclic_pattern(length, n))
        gef_print(pattern_str)
        ok("Saved as '{:s}'".format(gef_convenience(pattern_str)))
        return


@register_command
class PatternSearchCommand(GenericCommand):
    """Search a De Bruijn Sequence of unique substrings of length N and a
    maximum total length of MAX_LENGTH. The default value of N is set to match
    the currently loaded architecture. The PATTERN argument can be a GDB symbol
    (such as a register name), a string or a hexadecimal value"""

    _cmdline_ = "pattern search"
    _syntax_  = "{:s} [-h] [-n N] [--max-length MAX_LENGTH] [pattern]".format(_cmdline_)
    _example_ = "\n{0:s} $pc\n{0:s} 0x61616164\n{0:s} aaab".format(_cmdline_)
    _aliases_ = ["pattern offset"]

    @only_if_gdb_running
    @parse_arguments({"pattern": ""}, {("-n", "--n"): 0, ("-l", "--max-length"): 0})
    def do_invoke(self, *args, **kwargs):
        args = kwargs["arguments"]
        max_length = args.max_length or gef.config["pattern.length"]
        n = args.n or gef.arch.ptrsize
        info("Searching for '{:s}'".format(args.pattern))
        self.search(args.pattern, max_length, n)
        return

    def search(self, pattern, size, period):
        pattern_be, pattern_le = None, None

        # 1. check if it's a symbol (like "$sp" or "0x1337")
        symbol = safe_parse_and_eval(pattern)
        if symbol:
            addr = int(symbol)
            dereferenced_value = dereference(addr)
            # 1-bis. try to dereference
            if dereferenced_value:
                addr = int(dereferenced_value)
            struct_packsize = {
                2: "H",
                4: "I",
                8: "Q",
            }
            pattern_be = struct.pack(">{}".format(struct_packsize[gef.arch.ptrsize]), addr)
            pattern_le = struct.pack("<{}".format(struct_packsize[gef.arch.ptrsize]), addr)
        else:
            # 2. assume it's a plain string
            pattern_be = gef_pybytes(pattern)
            pattern_le = gef_pybytes(pattern[::-1])

        cyclic_pattern = generate_cyclic_pattern(size, period)
        found = False
        off = cyclic_pattern.find(pattern_le)
        if off >= 0:
            ok("Found at offset {:d} (little-endian search) {:s}".format(off, Color.colorify("likely", "bold red") if is_little_endian() else ""))
            found = True

        off = cyclic_pattern.find(pattern_be)
        if off >= 0:
            ok("Found at offset {:d} (big-endian search) {:s}".format(off, Color.colorify("likely", "bold green") if is_big_endian() else ""))
            found = True

        if not found:
            err("Pattern '{}' not found".format(pattern))
        return


@register_command
class ChecksecCommand(GenericCommand):
    """Checksec the security properties of the current executable or passed as argument. The
    command checks for the following protections:
    - PIE
    - NX
    - RelRO
    - Glibc Stack Canaries
    - Fortify Source"""

    _cmdline_ = "checksec"
    _syntax_  = "{:s} [FILENAME]".format(_cmdline_)
    _example_ = "{} /bin/ls".format(_cmdline_)

    def __init__(self):
        super().__init__(complete=gdb.COMPLETE_FILENAME)
        return

    def pre_load(self):
        which("readelf")
        return

    def do_invoke(self, argv):
        argc = len(argv)

        if argc == 0:
            filename = get_filepath()
            if filename is None:
                warn("No executable/library specified")
                return
        elif argc == 1:
            filename = os.path.realpath(os.path.expanduser(argv[0]))
            if not os.access(filename, os.R_OK):
                err("Invalid filename")
                return
        else:
            self.usage()
            return

        info("{:s} for '{:s}'".format(self._cmdline_, filename))
        self.print_security_properties(filename)
        return

    def print_security_properties(self, filename):
        sec = checksec(filename)
        for prop in sec:
            if prop in ("Partial RelRO", "Full RelRO"): continue
            val = sec[prop]
            msg = Color.greenify(Color.boldify(TICK)) if val is True else Color.redify(Color.boldify(CROSS))
            if val and prop == "Canary" and is_alive():
                canary = gef_read_canary()[0]
                msg+= "(value: {:#x})".format(canary)

            gef_print("{:<30s}: {:s}".format(prop, msg))

        if sec["Full RelRO"]:
            gef_print("{:<30s}: {:s}".format("RelRO", Color.greenify("Full")))
        elif sec["Partial RelRO"]:
            gef_print("{:<30s}: {:s}".format("RelRO", Color.yellowify("Partial")))
        else:
            gef_print("{:<30s}: {:s}".format("RelRO", Color.redify(Color.boldify(CROSS))))
        return


@register_command
class GotCommand(GenericCommand):
    """Display current status of the got inside the process."""

    _cmdline_ = "got"
    _syntax_ = "{:s} [FUNCTION_NAME ...] ".format(_cmdline_)
    _example_ = "got read printf exit"

    def __init__(self, *args, **kwargs):
        super().__init__()
        self["function_resolved"] = ( "green", "Line color of the got command output if the function has "
                                                       "been resolved")
        self["function_not_resolved"] = ( "yellow", "Line color of the got command output if the function has "
                                                       "not been resolved")
        return

    def pre_load(self):
        which("readelf")
        return

    def get_jmp_slots(self, readelf, filename):
        output = []
        cmd = [readelf, "--relocs", filename]
        lines = gef_execute_external(cmd, as_list=True)
        for line in lines:
            if "JUMP" in line:
                output.append(line)
        return output

    @only_if_gdb_running
    def do_invoke(self, argv):

        try:
            readelf = which("readelf")
        except IOError:
            err("Missing `readelf`")
            return

        # get the filtering parameter.
        func_names_filter = []
        if argv:
            func_names_filter = argv

        # getting vmmap to understand the boundaries of the main binary
        # we will use this info to understand if a function has been resolved or not.
        vmmap = get_process_maps()
        base_address = min([x.page_start for x in vmmap if x.path == get_filepath()])
        end_address = max([x.page_end for x in vmmap if x.path == get_filepath()])

        # get the checksec output.
        checksec_status = checksec(get_filepath())
        relro_status = "Full RelRO"
        full_relro = checksec_status["Full RelRO"]
        pie = checksec_status["PIE"]  # if pie we will have offset instead of abs address.

        if not full_relro:
            relro_status = "Partial RelRO"
            partial_relro = checksec_status["Partial RelRO"]

            if not partial_relro:
                relro_status = "No RelRO"

        # retrieve jump slots using readelf
        jmpslots = self.get_jmp_slots(readelf, get_filepath())

        gef_print("\nGOT protection: {} | GOT functions: {}\n ".format(relro_status, len(jmpslots)))

        for line in jmpslots:
            address, _, _, _, name = line.split()[:5]

            # if we have a filter let's skip the entries that are not requested.
            if func_names_filter:
                if not any(map(lambda x: x in name, func_names_filter)):
                    continue

            address_val = int(address, 16)

            # address_val is an offset from the base_address if we have PIE.
            if pie:
                address_val = base_address + address_val

            # read the address of the function.
            got_address = gef.memory.read_integer(address_val)

            # for the swag: different colors if the function has been resolved or not.
            if base_address < got_address < end_address:
                color = self["function_not_resolved"]  # function hasn't already been resolved
            else:
                color = self["function_resolved"]      # function has already been resolved

            line = "[{}] ".format(hex(address_val))
            line += Color.colorify("{} {} {}".format(name, RIGHT_ARROW, hex(got_address)), color)
            gef_print(line)

        return


@register_command
class HighlightCommand(GenericCommand):
    """Highlight user-defined text matches in GEF output universally."""
    _cmdline_ = "highlight"
    _syntax_ = "{} (add|remove|list|clear)".format(_cmdline_)
    _aliases_ = ["hl"]

    def __init__(self):
        super().__init__(prefix=True)
        self["regex"] = ( False, "Enable regex highlighting")

    def do_invoke(self, argv):
        return self.usage()


@register_command
class HighlightListCommand(GenericCommand):
    """Show the current highlight table with matches to colors."""
    _cmdline_ = "highlight list"
    _aliases_ = ["highlight ls", "hll"]
    _syntax_ = _cmdline_

    def print_highlight_table(self):
        if not highlight_table:
            return err("no matches found")

        left_pad = max(map(len, highlight_table.keys()))
        for match, color in sorted(highlight_table.items()):
            print("{} {} {}".format(Color.colorify(match.ljust(left_pad), color), VERTICAL_LINE, Color.colorify(color, color)))
        return

    def do_invoke(self, argv):
        return self.print_highlight_table()


@register_command
class HighlightClearCommand(GenericCommand):
    """Clear the highlight table, remove all matches."""
    _cmdline_ = "highlight clear"
    _aliases_ = ["hlc"]
    _syntax_ = _cmdline_

    def do_invoke(self, argv):
        return highlight_table.clear()


@register_command
class HighlightAddCommand(GenericCommand):
    """Add a match to the highlight table."""
    _cmdline_ = "highlight add"
    _syntax_ = "{} MATCH COLOR".format(_cmdline_)
    _aliases_ = ["highlight set", "hla"]
    _example_ = "{} 41414141 yellow".format(_cmdline_)

    def do_invoke(self, argv):
        if len(argv) < 2:
            return self.usage()

        match, color = argv
        highlight_table[match] = color
        return


@register_command
class HighlightRemoveCommand(GenericCommand):
    """Remove a match in the highlight table."""
    _cmdline_ = "highlight remove"
    _syntax_ = "{} MATCH".format(_cmdline_)
    _aliases_ = [
        "highlight delete",
        "highlight del",
        "highlight unset",
        "highlight rm",
        "hlr",
    ]
    _example_ = "{} remove 41414141".format(_cmdline_)

    def do_invoke(self, argv):
        if not argv:
            return self.usage()

        highlight_table.pop(argv[0], None)
        return


@register_command
class FormatStringSearchCommand(GenericCommand):
    """Exploitable format-string helper: this command will set up specific breakpoints
    at well-known dangerous functions (printf, snprintf, etc.), and check if the pointer
    holding the format string is writable, and therefore susceptible to format string
    attacks if an attacker can control its content."""
    _cmdline_ = "format-string-helper"
    _syntax_ = _cmdline_
    _aliases_ = ["fmtstr-helper",]

    def do_invoke(self, argv):
        dangerous_functions = {
            "printf": 0,
            "sprintf": 1,
            "fprintf": 1,
            "snprintf": 2,
            "vsnprintf": 2,
        }

        enable_redirect_output("/dev/null")

        for func_name, num_arg in dangerous_functions.items():
            FormatStringBreakpoint(func_name, num_arg)

        disable_redirect_output()
        ok("Enabled {:d} FormatStringBreakpoint".format(len(dangerous_functions)))
        return



@register_command
class HeapAnalysisCommand(GenericCommand):
    """Heap vulnerability analysis helper: this command aims to track dynamic heap allocation
    done through malloc()/free() to provide some insights on possible heap vulnerabilities. The
    following vulnerabilities are checked:
    - NULL free
    - Use-after-Free
    - Double Free
    - Heap overlap"""
    _cmdline_ = "heap-analysis-helper"
    _syntax_ = _cmdline_

    def __init__(self, *args, **kwargs):
        super().__init__(complete=gdb.COMPLETE_NONE)
        self["check_free_null"] = ( False, "Break execution when a free(NULL) is encountered")
        self["check_double_free"] = ( True, "Break execution when a double free is encountered")
        self["check_weird_free"] = ( True, "Break execution when free() is called against a non-tracked pointer")
        self["check_uaf"] = ( True, "Break execution when a possible Use-after-Free condition is found")
        self["check_heap_overlap"] = ( True, "Break execution when a possible overlap in allocation is found")

        self.bp_malloc = None
        self.bp_calloc = None
        self.bp_free = None
        self.bp_realloc = None
        return

    @only_if_gdb_running
    @experimental_feature
    def do_invoke(self, argv):
        if not argv:
            self.setup()
            return

        if argv[0] == "show":
            self.dump_tracked_allocations()
        return

    def setup(self):
        ok("Tracking malloc() & calloc()")
        self.bp_malloc = TraceMallocBreakpoint("__libc_malloc")
        self.bp_calloc = TraceMallocBreakpoint("__libc_calloc")
        ok("Tracking free()")
        self.bp_free = TraceFreeBreakpoint()
        ok("Tracking realloc()")
        self.bp_realloc = TraceReallocBreakpoint()

        ok("Disabling hardware watchpoints (this may increase the latency)")
        gdb.execute("set can-use-hw-watchpoints 0")

        info("Dynamic breakpoints correctly setup, GEF will break execution if a possible vulnerabity is found.")
        warn("{}: The heap analysis slows down the execution noticeably.".format(
            Color.colorify("Note", "bold underline yellow")))

        # when inferior quits, we need to clean everything for a next execution
        gef_on_exit_hook(self.clean)
        return

    def dump_tracked_allocations(self):
        global __heap_allocated_list__, __heap_freed_list__, __heap_uaf_watchpoints__

        if __heap_allocated_list__:
            ok("Tracked as in-use chunks:")
            for addr, sz in __heap_allocated_list__: gef_print("{} malloc({:d}) = {:#x}".format(CROSS, sz, addr))
        else:
            ok("No malloc() chunk tracked")

        if __heap_freed_list__:
            ok("Tracked as free-ed chunks:")
            for addr, sz in __heap_freed_list__: gef_print("{}  free({:d}) = {:#x}".format(TICK, sz, addr))
        else:
            ok("No free() chunk tracked")
        return

    def clean(self, event):
        global __heap_allocated_list__, __heap_freed_list__, __heap_uaf_watchpoints__

        ok("{} - Cleaning up".format(Color.colorify("Heap-Analysis", "yellow bold"),))
        for bp in [self.bp_malloc, self.bp_calloc, self.bp_free, self.bp_realloc]:
            if hasattr(bp, "retbp") and bp.retbp:
                try:
                    bp.retbp.delete()
                except RuntimeError:
                    # in some cases, gdb was found failing to correctly remove the retbp but they can be safely ignored since the debugging session is over
                    pass

            bp.delete()

        for wp in __heap_uaf_watchpoints__:
            wp.delete()

        __heap_allocated_list__ = []
        __heap_freed_list__ = []
        __heap_uaf_watchpoints__ = []

        ok("{} - Re-enabling hardware watchpoints".format(Color.colorify("Heap-Analysis", "yellow bold"),))
        gdb.execute("set can-use-hw-watchpoints 1")

        gef_on_exit_unhook(self.clean)
        return


@register_command
class IsSyscallCommand(GenericCommand):
    """Tells whether the next instruction is a system call."""
    _cmdline_ = "is-syscall"
    _syntax_ = _cmdline_

    def do_invoke(self, argv):
        insn = gef_current_instruction(gef.arch.pc)
        ok("Current instruction is{}a syscall".format(" " if self.is_syscall(gef.arch, insn) else " not "))

        return

    def is_syscall(self, arch, instruction):
        insn_str = instruction.mnemonic + " " + ", ".join(instruction.operands)
        return insn_str.strip() in arch.syscall_instructions


@register_command
class SyscallArgsCommand(GenericCommand):
    """Gets the syscall name and arguments based on the register values in the current state."""
    _cmdline_ = "syscall-args"
    _syntax_ = _cmdline_

    def __init__(self):
        super().__init__()
        path = pathlib.Path(gef.config["gef.tempdir"]) / "syscall-tables"
        if not path.exists():
            raise EnvironmentError("Syscall tables directory not found")
        self["path"] = (str(path.absolute()), "Path to store/load the syscall tables files")
        return

    def do_invoke(self, argv):
        color = gef.config["theme.table_heading"]

        path = self.get_settings_path()
        if path is None:
            err("Cannot open '{0}': check directory and/or `gef config {0}` setting, "
                "currently: '{1}'".format("syscall-args.path", self["path"]))
            info("This setting can be configured by running gef-extras' install script.")
            return

        arch = gef.arch.__class__.__name__
        syscall_table = self.get_syscall_table(arch)

        reg_value = get_register(gef.arch.syscall_register)
        if reg_value not in syscall_table:
            warn("There is no system call for {:#x}".format(reg_value))
            return
        syscall_entry = syscall_table[reg_value]

        values = []
        for param in syscall_entry.params:
            values.append(get_register(param.reg))

        parameters = [s.param for s in syscall_entry.params]
        registers = [s.reg for s in syscall_entry.params]

        info("Detected syscall {}".format(Color.colorify(syscall_entry.name, color)))
        gef_print("    {}({})".format(syscall_entry.name, ", ".join(parameters)))

        headers = ["Parameter", "Register", "Value"]
        param_names = [re.split(r" |\*", p)[-1] for p in parameters]
        info(Color.colorify("{:<20} {:<20} {}".format(*headers), color))
        for name, register, value in zip(param_names, registers, values):
            line = "    {:<20} {:<20} 0x{:x}".format(name, register, value)

            addrs = dereference_from(value)

            if len(addrs) > 1:
                sep = " {:s} ".format(RIGHT_ARROW)
                line += sep
                line += sep.join(addrs[1:])

            gef_print(line)

        return

    def get_filepath(self, x):
        p = self.get_settings_path()
        if not p: return None
        return os.path.join(p, "{}.py".format(x))

    def get_module(self, modname):
        _fullname = self.get_filepath(modname)
        return importlib.machinery.SourceFileLoader(modname, _fullname).load_module(None)

    def get_syscall_table(self, modname):
        _mod = self.get_module(modname)
        return getattr(_mod, "syscall_table")

    def get_settings_path(self):
        path = os.path.expanduser(self["path"])
        path = os.path.realpath(path)
        return path if os.path.isdir(path) else None


@lru_cache()
def get_section_base_address(name):
    section = process_lookup_path(name)
    if section:
        return section.page_start

    return None

@lru_cache()
def get_zone_base_address(name):
    zone = file_lookup_name_path(name, get_filepath())
    if zone:
        return zone.zone_start

    return None

class GenericFunction(gdb.Function, metaclass=abc.ABCMeta):
    """This is an abstract class for invoking convenience functions, should not be instantiated."""

    _example_ = ""

    @abc.abstractproperty
    def _function_(self): pass
    @property
    def _syntax_(self):
        return "${}([offset])".format(self._function_)

    def __init__ (self):
        super().__init__(self._function_)

    def invoke(self, *args):
        if not is_alive():
            raise gdb.GdbError("No debugging session active")
        return int(self.do_invoke(args))

    def arg_to_long(self, args, index, default=0):
        try:
            addr = args[index]
            return int(addr) if addr.address is None else int(addr.address)
        except IndexError:
            return default

    @abc.abstractmethod
    def do_invoke(self, args): pass


@register_function
class StackOffsetFunction(GenericFunction):
    """Return the current stack base address plus an optional offset."""
    _function_ = "_stack"

    def do_invoke(self, args):
        return self.arg_to_long(args, 0) + get_section_base_address("[stack]")


@register_function
class HeapBaseFunction(GenericFunction):
    """Return the current heap base address plus an optional offset."""
    _function_ = "_heap"

    def do_invoke(self, args):
        base = HeapBaseFunction.heap_base()
        if not base:
            raise gdb.GdbError("Heap not found")

        return self.arg_to_long(args, 0) + base

    @staticmethod
    def heap_base():
        try:
            base = parse_address("mp_->sbrk_base")
            if base != 0:
                return base
        except gdb.error:
            pass
        return get_section_base_address("[heap]")


@register_function
class SectionBaseFunction(GenericFunction):
    """Return the matching file's base address plus an optional offset.
    Defaults to current file. Note that quotes need to be escaped"""
    _function_ = "_base"
    _syntax_   = "$_base([filepath])"
    _example_  = "p $_base(\\\"/usr/lib/ld-2.33.so\\\")"

    def do_invoke(self, args):
        try:
            name = args[0].string()
        except IndexError:
            name = get_filename()
        except gdb.error:
            err("Invalid arg: {}".format(args[0]))
            return 0

        try:
            addr = int(get_section_base_address(name))
        except TypeError:
            err("Cannot find section {}".format(name))
            return 0
        return addr


@register_function
class BssBaseFunction(GenericFunction):
    """Return the current bss base address plus the given offset."""
    _function_ = "_bss"
    _example_ = "deref $_bss(0x20)"

    def do_invoke(self, args):
        return self.arg_to_long(args, 0) + get_zone_base_address(".bss")


@register_function
class GotBaseFunction(GenericFunction):
    """Return the current bss base address plus the given offset."""
    _function_ = "_got"

    def do_invoke(self, args):
        return self.arg_to_long(args, 0) + get_zone_base_address(".got")


@register_command
class GefFunctionsCommand(GenericCommand):
    """List the convenience functions provided by GEF."""
    _cmdline_ = "functions"
    _syntax_ = _cmdline_

    def __init__(self):
        super().__init__()
        self.docs = []
        self.setup()
        return

    def setup(self):
        global gef
        for function in gef.instance.loaded_functions:
            self.add_function_to_doc(function)
        self.__doc__ = "\n".join(sorted(self.docs))
        return

    def add_function_to_doc(self, function):
        """Add function to documentation."""
        doc = getattr(function, "__doc__", "").lstrip()
        doc = "\n                         ".join(doc.split("\n"))
        syntax = getattr(function, "_syntax_", "").lstrip()
        msg = "{syntax:<25s} -- {help:s}".format(syntax=syntax, help=Color.greenify(doc))
        example = getattr(function, "_example_", "").strip()
        if example:
            msg += "\n {padding:27s} example: {example:s}".format(
                padding="", example=Color.yellowify(example))
        self.docs.append(msg)
        return

    def do_invoke(self, argv):
        self.dont_repeat()
        gef_print(titlify("GEF - Convenience Functions"))
        gef_print("These functions can be used as arguments to other "
                  "commands to dynamically calculate values\n")
        gef_print(self.__doc__)
        return


class GefCommand(gdb.Command):
    """GEF main command: view all new commands by typing `gef`."""

    _cmdline_ = "gef"
    _syntax_  = "{:s} (missing|config|save|restore|set|run)".format(_cmdline_)

    def __init__(self):
        super().__init__(self._cmdline_, gdb.COMMAND_SUPPORT, gdb.COMPLETE_NONE, True)
        gef.config["gef.follow_child"] = GefSetting(True, bool, "Automatically set GDB to follow child when forking")
        gef.config["gef.readline_compat"] = GefSetting(False, bool, "Workaround for readline SOH/ETX issue (SEGV)")
        gef.config["gef.debug"] = GefSetting(False, bool, "Enable debug mode for gef")
        gef.config["gef.autosave_breakpoints_file"] = GefSetting("", str, "Automatically save and restore breakpoints")
        gef.config["gef.extra_plugins_dir"] = GefSetting("", str, "Autoload additional GEF commands from external directory")
        gef.config["gef.disable_color"] = GefSetting(False, bool, "Disable all colors in GEF")
        gef.config["gef.tempdir"] = GefSetting(GEF_TEMP_DIR, str, "Directory to use for temporary/cache content")
        self.loaded_commands = []
        self.loaded_functions = []
        self.missing_commands = {}
        return

    def setup(self):
        self.load(initial=True)
        # loading GEF sub-commands
        self.doc = GefHelpCommand(self.loaded_commands)
        self.cfg = GefConfigCommand(self.loaded_command_names)
        GefSaveCommand()
        GefRestoreCommand()
        GefMissingCommand()
        GefSetCommand()
        GefRunCommand()

        # load the saved settings
        gdb.execute("gef restore")

        # restore the autosave/autoreload breakpoints policy (if any)
        self.__reload_auto_breakpoints()

        # load plugins from `extra_plugins_dir`
        if self.__load_extra_plugins() > 0:
            # if here, at least one extra plugin was loaded, so we need to restore
            # the settings once more
            gdb.execute("gef restore quiet")
        return

    def __reload_auto_breakpoints(self):
        bkp_fname = gef.config["gef.autosave_breakpoints_file"]
        bkp_fname = bkp_fname[0] if bkp_fname else None
        if bkp_fname:
            # restore if existing
            if os.access(bkp_fname, os.R_OK):
                gdb.execute("source {:s}".format(bkp_fname))

            # add hook for autosave breakpoints on quit command
            source = [
                "define hook-quit",
                " save breakpoints {:s}".format(bkp_fname),
                "end",
            ]
            gef_execute_gdb_script("\n".join(source) + "\n")
        return

    def __load_extra_plugins(self):
        nb_added = -1
        try:
            nb_inital = len(self.loaded_commands)
            directories = gef.config["gef.extra_plugins_dir"]
            if directories:
                for directory in directories.split(";"):
                    directory = os.path.realpath(os.path.expanduser(directory))
                    if os.path.isdir(directory):
                        sys.path.append(directory)
                        for fname in os.listdir(directory):
                            if not fname.endswith(".py"): continue
                            fpath = "{:s}/{:s}".format(directory, fname)
                            if os.path.isfile(fpath):
                                gdb.execute("source {:s}".format(fpath))
            nb_added = len(self.loaded_commands) - nb_inital
            if nb_added > 0:
                ok("{:s} extra commands added from '{:s}'".format(Color.colorify(nb_added, "bold green"),
                                                                  Color.colorify(directories, "bold blue")))
        except gdb.error as e:
            err("failed: {}".format(str(e)))
        return nb_added

    @property
    def loaded_command_names(self):
        return [x[0] for x in self.loaded_commands]

    def invoke(self, args, from_tty):
        self.dont_repeat()
        gdb.execute("gef help")
        return

    def add_context_pane(self, pane_name, display_pane_function, pane_title_function):
        """Add a new context pane to ContextCommand."""
        for _, _, class_obj in self.loaded_commands:
            if isinstance(class_obj, ContextCommand):
                context_obj = class_obj
                break

        # assure users can toggle the new context
        corrected_settings_name = pane_name.replace(" ", "_")
        layout_settings = context_obj.get_setting("layout")
        context_obj.update_setting("layout", "{} {}".format(layout_settings, corrected_settings_name))

        # overload the printing of pane title
        context_obj.layout_mapping[corrected_settings_name] = (display_pane_function, pane_title_function)

    def load(self, initial=False):
        """Load all the commands and functions defined by GEF into GDB."""
        nb_missing = 0
        self.commands = [(x._cmdline_, x) for x in __commands__]

        # load all of the functions
        for function_class_name in __functions__:
            self.loaded_functions.append(function_class_name())

        def is_loaded(x):
            return any(u for u in self.loaded_commands if x == u[0])

        for cmd, class_name in self.commands:
            if is_loaded(cmd):
                continue

            try:
                self.loaded_commands.append((cmd, class_name, class_name()))

                if hasattr(class_name, "_aliases_"):
                    aliases = getattr(class_name, "_aliases_")
                    for alias in aliases:
                        GefAlias(alias, cmd)

            except Exception as reason:
               self.missing_commands[cmd] = reason
               nb_missing += 1

        # sort by command name
        self.loaded_commands = sorted(self.loaded_commands, key=lambda x: x[1]._cmdline_)

        if initial:
            gef_print("{:s} for {:s} ready, type `{:s}' to start, `{:s}' to configure"
                      .format(Color.greenify("GEF"), get_os(),
                              Color.colorify("gef", "underline yellow"),
                              Color.colorify("gef config", "underline pink")))

            ver = "{:d}.{:d}".format(sys.version_info.major, sys.version_info.minor)
            nb_cmds = len(self.loaded_commands)
            gef_print("{:s} commands loaded for GDB {:s} using Python engine {:s}"
                      .format(Color.colorify(nb_cmds, "bold green"),
                              Color.colorify(gdb.VERSION, "bold yellow"),
                              Color.colorify(ver, "bold red")))

            if nb_missing:
                warn("{:s} command{} could not be loaded, run `{:s}` to know why."
                          .format(Color.colorify(nb_missing, "bold red"),
                                  "s" if nb_missing > 1 else "",
                                  Color.colorify("gef missing", "underline pink")))
        return


class GefHelpCommand(gdb.Command):
    """GEF help sub-command."""
    _cmdline_ = "gef help"
    _syntax_  = _cmdline_

    def __init__(self, commands, *args, **kwargs):
        super().__init__(self._cmdline_, gdb.COMMAND_SUPPORT, gdb.COMPLETE_NONE, False)
        self.docs = []
        self.generate_help(commands)
        self.refresh()
        return

    def invoke(self, args, from_tty):
        self.dont_repeat()
        gef_print(titlify("GEF - GDB Enhanced Features"))
        gef_print(self.__doc__)
        return

    def generate_help(self, commands):
        """Generate builtin commands documentation."""
        for command in commands:
            self.add_command_to_doc(command)
        return

    def add_command_to_doc(self, command):
        """Add command to GEF documentation."""
        cmd, class_name, _  = command
        if " " in cmd:
            # do not print subcommands in gef help
            return
        doc = getattr(class_name, "__doc__", "").lstrip()
        doc = "\n                         ".join(doc.split("\n"))
        aliases = " (alias: {:s})".format(", ".join(class_name._aliases_)) if hasattr(class_name, "_aliases_") else ""
        msg = "{cmd:<25s} -- {help:s}{aliases:s}".format(cmd=cmd, help=doc, aliases=aliases)
        self.docs.append(msg)
        return

    def refresh(self):
        """Refresh the documentation."""
        self.__doc__ = "\n".join(sorted(self.docs))
        return


class GefConfigCommand(gdb.Command):
    """GEF configuration sub-command
    This command will help set/view GEF settings for the current debugging session.
    It is possible to make those changes permanent by running `gef save` (refer
    to this command help), and/or restore previously saved settings by running
    `gef restore` (refer help).
    """
    _cmdline_ = "gef config"
    _syntax_  = "{:s} [setting_name] [setting_value]".format(_cmdline_)

    def __init__(self, loaded_commands, *args, **kwargs):
        super().__init__(self._cmdline_, gdb.COMMAND_NONE, prefix=False)
        self.loaded_commands = loaded_commands
        return

    def invoke(self, args, from_tty):
        self.dont_repeat()
        argv = gdb.string_to_argv(args)
        argc = len(argv)

        if not (0 <= argc <= 2):
            err("Invalid number of arguments")
            return

        if argc == 0:
            gef_print(titlify("GEF configuration settings"))
            self.print_settings()
            return

        if argc == 1:
            prefix = argv[0]
            names = [x for x in gef.config.keys() if x.startswith(prefix)]
            if names:
                if len(names) == 1:
                    gef_print(titlify("GEF configuration setting: {:s}".format(names[0])))
                    self.print_setting(names[0], verbose=True)
                else:
                    gef_print(titlify("GEF configuration settings matching '{:s}'".format(argv[0])))
                    for name in names: self.print_setting(name)
            return

        self.set_setting(argv)
        return

    def print_setting(self, plugin_name, verbose=False):
        res = gef.config.raw_entry(plugin_name)
        string_color = gef.config["theme.dereference_string"]
        misc_color = gef.config["theme.dereference_base_address"]

        if not res:
            return

        _setting = Color.colorify(plugin_name, "green")
        _type = res.type.__name__
        if _type == "str":
            _value = '"{:s}"'.format(Color.colorify(res.value, string_color))
        else:
            _value = Color.colorify(res.value, misc_color)

        gef_print("{:s} ({:s}) = {:s}".format(_setting, _type, _value))

        if verbose:
            gef_print(Color.colorify("\nDescription:", "bold underline"))
            gef_print("\t{:s}".format(res.description))
        return

    def print_settings(self):
        for x in sorted(gef.config):
            self.print_setting(x)
        return

    def set_setting(self, argv):
        global gef
        key, new_value = argv

        if "." not in key:
            err("Invalid command format")
            return

        loaded_commands = [ x[0] for x in gef.instance.loaded_commands ] + ["gef"]
        plugin_name = key.split(".", 1)[0]
        if plugin_name not in loaded_commands:
            err("Unknown plugin '{:s}'".format(plugin_name))
            return

        if key not in gef.config:
            err("'{}' is not a valid configuration setting".format(key))
            return

        _type = gef.config.raw_entry(key).type
        try:
            if _type == bool:
                _newval = True if new_value.upper() in ("TRUE", "T", "1") else False
            else:
                _newval = new_value

            gef.config[key] = _newval
        except Exception:
            err("{} expects type '{}'".format(key, _type.__name__))
            return

        reset_all_caches()
        return

    def complete(self, text, word):
        settings = sorted(gef.config)

        if text == "":
            # no prefix: example: `gef config TAB`
            return [s for s in settings if word in s]

        if "." not in text:
            # if looking for possible prefix
            return [s for s in settings if s.startswith(text.strip())]

        # finally, look for possible values for given prefix
        return [s.split(".", 1)[1] for s in settings if s.startswith(text.strip())]


class GefSaveCommand(gdb.Command):
    """GEF save sub-command.
    Saves the current configuration of GEF to disk (by default in file '~/.gef.rc')."""
    _cmdline_ = "gef save"
    _syntax_  = _cmdline_

    def __init__(self, *args, **kwargs):
        super().__init__(self._cmdline_, gdb.COMMAND_SUPPORT, gdb.COMPLETE_NONE, False)
        return

    def invoke(self, args, from_tty):
        self.dont_repeat()
        cfg = configparser.RawConfigParser()
        old_sect = None

        # save the configuration
        for key in sorted(gef.config):
            sect, optname = key.split(".", 1)
            value = gef.config[key]
            value = value[0] if value else None

            if old_sect != sect:
                cfg.add_section(sect)
                old_sect = sect

            cfg.set(sect, optname, value)

        # save the aliases
        cfg.add_section("aliases")
        for alias in __aliases__:
            cfg.set("aliases", alias._alias, alias._command)

        with open(GEF_RC, "w") as fd:
            cfg.write(fd)

        ok("Configuration saved to '{:s}'".format(GEF_RC))
        return


class GefRestoreCommand(gdb.Command):
    """GEF restore sub-command.
    Loads settings from file '~/.gef.rc' and apply them to the configuration of GEF."""
    _cmdline_ = "gef restore"
    _syntax_  = _cmdline_

    def __init__(self, *args, **kwargs):
        super().__init__(self._cmdline_, gdb.COMMAND_SUPPORT, gdb.COMPLETE_NONE, False)
        return

    def invoke(self, args, from_tty):
        self.dont_repeat()
        if not os.access(GEF_RC, os.R_OK):
            return

        quiet = args.lower() == "quiet"
        cfg = configparser.ConfigParser()
        cfg.read(GEF_RC)

        for section in cfg.sections():
            if section == "aliases":
                # load the aliases
                for key in cfg.options(section):
                    try:
                        GefAlias(key, cfg.get(section, key))
                    except:
                        pass
                continue

            # load the other options
            for optname in cfg.options(section):
                try:
                    key = "{:s}.{:s}".format(section, optname)
                    _type = gef.config[key].type
                    new_value = cfg.get(section, optname)
                    if _type == bool:
                        new_value = True if new_value == "True" else False
                    else:
                        new_value = _type(new_value)
                    gef.config[key][0] = new_value
                except Exception:
                    pass

        # ensure that the temporary directory always exists
        gef_makedirs(gef.config["gef.tempdir"])

        if not quiet:
            ok("Configuration from '{:s}' restored".format(Color.colorify(GEF_RC, "bold blue")))
        return


class GefMissingCommand(gdb.Command):
    """GEF missing sub-command
    Display the GEF commands that could not be loaded, along with the reason of why
    they could not be loaded.
    """
    _cmdline_ = "gef missing"
    _syntax_  = _cmdline_

    def __init__(self, *args, **kwargs):
        super().__init__(self._cmdline_, gdb.COMMAND_SUPPORT, gdb.COMPLETE_NONE, False)
        return

    def invoke(self, args, from_tty):
        self.dont_repeat()
        missing_commands = gef.instance.missing_commands.keys()
        if not missing_commands:
            ok("No missing command")
            return

        for missing_command in missing_commands:
            reason = gef.instance.missing_commands[missing_command]
            warn("Command `{}` is missing, reason {} {}".format(missing_command, RIGHT_ARROW, reason))
        return


class GefSetCommand(gdb.Command):
    """Override GDB set commands with the context from GEF."""
    _cmdline_ = "gef set"
    _syntax_  = "{:s} [GDB_SET_ARGUMENTS]".format(_cmdline_)

    def __init__(self, *args, **kwargs):
        super().__init__(self._cmdline_, gdb.COMMAND_SUPPORT, gdb.COMPLETE_SYMBOL, False)
        return

    def invoke(self, args, from_tty):
        self.dont_repeat()
        args = args.split()
        cmd = ["set", args[0],]
        for p in args[1:]:
            if p.startswith("$_gef"):
                c = gdb.parse_and_eval(p)
                cmd.append(c.string())
            else:
                cmd.append(p)

        gdb.execute(" ".join(cmd))
        return


class GefRunCommand(gdb.Command):
    """Override GDB run commands with the context from GEF.
    Simple wrapper for GDB run command to use arguments set from `gef set args`."""
    _cmdline_ = "gef run"
    _syntax_  = "{:s} [GDB_RUN_ARGUMENTS]".format(_cmdline_)

    def __init__(self, *args, **kwargs):
        super().__init__(self._cmdline_, gdb.COMMAND_SUPPORT, gdb.COMPLETE_FILENAME, False)
        return

    def invoke(self, args, from_tty):
        self.dont_repeat()
        if is_alive():
            gdb.execute("continue")
            return

        argv = args.split()
        gdb.execute("gef set args {:s}".format(" ".join(argv)))
        gdb.execute("run")
        return


class GefAlias(gdb.Command):
    """Simple aliasing wrapper because GDB doesn't do what it should."""

    def __init__(self, alias, command, completer_class=gdb.COMPLETE_NONE, command_class=gdb.COMMAND_NONE):
        p = command.split()
        if not p:
            return

        if any(x for x in __aliases__ if x._alias == alias):
            return

        self._command = command
        self._alias = alias
        c = command.split()[0]
        r = self.lookup_command(c)
        self.__doc__ = "Alias for '{}'".format(Color.greenify(command))
        if r is not None:
            _instance = r[2]
            self.__doc__ += ": {}".format(_instance.__doc__)

            if hasattr(_instance,  "complete"):
                self.complete = _instance.complete

        super().__init__(alias, command_class, completer_class=completer_class)
        __aliases__.append(self)
        return

    def invoke(self, args, from_tty):
        gdb.execute("{} {}".format(self._command, args), from_tty=from_tty)
        return

    def lookup_command(self, cmd):
        global gef
        for _name, _class, _instance in gef.instance.loaded_commands:
            if cmd == _name:
                return _name, _class, _instance

        return None

@register_command
class AliasesCommand(GenericCommand):
    """Base command to add, remove, or list aliases."""

    _cmdline_ = "aliases"
    _syntax_  = "{:s} (add|rm|ls)".format(_cmdline_)

    def __init__(self):
        super().__init__(prefix=True)
        return

    def do_invoke(self, argv):
        self.usage()
        return

@register_command
class AliasesAddCommand(AliasesCommand):
    """Command to add aliases."""

    _cmdline_ = "aliases add"
    _syntax_  = "{0} [ALIAS] [COMMAND]".format(_cmdline_)
    _example_ = "{0} scope telescope".format(_cmdline_)

    def __init__(self):
        super().__init__()
        return

    def do_invoke(self, argv):
        if (len(argv) < 2):
            self.usage()
            return
        GefAlias(argv[0], " ".join(argv[1:]))
        return

@register_command
class AliasesRmCommand(AliasesCommand):
    """Command to remove aliases."""

    _cmdline_ = "aliases rm"
    _syntax_ = "{0} [ALIAS]".format(_cmdline_)

    def __init__(self):
        super().__init__()
        return

    def do_invoke(self, argv):
        global __aliases__
        if len(argv) != 1:
            self.usage()
            return
        try:
            alias_to_remove = next(filter(lambda x: x._alias == argv[0], __aliases__))
            __aliases__.remove(alias_to_remove)
        except (ValueError, StopIteration):
            err("{0} not found in aliases.".format(argv[0]))
            return
        gef_print("You must reload GEF for alias removals to apply.")
        return

@register_command
class AliasesListCommand(AliasesCommand):
    """Command to list aliases."""

    _cmdline_ = "aliases ls"
    _syntax_ = _cmdline_

    def __init__(self):
        super().__init__()
        return

    def do_invoke(self, argv):
        ok("Aliases defined:")
        for a in __aliases__:
            gef_print("{:30s} {} {}".format(a._alias, RIGHT_ARROW, a._command))
        return

class GefTmuxSetup(gdb.Command):
    """Setup a confortable tmux debugging environment."""

    def __init__(self):
        super().__init__("tmux-setup", gdb.COMMAND_NONE, gdb.COMPLETE_NONE)
        GefAlias("screen-setup", "tmux-setup")
        return

    def invoke(self, args, from_tty):
        self.dont_repeat()

        tmux = os.getenv("TMUX")
        if tmux:
            self.tmux_setup()
            return

        screen = os.getenv("TERM")
        if screen is not None and screen == "screen":
            self.screen_setup()
            return

        warn("Not in a tmux/screen session")
        return

    def tmux_setup(self):
        """Prepare the tmux environment by vertically splitting the current pane, and
        forcing the context to be redirected there."""
        tmux = which("tmux")
        ok("tmux session found, splitting window...")
        old_ptses = set(os.listdir("/dev/pts"))
        gdb.execute("! {} split-window -h 'clear ; cat'".format(tmux))
        gdb.execute("! {} select-pane -L".format(tmux))
        new_ptses = set(os.listdir("/dev/pts"))
        pty = list(new_ptses - old_ptses)[0]
        pty = "/dev/pts/{}".format(pty)
        ok("Setting `context.redirect` to '{}'...".format(pty))
        gdb.execute("gef config context.redirect {}".format(pty))
        ok("Done!")
        return

    def screen_setup(self):
        """Hackish equivalent of the tmux_setup() function for screen."""
        screen = which("screen")
        sty = os.getenv("STY")
        ok("screen session found, splitting window...")
        fd_script, script_path = tempfile.mkstemp()
        fd_tty, tty_path = tempfile.mkstemp()
        os.close(fd_tty)

        with os.fdopen(fd_script, "w") as f:
            f.write("startup_message off\n")
            f.write("split -v\n")
            f.write("focus right\n")
            f.write("screen bash -c 'tty > {}; clear; cat'\n".format(tty_path))
            f.write("focus left\n")

        gdb.execute("""! {} -r {} -m -d -X source {}""".format(screen, sty, script_path))
        # artificial delay to make sure `tty_path` is populated
        time.sleep(0.25)
        with open(tty_path, "r") as f:
            pty = f.read().strip()
        ok("Setting `context.redirect` to '{}'...".format(pty))
        gdb.execute("gef config context.redirect {}".format(pty))
        ok("Done!")
        os.unlink(script_path)
        os.unlink(tty_path)
        return


def __gef_prompt__(current_prompt):
    """GEF custom prompt function."""

    if gef.config["gef.readline_compat"] is True: return GEF_PROMPT
    if gef.config["gef.disable_color"] is True: return GEF_PROMPT
    if is_alive(): return GEF_PROMPT_ON
    return GEF_PROMPT_OFF


class GefMemoryManager:
    """Class that manages memory access for gef."""

    def write(self, address, buffer, length=0x10):
        """Write `buffer` at address `address`."""
        return gdb.selected_inferior().write_memory(address, buffer, length)

    @lru_cache()
    def read(self, addr, length=0x10):
        """Return a `length` long byte array with the copy of the process memory at `addr`."""
        return gdb.selected_inferior().read_memory(addr, length).tobytes()

    def read_integer(self, addr):
        """Return an integer read from memory."""
        sz = gef.arch.ptrsize
        mem = self.read(addr, sz)
        unpack = u32 if sz == 4 else u64
        return unpack(mem)

    def read_cstring(self, address, max_length=GEF_MAX_STRING_LENGTH, encoding=None):
        """Return a C-string read from memory."""
        encoding = encoding or "unicode-escape"
        length = min(address | (DEFAULT_PAGE_SIZE-1), max_length+1)

        try:
            res_bytes = bytes(self.read(address, length))
        except gdb.error:
            err("Can't read memory at '{}'".format(address))
            return ""
        try:
            with warnings.catch_warnings():
                # ignore DeprecationWarnings (see #735)
                warnings.simplefilter("ignore")
                res = res_bytes.decode(encoding, "strict")
        except UnicodeDecodeError:
            # latin-1 as fallback due to its single-byte to glyph mapping
            res = res_bytes.decode("latin-1", "replace")

        res = res.split("\x00", 1)[0]
        ustr = res.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
        if max_length and len(res) > max_length:
            return "{}[...]".format(ustr[:max_length])

        return ustr

    def read_ascii_string(self, address):
        """Read an ASCII string from memory"""
        cstr = self.read_cstring(address)
        if isinstance(cstr, str) and cstr and all([x in string.printable for x in cstr]):
            return cstr
        return None

class GefHeapManager:

    def __init__(self):
        self.__libc_main_arena = None
        self.__libc_selected_arena = None
        self.__heap_base = None
        return

    @property
    def main_arena(self):
        if not self.__libc_main_arena:
            try:
                self.__libc_main_arena = GlibcArena(search_for_main_arena())
                # the initialization of `main_arena` also defined `selected_arena`, so
                # by default, `main_arena` == `selected_arena`
                self.selected_arena = self.__libc_main_arena
            except:
                # the search for arena can fail when the session is not started
                pass
        return self.__libc_main_arena

    @property
    def selected_arena(self):
        if not self.__libc_selected_arena:
            # `selected_arena` must default to `main_arena`
            self.__libc_selected_arena = self.__libc_default_arena
        return self.__libc_selected_arena

    @selected_arena.setter
    def selected_arena(self, value):
        self.__libc_selected_arena = value
        return

    @property
    def arenas(self):
        if not self.main_arena:
            return []
        return iter(self.__libc_main_arena)

    @property
    def base_address(self):
        if not self.__heap_base:
            self.__heap_base = HeapBaseFunction.heap_base()
        return self.__heap_base

    @property
    def chunks(self):
        if not self.base_address:
            return []
        return iter( GlibcChunk(self.base_address) )


class GefSetting:
    def __init__(self, value, cls = None, description = None):
        self.value = value
        self.type = cls or type(value)
        self.description = description or ""
        return

class GefSettingsManager(dict):
    """
    GefSettings acts as a dict where the global settings are stored and can be read, written or deleted as any other dict.
    For instance, to read a specific command setting: `gef.config[mycommand.mysetting]`
    """
    def __getitem__(self, name):
        try:
            return dict.__getitem__(self, name).value
        except KeyError:
            return None

    def __setitem__(self, name, value):
        # check if the key exists
        if dict.__contains__(self, name):
            # if so, update its value directly
            setting = dict.__getitem__(self, name)
            setting.value = setting.type(value)
            dict.__setitem__(self, name, setting)
        else:
            # if not, `value` must be a GefSetting
            if not isinstance(value, GefSetting): raise Exception("Invalid argument")
            if not value.type: raise Exception("Invalid type")
            if not value.description: raise Exception("Invalid description")
            dict.__setitem__(self, name, value)
        return

    def __delitem__(self, name):
        dict.__setitem__(self, name)
        return

    def raw_entry(self, name):
        return dict.__getitem__(self, name)


class GefSessionManager:
    def __init__(self):
        pass

class Gef:
    """The GEF root class"""
    def __init__(self):
        self.binary = None
        self.arch = GenericArchitecture() # see PR #516, will be reset by `new_objfile_handler`
        self.config = GefSettingsManager()
        return

    def setup(self):
        """
        Setup initialize the runtime setup, which may require for the `gef` to be not None
        """
        self.memory = GefMemoryManager()
        self.heap = GefHeapManager()
        self.instance = GefCommand()
        self.instance.setup()
        tempdir = self.config["gef.tempdir"]
        gef_makedirs(tempdir)
        gdb.execute("save gdb-index {}".format(tempdir))
        return




if __name__ == "__main__":
    if sys.version_info[0] == 2:
        err("GEF has dropped Python2 support for GDB when it reached EOL on 2020/01/01.")
        err("If you require GEF for GDB+Python2, use https://github.com/hugsy/gef-legacy.")

    elif GDB_VERSION < GDB_MIN_VERSION or PYTHON_VERSION < PYTHON_MIN_VERSION:
        err("You're using an old version of GDB. GEF will not work correctly. "
            "Consider updating to GDB {} or higher (with Python {} or higher).".format(".".join(map(str, GDB_MIN_VERSION)), ".".join(map(str, PYTHON_MIN_VERSION))))

    else:
        try:
            pyenv = which("pyenv")
            PYENV_ROOT = gef_pystring(subprocess.check_output([pyenv, "root"]).strip())
            PYENV_VERSION = gef_pystring(subprocess.check_output([pyenv, "version-name"]).strip())
            site_packages_dir = os.path.join(PYENV_ROOT, "versions", PYENV_VERSION, "lib",
                                             "python{}".format(PYENV_VERSION[:3]), "site-packages")
            site.addsitedir(site_packages_dir)
        except FileNotFoundError:
            pass

        # When using a Python virtual environment, GDB still loads the system-installed Python
        # so GEF doesn't load site-packages dir from environment
        # In order to fix it, from the shell with venv activated we run the python binary,
        # take and parse its path, add the path to the current python process using sys.path.extend

        PYTHONBIN = which("python3")
        PREFIX = gef_pystring(subprocess.check_output([PYTHONBIN, '-c', 'import os, sys;print((sys.prefix))'])).strip("\\n")
        if PREFIX != sys.base_prefix:
            SITE_PACKAGES_DIRS = subprocess.check_output(
                [PYTHONBIN, "-c", "import os, sys;print(os.linesep.join(sys.path).strip())"]).decode("utf-8").split()
            sys.path.extend(SITE_PACKAGES_DIRS)

        # setup prompt
        gdb.prompt_hook = __gef_prompt__

        # setup config
        gdb_initial_config = (
            "set confirm off",
            "set verbose off",
            "set pagination off",
            "set print elements 0",
            "set history save on",
            "set history filename ~/.gdb_history",
            "set output-radix 0x10",
            "set print pretty on",
            "set disassembly-flavor intel",
            "handle SIGALRM print nopass",
        )
        for cmd in gdb_initial_config:
            try:
                gdb.execute(cmd)
            except gdb.error:
                pass

        # load GEF
        gef = Gef()
        gef.setup()

        print(gef.arch)
        # gdb events configuration
        gef_on_continue_hook(continue_handler)
        gef_on_stop_hook(hook_stop_handler)
        gef_on_new_hook(new_objfile_handler)
        gef_on_exit_hook(exit_handler)
        gef_on_memchanged_hook(memchanged_handler)
        gef_on_regchanged_hook(regchanged_handler)

        if gdb.current_progspace().filename is not None:
            # if here, we are sourcing gef from a gdb session already attached
            # we must force a call to the new_objfile handler (see issue #278)
            new_objfile_handler(None)

        GefTmuxSetup()
