import base64

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

from polycab_client import _SIGN_KEY1, _SIGN_KEY2, num, sign


# Pinned outputs of the signing scheme, which production has been using successfully
# against the live API since July 2026 - a change here means requests will start failing.
def test_sign_pinned_values():
    assert sign({"MemberAutoID": "1234567", "date": "2026-07-20"}) == (
        "Rcke7expTtbbyMpVkGCIgKxPf/kbizYEXvaLFRK+oG03BYyqmNkVbCFdba18rRkTGAtPAxAfR+cblR5EVU0u7fqRTP2DzMftVQQWkSStlDg="
    )
    assert sign({"GoodsID": "1234-123456789P", "empty": "", "none": None, "flag": True, "list": [1, 2], "n": 5}) == (
        "9iqj7yuIVuyr6ZPZL/J/m7yf+vI901M48idvV2cpSKmUH6cUpg6u51OD6+oTlac/mXrbvMZ8RdmLFwEAWgOzKocCUffW9Wr4+aN6FJ2P+k8="
    )


def _decrypt(signature: str) -> str:
    cipher = AES.new(_SIGN_KEY1.encode(), AES.MODE_CBC, _SIGN_KEY2.encode())
    return unpad(cipher.decrypt(base64.b64decode(signature)), AES.block_size).decode()


def test_sign_canonical_string():
    """Checks the canonicalization rules from recon/notes.md independently of the pinned
    ciphertexts: drop ""/None/bools, sort keys, lists become "Array", KEY1 appended."""
    signed = sign({"b": "2", "a": "1", "empty": "", "none": None, "flag": False, "list": [1], "n": 0})
    assert _decrypt(signed) == f"a=1&b=2&list=Array&n=0&{_SIGN_KEY1}"


def test_num_coerces_placeholders_to_none():
    assert num("-") is None
    assert num("") is None
    assert num(None) is None
    assert num("12.5") == 12.5
    assert num(3) == 3.0
