"""BinaryView for UEFI Terse Executables
"""

import glob
import os
import struct
from binaryninja import BinaryView, platform, SegmentFlag, SectionSemantics, Symbol, SymbolType

TERSE_IMAGE_HEADER_SIZE = 40
SECTION_HEADER_SIZE = 40

class TerseExecutableView(BinaryView):
    """This class implements the BinaryView for Terse Executables
    """

    name = 'TE'
    long_name = 'Terse Executable'

    def __init__(self, data: bytes):
        BinaryView.__init__(self, parent_view=data, file_metadata=data.file)
        self.raw = data

    @classmethod
    def is_valid_for_data(cls, data: bytes) -> bool:
        """Determine if the loaded binary is a Terse Executable

        :param data: Raw binary data
        :return: True if the binary is a TE, otherwise False
        """

        if data.length < TERSE_IMAGE_HEADER_SIZE:
            return False

        if data[0:2].decode('utf-8', 'replace') != 'VZ':
            return False

        return True

    def _set_platform(self, machine_type: int):
        """Set platform/architecture from machine type

        :param machine_type: Machine type from TE header
        """

        if machine_type == 332:
            self.platform = platform.Platform['windows-x86']
        elif machine_type == -31132:
            self.platform = platform.Platform['windows-x86_64']
        elif machine_type == -21916:
            self.platform = platform.Platform['windows-aarch64']

    def _create_segments(self, image_base: int, num_of_sections: int):
        """There's really only one segment in a TE and it's RWX. However, we set the header to read only jsut to make
        sure it isn't disassembled as code.

        :param image_base: Virtual base address
        :param num_of_sections: Number of sections (for header region size calculation)
        """

        headers_size = TERSE_IMAGE_HEADER_SIZE + num_of_sections * SECTION_HEADER_SIZE
        self.add_auto_segment(image_base, headers_size, 0, headers_size, SegmentFlag.SegmentReadable)
        code_region_size = len(self.raw)-headers_size
        self.add_auto_segment(image_base+headers_size, code_region_size, headers_size, code_region_size,
                              SegmentFlag.SegmentReadable|SegmentFlag.SegmentWritable|SegmentFlag.SegmentExecutable)

    def _create_sections(self, image_base: int, num_of_sections: int):
        """Create sections

        :param image_base: Virtual base address
        :param num_of_sections: Number of sections
        """

        base = TERSE_IMAGE_HEADER_SIZE
        for _ in range(0, num_of_sections):
            name = self.raw[base:base+8].decode('utf-8')
            virtual_size = struct.unpack('<I', self.raw[base+8:base+12])[0]
            virtual_addr = struct.unpack('<I', self.raw[base+12:base+16])[0]

            # UEFI helper will change the section semantics to ReadWriteDataSectionSemantics, but in order for linear
            # sweep to run over our sections we need the semantics to be ReadOnlyCodeSectionSemantics (for now)
            self.add_auto_section(name, image_base+virtual_addr, virtual_size,
                                  SectionSemantics.ReadOnlyCodeSectionSemantics)
            base += SECTION_HEADER_SIZE

    def _apply_header_types(self, image_base: int, num_of_sections: int):
        """Import and apply the TE header and section header types

        :param image_base: Virtual base address
        :param num_of_sections: Number of sections (for header region size calculation)
        """

        t, name = self.parse_type_string(
            """struct {
             uint32_t VirtualAddress;
             uint32_t Size;
            } EFI_IMAGE_DATA_DIRECTORY;""")

        self.define_user_type(name, t)
        header, name = self.parse_type_string(
            """struct {
             char Signature[2];
             uint16_t Machine;
             uint8_t NumberOfSections;
             uint8_t Subsystem;
             uint16_t StrippedSize;
             uint32_t AddressOfEntryPoint;
             uint32_t BaseOfCode;
             uint64_t ImageBase;
             EFI_IMAGE_DATA_DIRECTORY DataDirectory[2];
            } EFI_TE_IMAGE_HEADER;""")
        self.define_user_type(name, header)
        section_header, name = self.parse_type_string(
            """struct {
                char Name[8];
                union {
                    uint32_t  PhysicalAddress;
                    uint32_t  VirtualSize;
                } Misc;
                uint32_t  VirtualAddress;
                uint32_t  SizeOfRawData;
                uint32_t  PointerToRawData;
                uint32_t  PointerToRelocations;
                uint32_t  PointerToLinenumbers;
                uint16_t  NumberOfRelocations;
                uint16_t  NumberOfLinenumbers;
                uint32_t  Characteristics;
            } EFI_IMAGE_SECTION_HEADER;""")
        self.define_user_type(name, section_header)
        self.define_user_data_var(image_base, header)
        self.define_user_symbol(Symbol(SymbolType.DataSymbol, image_base, 'gTEImageHdr'))

        for i in range(TERSE_IMAGE_HEADER_SIZE, num_of_sections*(SECTION_HEADER_SIZE+1), SECTION_HEADER_SIZE):
            self.define_user_data_var(image_base+i, section_header)
            self.define_user_symbol(Symbol(SymbolType.DataSymbol, image_base+i, 'gSectionHdr{}'.format(i-40)))

    def _import_types_from_headers(self):
        """Parse EDKII types from header files
        """

        dirname = os.path.dirname(os.path.abspath(__file__))
        hdrs_path = os.path.join(dirname, 'headers')
        headers = glob.glob(os.path.join(hdrs_path, '*.h'))
        for hdr in headers:
            _types = self.platform.parse_types_from_source_file(hdr)
            for name, _type in _types.types.items():
                self.define_user_type(name, _type)

    def _set_entry_point_prototype(self, addr):
        """Apply correct prototype to the module entry point

        :param addr: Address of the entry point
        """

        self.add_entry_point(addr)
        _start = self.get_function_at(addr)
        _start.function_type = (
            'EFI_STATUS ModuleEntryPoint(EFI_PEI_FILE_HANDLE FileHandle, EFI_PEI_SERVICES **PeiServices)'
        )

    def init(self):
        """Assign the platform, create segments, create sections, and set the entrypoint
        """

        machine = struct.unpack('<H', self.raw[2:4])[0]
        self._set_platform(machine)

        stripped_size = struct.unpack('<H', self.raw[6:8])[0]
        header_ofs = stripped_size - TERSE_IMAGE_HEADER_SIZE
        image_base = struct.unpack('<Q', self.raw[16:24])[0]
        num_of_sections = ord(self.raw[4])

        self._create_segments(image_base+header_ofs, num_of_sections)
        self._create_sections(image_base, num_of_sections)
        self._apply_header_types(image_base+header_ofs, num_of_sections)

        self._import_types_from_headers()
        entry_addr = struct.unpack('<I', self.raw[8:12])[0] + image_base
        self._set_entry_point_prototype(entry_addr)
        return True

    # pylint: disable=no-self-use
    def perform_is_executable(self) -> bool:
        """Terse Executables are executable, return true

        :return: True
        """

        return True

    def perform_get_entry_point(self) -> int:
        """Determine the address of the entry point function

        :return: Address of the entry point
        """
        image_base = struct.unpack('<Q', self.raw[16:24])[0]
        entry = struct.unpack('<I', self.raw[8:12])[0]
        return image_base+entry
