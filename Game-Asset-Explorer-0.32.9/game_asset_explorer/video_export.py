"""Small dependency-free MJPEG/AVI writer for offline viewport exports."""
from __future__ import annotations

import io
import struct
from pathlib import Path
from typing import Iterable


def _chunk(tag: bytes, payload: bytes) -> bytes:
    return tag + struct.pack("<I", len(payload)) + payload + (b"\0" if len(payload) & 1 else b"")


def _list(kind: bytes, payload: bytes) -> bytes:
    return _chunk(b"LIST", kind + payload)


def write_mjpeg_avi(
    path: Path, frames: Iterable, *, width: int, height: int, fps: int,
) -> int:
    """Write PIL images as a broadly playable Motion-JPEG AVI.

    Frames are encoded and written one at a time, so a slow, high-detail
    offline render does not have to retain the entire clip in memory.
    """
    if width <= 0 or height <= 0 or fps <= 0:
        raise ValueError("Video dimensions and frame rate must be positive.")
    path = Path(path)
    index: list[tuple[int, int]] = []
    max_frame = 0
    with path.open("w+b") as stream:
        stream.write(b"RIFF\0\0\0\0AVI ")
        strh_payload = struct.pack(
            "<4s4sIHHIIIIIIIIhhhh", b"vids", b"MJPG", 0, 0, 0,
            0, 1, fps, 0, 0, 0, 0xFFFFFFFF, 0, 0, 0, width, height,
        )
        strf_payload = struct.pack(
            "<IiiHH4sIiiII", 40, width, height, 1, 24, b"MJPG",
            width * height * 3, 0, 0, 0, 0,
        )
        # Header sizes are fixed; total frame counts and max frame size are
        # patched once the iterable has completed.
        avih = _chunk(b"avih", struct.pack(
            "<IIIIIIIIII4I", round(1_000_000 / fps), 0, 0, 0x10,
            0, 0, 1, 0, width, height, 0, 0, 0, 0,
        ))
        hdrl = _list(b"hdrl", avih + _list(b"strl", _chunk(b"strh", strh_payload)
                                             + _chunk(b"strf", strf_payload)))
        stream.write(hdrl)
        # Offsets within the fixed header assembled above. Keeping these
        # explicit also makes the final count patch independent of frame data.
        avih_payload_at = 32
        strh_payload_at = 108
        movi_start = stream.tell()
        stream.write(b"LIST\0\0\0\0movi")
        data_start = stream.tell()
        for frame in frames:
            image = frame.convert("RGB")
            if image.size != (width, height):
                raise ValueError("Every video frame must have the requested dimensions.")
            encoded = io.BytesIO()
            image.save(encoded, format="JPEG", quality=90, subsampling=1)
            payload = encoded.getvalue()
            offset = stream.tell() - data_start + 4
            stream.write(_chunk(b"00dc", payload))
            index.append((offset, len(payload)))
            max_frame = max(max_frame, len(payload))
        if not index:
            raise ValueError("No video frames were supplied.")
        movi_end = stream.tell()
        stream.seek(movi_start + 4)
        stream.write(struct.pack("<I", movi_end - movi_start - 8))
        stream.seek(movi_end)
        idx_payload = b"".join(
            struct.pack("<4sIII", b"00dc", 0x10, offset, size)
            for offset, size in index
        )
        stream.write(_chunk(b"idx1", idx_payload))
        file_end = stream.tell()

        # Patch RIFF size and the two variable fields in AVIMAINHEADER.
        stream.seek(4)
        stream.write(struct.pack("<I", file_end - 8))
        stream.seek(avih_payload_at + 16)
        stream.write(struct.pack("<I", len(index)))
        stream.seek(avih_payload_at + 28)
        stream.write(struct.pack("<I", max_frame))
        stream.seek(strh_payload_at + 32)
        stream.write(struct.pack("<I", len(index)))
        stream.seek(strh_payload_at + 36)
        stream.write(struct.pack("<I", max_frame))
    return len(index)
