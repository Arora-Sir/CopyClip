"""Windows-side image codec: hand-rolled PNG encode, WIC-backed decode, DIB<->PNG.

No third-party dependencies: same constraint as winclip.py. PNG encoding is hand-rolled
(IHDR/IDAT/IEND over stdlib zlib) rather than delegated to WIC, because it must be
**deterministic**: encoding the same pixels twice must produce the same bytes, which is
what own-write echo suppression in nodes/windows_node.py relies on (see the image-sync
implementation plan, section 7.3). Decoding arbitrary formats (JPEG, GIF, WEBP, HEIF where
an OS codec exists, ...) genuinely needs a real decoder, so that side uses WIC
(Windows Imaging Component) via raw COM/ctypes.

Every GUID and struct layout below was verified against the actual Windows SDK headers
installed on this machine (10.0.26100.0), not recalled from memory:
    wincodec.h  -> CLSID_WICImagingFactory1, IID_IWICImagingFactory, IWICImagingFactory /
                   IWICBitmapDecoder / IWICBitmapFrameDecode / IWICFormatConverter vtable
                   layouts (slot order matters: these are real COM vtables, not ones we
                   define), GUID_WICPixelFormat32bppBGRA.
    wingdi.h    -> BITMAPV5HEADER field layout, LCS_sRGB (='sRGB' packed big-endian =
                   0x73524742), LCS_GM_IMAGES.
A single wrong hex digit here fails silently or corrupts colors, so this was done by
reading the headers rather than guessing.
"""

from __future__ import annotations

import ctypes
import struct
import threading
import zlib
from ctypes import wintypes

ole32 = ctypes.WinDLL("ole32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

GMEM_MOVEABLE = 0x0002
CLSCTX_INPROC_SERVER = 0x1
COINIT_APARTMENTTHREADED = 0x2


class ImageDecodeError(RuntimeError):
    """WIC failed to decode the given bytes, or a DIB blob was malformed/unsupported."""


# ---------------------------------------------------------------- GUID / COM plumbing

class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _guid(text: str) -> GUID:
    """Parse 'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx' into a GUID structure."""
    h = text.replace("-", "")
    g = GUID()
    g.Data1 = int(h[0:8], 16)
    g.Data2 = int(h[8:12], 16)
    g.Data3 = int(h[12:16], 16)
    g.Data4 = (ctypes.c_ubyte * 8)(*bytes.fromhex(h[16:32]))
    return g


# Verified against wincodec.h: see module docstring.
CLSID_WIC_IMAGING_FACTORY = _guid("cacaf262-9370-4615-a13b-9f5539da4c0a")
IID_IWIC_IMAGING_FACTORY = _guid("ec5ec8a9-c395-4314-9c77-54d7a935ff70")
GUID_WIC_PIXEL_FORMAT_32BPP_BGRA = _guid("6fddc324-4e03-4bfe-b185-3d77768dc90f")

ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
ole32.CoInitializeEx.restype = ctypes.c_long
ole32.CoCreateInstance.argtypes = [
    ctypes.POINTER(GUID), ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p),
]
ole32.CoCreateInstance.restype = ctypes.c_long
ole32.CreateStreamOnHGlobal.argtypes = [
    wintypes.HGLOBAL, wintypes.BOOL, ctypes.POINTER(ctypes.c_void_p),
]
ole32.CreateStreamOnHGlobal.restype = ctypes.c_long

kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalLock.restype = wintypes.LPVOID
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]

# CoInitializeEx is per-thread (COM apartment state lives on the calling thread), so the
# "already initialized" flag must be thread-local too: a module-global bool here would let
# one thread's CoInitializeEx call satisfy the check for every other thread, leaving those
# threads' COM apartments genuinely uninitialized and CoCreateInstance failing with
# CO_E_NOTINITIALIZED. decode() is called from multiple long-lived threads (the clipboard
# watcher thread and each device's pump thread), so this isn't a hypothetical.
_com_state = threading.local()


def _ensure_com() -> None:
    if getattr(_com_state, "initialized", False):
        return
    hr = ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
    # S_OK=0 or S_FALSE=1 (already initialized on this thread) are both fine.
    if hr not in (0, 1):
        raise ImageDecodeError(f"CoInitializeEx failed: 0x{hr & 0xFFFFFFFF:08X}")
    _com_state.initialized = True


def _hr_check(hr: int, what: str) -> None:
    if hr < 0:
        raise ImageDecodeError(f"{what} failed: HRESULT 0x{hr & 0xFFFFFFFF:08X}")


