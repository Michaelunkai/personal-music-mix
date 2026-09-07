"""Accept private Sites connection settings on stdin and encrypt them with DPAPI."""
import base64
import ctypes
import json
import sys
from pathlib import Path


def main():
    value = json.load(sys.stdin)
    raw = value.pop("token").encode("utf-8")

    class Blob(ctypes.Structure):
        _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    incoming, outgoing = Blob(len(raw), buffer), Blob()
    if not ctypes.windll.crypt32.CryptProtectData(ctypes.byref(incoming), None, None, None, None, 0, ctypes.byref(outgoing)):
        raise RuntimeError("Could not encrypt the private site credential")
    try:
        value["encrypted_token"] = base64.b64encode(ctypes.string_at(outgoing.data, outgoing.size)).decode()
    finally:
        ctypes.windll.kernel32.LocalFree(outgoing.data)
    root = Path(__file__).resolve().parent.parent
    value["database_path"] = str(root / 'data' / 'ytmusic_recommender.sqlite3')
    (root / 'data' / 'cloud-sync.json').write_text(json.dumps(value), encoding='utf-8')
    print('Private site connection encrypted for the current Windows account.')


if __name__ == '__main__':
    main()
