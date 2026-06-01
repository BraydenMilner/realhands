import zlib, struct

def make_png(width, height, color):
    # RGB color
    r, g, b = color
    # Raw data: 1 byte filter (0) + 3 bytes RGB per pixel
    row = b'\x00' + bytes([r, g, b]) * width
    data = row * height
    
    # Chunk creation helper
    def chunk(tag, data):
        return struct.pack('>I', len(data)) + tag + data + struct.pack('>I', zlib.crc32(tag + data))

    # PNG signature
    png = b'\x89PNG\r\n\x1a\n'
    # IHDR
    png += chunk(b'IHDR', struct.pack('>2I5B', width, height, 8, 2, 0, 0, 0))
    # IDAT
    png += chunk(b'IDAT', zlib.compress(data))
    # IEND
    png += chunk(b'IEND', b'')
    return png

import os

# Write icons next to this script (extension/icons/).
ICONS_DIR = os.path.dirname(os.path.abspath(__file__))
sizes = [16, 32, 48, 128]

for size in sizes:
    path = os.path.join(ICONS_DIR, f"icon{size}.png")
    with open(path, 'wb') as f:
        f.write(make_png(size, size, (0, 122, 255)))  # Blue color
    print(f"Created {path}")