class _Com:
    """Thin COM interface pointer: calls a vtable slot by index.

    Genuine real-world COM objects, not ones we implement: the slot index must match
    the interface's declared method order exactly (QueryInterface/AddRef/Release are
    always 0/1/2; everything after is declaration order, inherited methods first). Every
    index used below was cross-checked against wincodec.h: see module docstring.
    """

    __slots__ = ("ptr",)

    def __init__(self, ptr: ctypes.c_void_p) -> None:
        self.ptr = ptr

    def _vtbl(self) -> int:
        return ctypes.cast(self.ptr, ctypes.POINTER(ctypes.c_void_p))[0]

    def call(self, index: int, restype, argtypes: list, *args):
        func_addr = ctypes.cast(self._vtbl(), ctypes.POINTER(ctypes.c_void_p))[index]
        proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
        return proto(func_addr)(self.ptr, *args)

    def release(self) -> None:
        if self.ptr:
            self.call(2, ctypes.c_ulong, [])
            self.ptr = None


# ---------------------------------------------------------------- decode (WIC)

def decode(data: bytes) -> tuple[bytes, int, int]:
    """Decode arbitrary image bytes (PNG/JPEG/GIF/BMP/TIFF, plus WEBP/HEIF where an OS
    codec is installed) into (bgra_bytes, width, height), straight (non-premultiplied)
    alpha, top-down, no row padding. Raises ImageDecodeError on any failure: callers
    must treat that as "skip this image", never crash the daemon.
    """
    if not data:
        raise ImageDecodeError("empty input")
    _ensure_com()

    hglobal = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not hglobal:
        raise ImageDecodeError("GlobalAlloc failed")
    ptr = kernel32.GlobalLock(hglobal)
    if not ptr:
        kernel32.GlobalFree(hglobal)
        raise ImageDecodeError("GlobalLock failed")
    ctypes.memmove(ptr, data, len(data))
    kernel32.GlobalUnlock(hglobal)

    stream_ptr = ctypes.c_void_p()
    # fDeleteOnRelease=True: the stream now owns hglobal's lifetime; we must not free it.
    hr = ole32.CreateStreamOnHGlobal(hglobal, True, ctypes.byref(stream_ptr))
    if hr < 0:
        kernel32.GlobalFree(hglobal)
    _hr_check(hr, "CreateStreamOnHGlobal")
    stream = _Com(stream_ptr)

    factory_ptr = ctypes.c_void_p()
    hr = ole32.CoCreateInstance(
        ctypes.byref(CLSID_WIC_IMAGING_FACTORY), None, CLSCTX_INPROC_SERVER,
        ctypes.byref(IID_IWIC_IMAGING_FACTORY), ctypes.byref(factory_ptr))
    _hr_check(hr, "CoCreateInstance(WICImagingFactory)")
    factory = _Com(factory_ptr)

    try:
        decoder_ptr = ctypes.c_void_p()
        # slot 4 = IWICImagingFactory::CreateDecoderFromStream
        hr = factory.call(
            4, ctypes.c_long,
            [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)],
            stream.ptr, None, 0, ctypes.byref(decoder_ptr))
        _hr_check(hr, "CreateDecoderFromStream (unrecognized or corrupt image format)")
        decoder = _Com(decoder_ptr)
        try:
            frame_ptr = ctypes.c_void_p()
            # slot 13 = IWICBitmapDecoder::GetFrame
            hr = decoder.call(
                13, ctypes.c_long, [ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)],
                0, ctypes.byref(frame_ptr))
            _hr_check(hr, "GetFrame")
            frame = _Com(frame_ptr)
            try:
                converter_ptr = ctypes.c_void_p()
                # slot 10 = IWICImagingFactory::CreateFormatConverter
                hr = factory.call(10, ctypes.c_long, [ctypes.POINTER(ctypes.c_void_p)],
                                   ctypes.byref(converter_ptr))
                _hr_check(hr, "CreateFormatConverter")
                converter = _Com(converter_ptr)
                try:
                    # slot 8 = IWICFormatConverter::Initialize. frame's IWICBitmapSource
                    # vtable prefix is layout-identical (single inheritance, no offset),
                    # so passing frame.ptr directly as pISource is valid without QI.
                    hr = converter.call(
                        8, ctypes.c_long,
                        [ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.c_int,
                         ctypes.c_void_p, ctypes.c_double, ctypes.c_int],
                        frame.ptr, ctypes.byref(GUID_WIC_PIXEL_FORMAT_32BPP_BGRA), 0,
                        None, 0.0, 0)
                    _hr_check(hr, "FormatConverter.Initialize")

                    w, h = ctypes.c_uint(0), ctypes.c_uint(0)
                    # slot 3 = IWICBitmapSource::GetSize
                    hr = converter.call(
                        3, ctypes.c_long,
                        [ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint)],
                        ctypes.byref(w), ctypes.byref(h))
                    _hr_check(hr, "GetSize")
                    width, height = w.value, h.value
                    if width == 0 or height == 0:
                        raise ImageDecodeError("decoded image has zero dimensions")

                    stride = width * 4
                    buf_size = stride * height
                    buf = (ctypes.c_ubyte * buf_size)()
                    # slot 7 = IWICBitmapSource::CopyPixels
                    hr = converter.call(
                        7, ctypes.c_long,
                        [ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint,
                         ctypes.POINTER(ctypes.c_ubyte)],
                        None, stride, buf_size, buf)
                    _hr_check(hr, "CopyPixels")
                    return bytes(buf), width, height
                finally:
                    converter.release()
            finally:
                frame.release()
        finally:
            decoder.release()
    finally:
        factory.release()
        stream.release()


