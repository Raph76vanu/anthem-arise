"""Anthem-generation Frostbite texture resource decoding.

Texture ``.res`` records contain a small descriptor while the mip payload is
stored in a separate chunk.  This module parses that descriptor and wraps the
chunk in a standard DDS header so Pillow can decode the GPU-compressed data.
"""
from __future__ import annotations

import io
import math
import struct
import uuid
from dataclasses import dataclass

from PIL import Image


TEXTURE_RESOURCE_TYPE = 0x6BDE20BA
TEXTURE_DESCRIPTOR_SIZE = 132


class FrostbiteTextureError(RuntimeError):
    pass


@dataclass(frozen=True)
class AnthemTextureHeader:
    width: int
    height: int
    depth: int
    texture_type: int
    pixel_format: int
    flags: int
    slice_count: int
    mip_count: int
    first_mip: int
    chunk_uid: bytes
    mip_sizes: tuple[int, ...]
    chunk_size: int
    asset_hash: int
    texture_group: str


# Anthem's internal TextureFormat enum follows the DXGI block-compression
# family.  The values below are the formats observed in retail Anthem data.
_DXGI_FORMATS = {
    0x06: 61,   # R8_UNORM
    0x12: 28,   # R8G8B8A8_UNORM
    0x14: 29,   # R8G8B8A8_UNORM_SRGB
    0x1B: 24,   # R10G10B10A2_UNORM
    0x28: 10,   # R16G16B16A16_FLOAT
    0x33: 2,    # R32G32B32A32_FLOAT
    0x36: 71,   # BC1_UNORM
    # Pillow's DDS reader decodes BC1/2/3 UNORM but rejects their bit-identical
    # sRGB aliases. Decode with the UNORM ID; the colour bytes are unchanged.
    0x37: 71,   # BC1_UNORM_SRGB (preview as UNORM)
    0x38: 71,   # BC1A_UNORM
    0x39: 71,   # BC1A_UNORM_SRGB (preview as UNORM)
    0x3A: 74,   # BC2_UNORM
    0x3B: 74,   # BC2_UNORM_SRGB (preview as UNORM)
    0x3C: 77,   # BC3_UNORM
    0x3D: 77,   # BC3_UNORM_SRGB (preview as UNORM)
    0x3E: 80,   # BC4_UNORM
    0x3F: 83,   # BC5_UNORM
    0x40: 95,   # BC6H_UF16
    0x41: 96,   # BC6H_SF16
    0x42: 98,   # BC7_UNORM
    0x43: 99,   # BC7_UNORM_SRGB
    0x7B: 56,   # D16_UNORM
}


def format_name(pixel_format: int) -> str:
    names = {
        0x06: "R8", 0x12: "RGBA8", 0x14: "RGBA8 sRGB",
        0x1B: "RGB10A2", 0x28: "RGBA16F", 0x33: "RGBA32F",
        0x36: "BC1", 0x37: "BC1 sRGB",
        0x38: "BC1 alpha", 0x39: "BC1 alpha sRGB", 0x3A: "BC2",
        0x3B: "BC2 sRGB", 0x3C: "BC3", 0x3D: "BC3 sRGB",
        0x3E: "BC4", 0x3F: "BC5", 0x40: "BC6H unsigned",
        0x41: "BC6H signed", 0x42: "BC7", 0x43: "BC7 sRGB",
        0x7B: "D16 depth",
    }
    return names.get(pixel_format, f"format 0x{pixel_format:02X}")


def parse_anthem_texture(data: bytes) -> AnthemTextureHeader:
    if len(data) < TEXTURE_DESCRIPTOR_SIZE:
        raise FrostbiteTextureError(
            f"Texture descriptor is truncated ({len(data)} bytes; expected at least 132)."
        )
    texture_type, pixel_format = struct.unpack_from("<II", data, 8)
    flags, width, height, depth, slices = struct.unpack_from("<5H", data, 20)
    mip_count, first_mip = struct.unpack_from("<2B", data, 30)
    if not width or not height or width > 32768 or height > 32768:
        raise FrostbiteTextureError(f"Invalid texture dimensions {width} x {height}.")
    if not mip_count or mip_count > 15:
        raise FrostbiteTextureError(f"Invalid texture mip count {mip_count}.")
    if pixel_format not in _DXGI_FORMATS:
        raise FrostbiteTextureError(
            f"Unsupported Anthem texture {format_name(pixel_format)}."
        )
    # Frostbite stores Guid fields in the byte order used by .NET Guid.
    chunk_uid = uuid.UUID(bytes_le=data[32:48]).bytes
    mip_sizes = struct.unpack_from("<15I", data, 48)
    chunk_size, asset_hash = struct.unpack_from("<II", data, 108)
    group = data[116:132].split(b"\0", 1)[0].decode("ascii", "replace")
    return AnthemTextureHeader(
        width=width, height=height, depth=max(1, depth),
        texture_type=texture_type, pixel_format=pixel_format, flags=flags,
        slice_count=max(1, slices), mip_count=mip_count, first_mip=first_mip,
        chunk_uid=chunk_uid, mip_sizes=tuple(mip_sizes), chunk_size=chunk_size,
        asset_hash=asset_hash, texture_group=group,
    )


