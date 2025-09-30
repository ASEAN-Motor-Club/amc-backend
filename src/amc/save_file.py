from Crypto.Cipher import AES

KEY = b"66c5fd51a70e5e232cd236bd6895f802"
BLOCK_SIZE = 16

def encrypt(data: bytes) -> bytes:
    size = 4 + len(data)
    pad_size = (size + BLOCK_SIZE) & ~(BLOCK_SIZE - 1)
    out = bytearray(pad_size)
    out[0:4] = len(data).to_bytes(4, "little")
    for i, b in enumerate(data):
        out[i + 4] = (b - 1) & 0xFF

    cipher = AES.new(KEY, AES.MODE_ECB)
    for i in range(0, pad_size, BLOCK_SIZE):
        out[i : i + BLOCK_SIZE] = cipher.encrypt(bytes(out[i : i + BLOCK_SIZE]))
    return bytes(out)

def decrypt(data: bytes) -> bytes:
    cipher = AES.new(KEY, AES.MODE_ECB)
    buf = bytearray(data)
    for i in range(0, len(buf), BLOCK_SIZE):
        buf[i : i + BLOCK_SIZE] = cipher.decrypt(bytes(buf[i : i + BLOCK_SIZE]))

    orig_len = int.from_bytes(buf[0:4], "little")
    res = bytearray()
    for b in buf[4:]:
        res.append((b + 1) & 0xFF)
    return bytes(res[:orig_len])