# ---------------------------------------------------------------- PNG encode (hand-rolled)

_PNG_SIG = b"\x89PNG\r\n\x1a\n"


def _chunk(tag: bytes, payload: bytes) -> bytes:
    return (struct.pack(">I", len(payload)) + tag + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))


def png_from_bgra(bgra: bytes, width: int, height: int, has_alpha: bool = True) -> bytes:
    """Encode raw top-down BGRA pixel bytes (4 bytes/pixel, no row padding) as a PNG.

    Deterministic by construction: filter type 0 (None) on every scanline and a fixed
    zlib level, so re-encoding identical pixels always produces identical bytes byte for
    byte. That determinism is load-bearing for own-write echo suppression in
    nodes/windows_node.py: a re-encode that produced different bytes would produce a
    different hash and the write would never recognise itself, looping forever.
    """
    if len(bgra) != width * height * 4:
        raise ValueError(f"bgra buffer size {len(bgra)} does not match {width}x{height}x4")

    buf = bytearray(bgra)
    # BGRA -> RGBA in place: swap the B (offset 0) and R (offset 2) byte of every pixel.
    # Extended-slice assignment is a single C-level operation over the whole buffer, not
    # a width*height Python loop.
    buf[0::4], buf[2::4] = buf[2::4], buf[0::4]

    if has_alpha:
        color_type, bpp = 6, 4  # PNG color type 6 = truecolor with alpha
        pixels = buf
    else:
        color_type, bpp = 2, 3  # PNG color type 2 = truecolor
        del buf[3::4]            # drop the alpha byte -> tightly packed RGB
        pixels = buf

    row_stride = width * bpp
    raw = bytearray(height * (row_stride + 1))
    for y in range(height):
        off = y * (row_stride + 1)
        raw[off] = 0  # filter type 0 (None)
        raw[off + 1: off + 1 + row_stride] = pixels[y * row_stride: (y + 1) * row_stride]

    ihdr = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    idat = zlib.compress(bytes(raw), 6)
    return _PNG_SIG + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")


# ---------------------------------------------------------------- DIB <-> PNG

_BI_RGB = 0
_BI_BITFIELDS = 3


