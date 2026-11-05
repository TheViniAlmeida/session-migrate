"""Bounded synthetic Zstandard encoding for cross-version native fixtures."""

import ctypes

from session_migrate import codex_storage as storage


def compress_fixture(data: bytes) -> bytes:
    """Encode only bounded synthetic fixtures, including on supported Python 3.11."""
    assert len(data) <= 8 * 1024 * 1024
    module = storage._stdlib_zstd()
    if module is not None:
        return module.compress(data)
    library = storage._load_libzstd()
    library.ZSTD_compressBound.argtypes = [ctypes.c_size_t]
    library.ZSTD_compressBound.restype = ctypes.c_size_t
    library.ZSTD_compress.argtypes = [
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_int,
    ]
    library.ZSTD_compress.restype = ctypes.c_size_t
    capacity = library.ZSTD_compressBound(len(data))
    assert capacity <= 9 * 1024 * 1024
    source, destination = ctypes.create_string_buffer(data), ctypes.create_string_buffer(capacity)
    size = library.ZSTD_compress(destination, capacity, source, len(data), 3)
    assert not library.ZSTD_isError(size)
    return destination.raw[:size]
