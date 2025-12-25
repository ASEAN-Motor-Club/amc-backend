import os
import json
from Crypto.Cipher import AES

KEY = b"66c5fd51a70e5e232cd236bd6895f802"
BLOCK_SIZE = 16

SAVED_PATH = os.environ.get('SAVED_PATH', '/var/lib/motortown-server/MotorTown/Saved')
DATA_PATH = os.environ.get('DATA_PATH', '/srv/www')

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

def decrypt_file(path: str) -> bytes:
    """
    Read the file at `path` (which must contain data previously encrypted
    by `encrypt_file`), decrypt it, and return the original bytes.
    """
    with open(path, "rb") as f:
        data = f.read()
    return decrypt(data)

def get_world():
  path = os.path.join(SAVED_PATH, 'SaveGames/Worlds/0/Island.world')
  decrypted_bytes = decrypt_file(path)
  decrypted_str = decrypted_bytes.decode("utf-8")
  return json.loads(decrypted_str)['world']

def get_character():
  path = os.path.join(SAVED_PATH, 'SaveGames/Characters/0.sav')
  decrypted_bytes = decrypt_file(path)
  decrypted_str = decrypted_bytes.decode("utf-8")
  return json.loads(decrypted_str)

def format_duration(seconds: int) -> str:
    periods = [
        ('day',    86400),
        ('hour',    3600),
        ('minute',     60),
        ('second',      1),
    ]
    parts = []
    for name, count in periods:
        value, seconds = divmod(seconds, count)
        if value:
            unit = name if value == 1 else name + 's'
            parts.append(f"{value} {unit}")
    if not parts:
        return "0 seconds"
    if len(parts) == 1:
        return parts[0]
    return ', '.join(parts[:-1]) + ' and ' + parts[-1]

def get_housings(world):
    housings = world['housings']
    return {
      name: {
        'rentLeft': format_duration(h['rentLeftTimeSeconds']),
        **h
      }
      for name, h in housings.items()
    }


