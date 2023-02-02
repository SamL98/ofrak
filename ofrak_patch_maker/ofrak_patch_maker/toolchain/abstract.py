"""
Toolchain - Python driver for compiler, linker, assembler, and binutils that generates linker scripts
    for use with [PatchMaker][ofrak_patch_maker.patch_maker.PatchMaker].
"""
import logging
import os
import subprocess
from abc import ABC, abstractmethod
from os.path import join, split
from typing import Dict, Iterable, List, Optional, Tuple, Mapping

from ofrak_type import ArchInfo
from ofrak_patch_maker.binary_parser.abstract import AbstractBinaryFileParser
from ofrak_patch_maker.toolchain.model import Segment, ToolchainConfig
from ofrak_patch_maker.toolchain.utils import get_repository_config
from ofrak_type.architecture import InstructionSet
from ofrak_type.bit_width import BitWidth
from ofrak_type.memory_permissions import MemoryPermissions

RBS_AUTOGEN_WARNING = (
    "/*\n"
    "*\n"
    "* WARNING: DO NOT EDIT THIS FILE MANUALLY\n"
    "*      This is an autogenerated file\n"
    "*\n"
    "*/\n\n"
)


class Toolchain(ABC):
    binary_file_parsers: List[AbstractBinaryFileParser] = []

    def __init__(
        self,
        processor: ArchInfo,
        toolchain_config: ToolchainConfig,
        logger: logging.Logger = logging.getLogger(),
    ):
        """
        Responsible for building provided patch source. Also responsible for all flag, and linker
        script syntax implementations.

        `Toolchain` instances should be considered stateless after initialization; they can only
        be configured once.

        Only certain, very specific flags may be appended at runtime. For instance,
        the output value of `--Map={PATH}` will change for every distinct executable that is
        generated in a project.

        Generally, compiler flags should never, ever change throughout the lifetime of a `Toolchain`
        instance.

        :param processor: hardware description
        :param toolchain_config: assembler, compiler, linker options
        :param logger:

        :raises ValueError: if a binary parser doesn't exist for the filetype provided in the
            [ToolchainConfig][ofrak_patch_maker.toolchain.model.ToolchainConfig]
        """
        self._processor = processor

        self.file_format = toolchain_config.file_format

        self._parser: AbstractBinaryFileParser = None  # type: ignore
        for parser in self.binary_file_parsers:
            if parser.file_format is toolchain_config.file_format:
                self._parser = parser
                break
        if self._parser is None:
            raise ValueError(
                f"No binary file parser found for format " f"{toolchain_config.file_format.name}!"
            )

        self._preprocessor_flags: List[str] = []
        self._compiler_flags: List[str] = []
        self._assembler_flags: List[str] = []
        self._linker_flags: List[str] = []
        self._config = toolchain_config
        self._logger = logger

        # The keep_list should only contain FUNCTIONALLY important sections
        # (not empty .got.plt, for instance).
        # TODO: Come up with a better system to handle this...
        self._linker_keep_list = [".data", ".rodata", ".text", ".rel"]
        self._linker_discard_list = [
            ".gnu.hash",
            ".comment",
            ".ARM.attributes",
            ".dynamic",
            ".ARM.exidx",
            ".hash",
            ".dynsym",
            ".dynstr",
            ".eh_frame",
        ]

        self._assembler_target = self._get_assembler_target(processor)
        self._compiler_target = self._get_compiler_target(processor)

    @property
    @abstractmethod
    def name(self) -> str:
        """
        :return str: name property that matches the value used in `toolchain.conf` to access paths
        """
        raise NotImplementedError()

    @abstractmethod
    def _get_assembler_target(self, processor: ArchInfo) -> str:
        """
        Red Balloon Security strongly recommends all users provide their specific hardware target
        for best results.

        :param processor:

        :raises PatchMakerException: if no target provided and program attributes do not correspond
            to a default value.
        :return str: a default assembler target for the provided processor unless one is provided
            in `self._config`.
        """
        raise NotImplementedError()

    @abstractmethod
    def _get_compiler_target(self, processor: ArchInfo) -> Optional[str]:
        """
        Returns a default compiler target for the provided processor unless one is provided
        in `self._config`.

        Red Balloon Security strongly recommends all users provide their specific hardware target
        for best results.

        :param processor:

        :return str:
        """
        raise NotImplementedError()

    @property
    def _assembler_path(self) -> str:
        """
        Provides path to installed assembler given the ISA.

        :raises NotImplementedError: if an assembler for that ISA does not exist
        :returns: filepath to the assembler program
        """
        if self._processor.isa == InstructionSet.M68K:
            assembler_path = "M68K_ASM_PATH"
        elif (
            self._processor.isa == InstructionSet.X86
            and self._processor.bit_width == BitWidth.BIT_64
        ):
            assembler_path = "X86_64_ASM_PATH"
        else:
            assembler_path = f"{self._processor.isa.value.upper()}_ASM_PATH"
        return get_repository_config("ASM", assembler_path)

    @property
    def _preprocessor_path(self) -> str:
        """
        :return str: path to the toolchain preprocessor - this is usually the compiler.
        """
        return get_repository_config(self.name, "PREPROCESSOR")

    @property
    def _compiler_path(self) -> str:
        """
        :return str: path to the toolchain compiler
        """
        return get_repository_config(self.name, "COMPILER")

    @property
    def _linker_path(self) -> str:
        """
        :return str: path to the toolchain linker
        """
        return get_repository_config(self.name, "LINKER")

    @property
    def _readobj_path(self) -> str:
        """
        :return str: path to the toolchain binary analysis utility
        """
        return get_repository_config(self.name, "BIN_PARSER")

    @property
    def _lib_path(self) -> str:
        """
        :return str: path to the toolchain libraries
        """
        return get_repository_config(self.name, "LIB")

    @property
    @abstractmethod
    def _linker_script_flag(self) -> str:
        """
        :return str: the linker script flag for this toolchain, usually `-T`
        """
        raise NotImplementedError()

    def is_userspace(self) -> bool:
        """
        Provides whether the toolchain is configured for userspace patch generation.

        :return bool:
        """
        return self._config.userspace_dynamic_linker is not None

    def is_relocatable(self) -> bool:
        """
        Provides whether the toolchain is configured for relocatable patch generation.

        This often means hard failures when trying to assemble instructions that branch to absolute
        values.

        :return bool:
        """
        return self._config.relocatable

    @property
    @abstractmethod
    def segment_alignment(self) -> int:
        """
        For example, x86 returns 16. This will most often be used when programmatically allocating
        memory for code/data.

        :return int: required alignment factor for the toolchain/ISA
        """
        raise NotImplementedError()

    def _execute_tool(
        self,
        tool_path: str,
        flags: List[str],
        in_files: List[str],
        out_file: Optional[str] = None,
        env: Dict[str, str] = None,
    ) -> str:
        """
        Utility function used to invoke the toolchain subprocess we use.

        :param tool_path: path
        :param flags: various CLI flags
        :param in_files: input files, usually positional
        :param out_file:
        :param env: environment variables and stringified values to append to the existing
        environment.

        :return str: `stdout` of the subprocess call
        """
        final_flags = []
        final_flags.extend(flags)

        if out_file is not None:
            final_flags.extend([f"-o{out_file}"])

        args = [tool_path] + final_flags + in_files
        self._logger.info(" ".join(args))
        try:
            if env:
                my_env = os.environ.copy()
                my_env.update(env)
                self._logger.info(f"With env: {my_env}")
                proc = subprocess.run(
                    args, stdout=subprocess.PIPE, encoding="utf-8", check=True, env=my_env
                )
            else:
                proc = subprocess.run(args, stdout=subprocess.PIPE, encoding="utf-8", check=True)
        except subprocess.CalledProcessError as e:
            cmd = " ".join(args)
            raise ValueError(f'Command "{cmd}" returned non-zero exit status {e.returncode}')

        return proc.stdout

    def preprocess(self, source_file: str, header_dirs: List[str], out_dir: str = ".") -> str:
        """
        Runs the Toolchain's C preprocessor on the input file.

        :return str: path to the original source file
        """
        out_file = join(out_dir, split(source_file)[-1] + ".p")

        self._execute_tool(
            self._preprocessor_path,
            self._preprocessor_flags,
            [source_file] + ["-I" + x for x in header_dirs],
            out_file=out_file,
        )
        return os.path.abspath(out_file)

    def compile(self, c_file: str, header_dirs: List[str], out_dir: str = ".") -> str:
        """
        Runs the Toolchain's C compiler on the input file.

        :return str: path to the object file
        """
        out_file = join(out_dir, split(c_file)[-1] + ".o")

        self._execute_tool(
            self._compiler_path,
            self._compiler_flags,
            [c_file] + ["-I" + x for x in header_dirs],
            out_file=out_file,
        )

        return os.path.abspath(out_file)

    def assemble(self, asm_file: str, header_dirs: List[str], out_dir: str = "."):
        """
        Runs the Toolchain's assembler on the input file

        :return str: path to the object file
        """
        out_file = join(out_dir, split(asm_file)[-1] + ".o")

        self._execute_tool(
            self._assembler_path,
            self._assembler_flags,
            [asm_file] + ["-I" + x for x in header_dirs],
            out_file=out_file,
        )

        return os.path.abspath(out_file)

    @staticmethod
    @abstractmethod
    def _get_linker_map_flag(exec_path: str) -> Iterable[str]:
        """
        Generates the linker map file flag for a linker invocation given the executable path.

        :param exec_path: path to executable

        :return str: path to map file
        """
        raise NotImplementedError()

    def link(self, o_files: List[str], exec_path: str, script: str = None):
        """
        Run's the `Toolchain`'s linker on the input object files.

        :param o_files: list of object files to be linked
        :param exec_path: path to executable output file
        :param script: path to linker script (usually an `.ld` file)
        """
        flags = []
        flags.extend(self._linker_flags)

        if script is not None:
            flags.append(self._linker_script_flag + script)

        if self._config.create_map_files:
            flags.extend(self._get_linker_map_flag(exec_path))

        self._execute_tool(self._linker_path, flags, o_files, out_file=exec_path)

    @staticmethod
    def linker_include_filter(symbol_name: str) -> bool:
        return "." in symbol_name or "_DYNAMIC" in symbol_name

    def keep_section(self, section_name: str) -> bool:
        if self._config.separate_data_sections:
            raise NotImplementedError("you must override keep_section() in your Toolchain sublass")
        else:
            return section_name in self._linker_keep_list

    @abstractmethod
    def generate_linker_include_file(self, symbols: Mapping[str, int], out_path: str) -> str:
        """
        This utility function receives the generated symbols dictionary that results
        from preprocessing a firmware image and generates a `.inc` file for use
        with linker scripts, enabling direct function calls when using the complete
        cross compilation toolchain.

        This functionality must be defined for each toolchain given potential
        syntactical differences.

        :param symbols: mappings of symbol string to effective address
        :param out_path: the path to the resulting symbol include file (usually `.inc`)

        :return str: returns out_path
        """
        raise NotImplementedError()

    @abstractmethod
    def add_linker_include_values(self, symbols: Dict[str, int], path: str):
        """
        Adds linker include entries to a provided file (usually ending in `.inc`).

        For example GNU syntax prescribes `PROVIDE(name = 0xdeadbeef);`.

        :param symbols: mapping of symbol string to effective address
        :param path: path to the provided linker include file.
        """
        raise NotImplementedError()

    @abstractmethod
    def ld_generate_region(
        self,
        object_path: str,
        segment_name: str,
        permissions: MemoryPermissions,
        vm_address: int,
        length: int,
    ) -> Tuple[str, str]:
        """
        Generates regions for linker scripts.

        :return str: a string entry for a "memory region" for the toolchain in question.
        """
        raise NotImplementedError()

    @abstractmethod
    def ld_generate_bss_region(self, vm_address: int, length: int) -> Tuple[str, str]:
        """
        Generates `.bss` regions for linker scripts.

        :return str: a `.bss` memory entry string for the toolchain in question.
        """
        raise NotImplementedError()

    @staticmethod
    @abstractmethod
    def ld_generate_section(object_path: str, segment_name: str, memory_region_name: str) -> str:
        """
        Generates sections for linker scripts.

        :return str: a string entry for a "section" for the toolchain in question.
        """
        raise NotImplementedError()

    @staticmethod
    @abstractmethod
    def ld_generate_bss_section(memory_region_name: str) -> str:
        """
        Generates `.bss` sections for linker scripts.

        :return str: a `.bss` section entry string for the toolchain in question.
        """
        raise NotImplementedError()

    def ld_generate_placeholder_reloc_sections(self) -> Tuple[Iterable[str], Iterable[str]]:
        """
        Implements functionality to generate additional regions and sections required as
        placeholders for certain toolchains during the link process when compiling relocatable code.

        For instance, GNU seems to create `.got.plt` and `.rel.dyn` temporarily during link. These
        sections require memory regions that will take them during the link process even if the
        linker ends up leaving them out of the final executable.

        We can mock them with the resulting regions/sections.

        :return Tuple[Iterable[str], Iterable[str]]: (memory regions, sections)
        """
        return [], []

    @abstractmethod
    def ld_script_create(
        self,
        name: str,
        memory_regions: List[str],
        sections: List[str],
        build_dir: str,
        symbol_files: List[str],
    ) -> str:
        """
        Constructs the linker script for the concrete toolchain class in use.

        Uses the provided name, memory region strings, section strings, symbol files,
        expected entrypoint (if any) to generate a linker script that results in a valid
        FEM object when used within `link`.

        :param name:
        :param memory_regions:
        :param sections:
        :param build_dir:
        :param symbol_files:

        :return str: path to the generated linker script
        """
        raise NotImplementedError()

    @abstractmethod
    def get_bin_file_symbols(self, executable_path: str) -> Dict[str, int]:
        """
        For now, this utility only searches for global function and data symbols which are
        actually contained in a section in the file, as opposed to symbols which are referenced
        but undefined.

        :param executable_path: path to the program to be analyzed for symbols

        :return Dict[str, int]: mapping of symbol string to effective address.
        """
        raise NotImplementedError()

    @abstractmethod
    def get_bin_file_segments(self, path: str) -> Tuple[Segment, ...]:
        """
        Parses all segments found in the executable path provided.

        :param path: path to the program to be analyzed for symbols

        :return Tuple[Segment, ...]: Tuple of [Segment][ofrak_patch_maker.toolchain.model.Segment]
        objects
        """
        raise NotImplementedError()