def build_dds(header: AnthemTextureHeader, payload: bytes) -> bytes:
    """Return a DDS-DX10 stream for a 2D Anthem texture chunk."""
    # Cubes, volumes and arrays are intentionally exposed as their first 2D
    # surface. A useful first face/slice is preferable to rejecting the asset.
    dxgi = _DXGI_FORMATS[header.pixel_format]
    # Streamed textures can omit their largest mips. ``first_mip`` identifies
    # the first level resident in this chunk (e.g. a 4096 map with first_mip=3
    # contains a 512-wide top level). Build the DDS around resident data only.
    first_mip = min(header.first_mip, max(0, header.mip_count - 1), 14)
    width = max(1, header.width >> first_mip)
    height = max(1, header.height >> first_mip)
    top_size = header.mip_sizes[first_mip] or len(payload)
    mip_count = max(1, min(header.mip_count - first_mip, 15 - first_mip))
    # DDS_HEADER with DDS_PIXELFORMAT(DX10), followed by DDS_HEADER_DXT10.
    dds_flags = 0x00021007  # CAPS|HEIGHT|WIDTH|PIXELFORMAT|MIPMAPCOUNT|LINEARSIZE
    caps = 0x1000 | (0x400008 if mip_count > 1 else 0)
    reserved = (0,) * 11
    header_bytes = struct.pack(
        "<7I11I8I5I",
        124, dds_flags, height, width, top_size, 0, mip_count,
        *reserved,
        32, 0x4, struct.unpack("<I", b"DX10")[0], 0, 0, 0, 0, 0,
        caps, 0, 0, 0, 0,
    )
    dx10 = struct.pack("<5I", dxgi, 3, 0, 1, 0)  # TEXTURE2D, one slice
    return b"DDS " + header_bytes + dx10 + payload


def _display_linear(value: float) -> int:
    if not math.isfinite(value) or value <= 0.0:
        return 0
    value = min(value, 1.0)
    value = 12.92 * value if value <= 0.0031308 else 1.055 * value ** (1 / 2.4) - 0.055
    return max(0, min(255, round(value * 255)))


def _decode_float_rgba(
    payload: bytes, width: int, height: int, component_code: str, bytes_per_pixel: int,
) -> Image.Image:
    """Tone-map a bounded first 2D surface from half/full float RGBA."""
    stride = max(1, math.ceil(max(width, height) / 1024))
    out_width = math.ceil(width / stride)
    out_height = math.ceil(height / stride)
    required = width * height * bytes_per_pixel
    if len(payload) < required:
        raise FrostbiteTextureError(
            f"Float surface is truncated ({len(payload):,} of {required:,} bytes)."
        )
    converted = bytearray(out_width * out_height * 4)
    destination = 0
    unpack_format = "<4" + component_code
    for y in range(0, height, stride):
        row = y * width
        for x in range(0, width, stride):
            red, green, blue, alpha = struct.unpack_from(
                unpack_format, payload, (row + x) * bytes_per_pixel,
            )
            converted[destination:destination + 4] = bytes((
                _display_linear(red), _display_linear(green), _display_linear(blue),
                max(0, min(255, round(alpha * 255))) if math.isfinite(alpha) else 255,
            ))
            destination += 4
    return Image.frombytes("RGBA", (out_width, out_height), bytes(converted))


def decode_texture_image(
    descriptor: bytes, payload: bytes, *, max_preview_size: int = 1536,
) -> tuple[Image.Image, AnthemTextureHeader]:
    header = parse_anthem_texture(descriptor)
    resident_width = max(1, header.width >> header.first_mip)
    resident_height = max(1, header.height >> header.first_mip)
    try:
        if header.pixel_format == 0x06:
            needed = resident_width * resident_height
            image = Image.frombytes("L", (resident_width, resident_height), payload[:needed]).convert("RGBA")
        elif header.pixel_format in (0x12, 0x14):
            needed = resident_width * resident_height * 4
            image = Image.frombytes("RGBA", (resident_width, resident_height), payload[:needed])
        elif header.pixel_format == 0x1B:
            required = resident_width * resident_height * 4
            if len(payload) < required:
                raise FrostbiteTextureError(
                    f"RGB10A2 surface is truncated ({len(payload):,} of {required:,} bytes)."
                )
            converted = bytearray(resident_width * resident_height * 4)
            for index, (value,) in enumerate(struct.iter_unpack("<I", payload[:required])):
                offset = index * 4
                converted[offset:offset + 4] = bytes((
                    round((value & 0x3FF) * 255 / 1023),
                    round(((value >> 10) & 0x3FF) * 255 / 1023),
                    round(((value >> 20) & 0x3FF) * 255 / 1023),
                    round(((value >> 30) & 0x3) * 255 / 3),
                ))
            image = Image.frombytes("RGBA", (resident_width, resident_height), bytes(converted))
        elif header.pixel_format == 0x28:
            # Four IEEE-754 half floats per pixel. Convert the first 2D
            # face/slice to an sRGB display image and subsample large HDR maps
            # while reading so preview memory/CPU remain bounded.
            image = _decode_float_rgba(payload, resident_width, resident_height, "e", 8)
        elif header.pixel_format == 0x33:
            image = _decode_float_rgba(payload, resident_width, resident_height, "f", 16)
        elif header.pixel_format == 0x7B:
            required = resident_width * resident_height * 2
            if len(payload) < required:
                raise FrostbiteTextureError(
                    f"D16 surface is truncated ({len(payload):,} of {required:,} bytes)."
                )
            luminance = bytes(
                value >> 8 for (value,) in struct.iter_unpack("<H", payload[:required])
            )
            image = Image.frombytes(
                "L", (resident_width, resident_height), luminance,
            ).convert("RGBA")
        else:
            with Image.open(io.BytesIO(build_dds(header, payload))) as decoded:
                image = decoded.convert("RGBA")
    except Exception as exc:
        raise FrostbiteTextureError(
            f"Could not decode {format_name(header.pixel_format)} texture: {exc}"
        ) from exc
    if max(image.size) > max_preview_size:
        image.thumbnail((max_preview_size, max_preview_size), Image.Resampling.LANCZOS)
    return image, header