def dib_to_png(dib: bytes) -> bytes:
    """Convert a CF_DIB/CF_DIBV5 blob (as returned by GetClipboardData) to PNG bytes.

    Handles BITMAPINFOHEADER (biSize=40) and BITMAPV4/V5HEADER (the width/height/
    planes/bitcount/compression fields are a common prefix across all three, per
    wingdi.h). Only 24bpp and 32bpp are handled: CopyClip's use case is screenshots and
    photos, which are always one of these; paletted (<=8bpp) DIBs are out of scope.
    """
    if len(dib) < 40:
        raise ImageDecodeError("DIB too small to contain BITMAPINFOHEADER")

    (bi_size,) = struct.unpack_from("<I", dib, 0)
    bi_width, bi_height_signed, _planes, bi_bit_count, bi_compression = \
        struct.unpack_from("<iiHHI", dib, 4)

    width = bi_width
    top_down = bi_height_signed < 0
    height = -bi_height_signed if top_down else bi_height_signed
    if width <= 0 or height <= 0:
        raise ImageDecodeError(f"invalid DIB dimensions {width}x{height}")

    pixel_offset = bi_size
    if bi_compression == _BI_BITFIELDS and bi_size == 40:
        # A plain BITMAPINFOHEADER doesn't embed mask fields; three DWORD masks (R,G,B)
        # follow it before pixel data. BITMAPV4/V5HEADER already embed their own masks.
        pixel_offset += 12
    elif bi_compression not in (_BI_RGB, _BI_BITFIELDS):
        raise ImageDecodeError(f"unsupported DIB compression {bi_compression}")

    if bi_bit_count not in (24, 32):
        raise ImageDecodeError(f"unsupported DIB bit depth {bi_bit_count} (only 24/32bpp)")

    bytes_per_px = bi_bit_count // 8
    row_stride = ((width * bytes_per_px + 3) // 4) * 4  # DIB rows pad to a 4-byte boundary
    needed = pixel_offset + row_stride * height
    if len(dib) < needed:
        raise ImageDecodeError(f"DIB truncated: need {needed} bytes, have {len(dib)}")

    bgra = bytearray(width * height * 4)
    for y in range(height):
        src_row = y if top_down else (height - 1 - y)
        src_off = pixel_offset + src_row * row_stride
        row = dib[src_off: src_off + width * bytes_per_px]
        dst_off = y * width * 4
        if bytes_per_px == 4:
            bgra[dst_off: dst_off + width * 4] = row
        else:  # 24bpp B,G,R triples -> B,G,R,255 (opaque)
            out = bytearray(width * 4)
            out[0::4] = row[0::3]
            out[1::4] = row[1::3]
            out[2::4] = row[2::3]
            out[3::4] = bytes([255]) * width
            bgra[dst_off: dst_off + width * 4] = out

    return png_from_bgra(bytes(bgra), width, height, has_alpha=(bytes_per_px == 4))


def png_to_dibv5(bgra: bytes, width: int, height: int) -> bytes:
    """Build a BITMAPV5HEADER + bottom-up 32bpp BGRA pixel blob, ready for
    SetClipboardData(CF_DIBV5, ...). Alpha is preserved via an explicit BI_BITFIELDS
    alpha mask: this is the fix for the alpha-destroying bug found in ClipCascade's
    Windows paste path (`.convert("RGB")` before SetClipboardData(CF_DIB, ...)), which
    the image-sync implementation plan explicitly calls out as not to be repeated.

    Field layout verified against wingdi.h's BITMAPV5HEADER (see module docstring); BGRA
    byte order already matches the R=0x00FF0000/G=0x0000FF00/B=0x000000FF/A=0xFF000000
    little-endian mask layout with no reordering, so only the row order needs flipping.
    """
    if len(bgra) != width * height * 4:
        raise ValueError(f"bgra buffer size {len(bgra)} does not match {width}x{height}x4")

    LCS_sRGB = 0x73524742
    LCS_GM_IMAGES = 4

    header = struct.pack(
        "<IiiHHIIiiIIIIIII9iIIIIIII",
        124,                      # bV5Size
        width, height,            # bV5Width, bV5Height (positive = bottom-up)
        1,                        # bV5Planes
        32,                       # bV5BitCount
        _BI_BITFIELDS,            # bV5Compression
        width * height * 4,       # bV5SizeImage
        0, 0,                     # bV5XPelsPerMeter, bV5YPelsPerMeter
        0, 0,                     # bV5ClrUsed, bV5ClrImportant
        0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000,  # R, G, B, A masks
        LCS_sRGB,                 # bV5CSType
        *([0] * 9),               # bV5Endpoints (ignored under LCS_sRGB)
        0, 0, 0,                  # bV5GammaRed/Green/Blue (ignored under LCS_sRGB)
        LCS_GM_IMAGES,            # bV5Intent
        0, 0, 0,                  # bV5ProfileData, bV5ProfileSize, bV5Reserved
    )

    row_stride = width * 4
    pixels = bytearray(row_stride * height)
    for y in range(height):
        src_row = height - 1 - y
        pixels[y * row_stride: (y + 1) * row_stride] = \
            bgra[src_row * row_stride: (src_row + 1) * row_stride]

    return header + bytes(pixels)


def png_to_dib(bgra: bytes, width: int, height: int) -> bytes:
    """Build a standard BITMAPINFOHEADER and bottom-up 32bpp BGRA pixel blob for SetClipboardData(CF_DIB, ...).

    Applications such as Chrome, WhatsApp, Discord, Slack, and Paint specifically query CF_DIB.
    Providing CF_DIB directly avoids Windows shell dynamic synthesis lag when handling Ctrl+V.
    """
    if len(bgra) != width * height * 4:
        raise ValueError(f"bgra buffer size {len(bgra)} does not match {width}x{height}x4")

    header = struct.pack(
        "<IiiHHIIiiII",
        40,                 # biSize
        width, height,      # biWidth, biHeight (positive = bottom-up)
        1,                  # biPlanes
        32,                 # biBitCount
        _BI_RGB,            # biCompression (BI_RGB = 0 for standard uncompressed 32bpp BGRA)
        width * height * 4, # biSizeImage
        0, 0,               # biXPelsPerMeter, biYPelsPerMeter
        0, 0,               # biClrUsed, biClrImportant
    )

    row_stride = width * 4
    pixels = bytearray(row_stride * height)
    for y in range(height):
        src_row = height - 1 - y
        pixels[y * row_stride: (y + 1) * row_stride] = \
            bgra[src_row * row_stride: (src_row + 1) * row_stride]

    return header + bytes(pixels)

