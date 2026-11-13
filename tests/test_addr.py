"""Guard address derivation across Python and cryptography dependency updates."""

from Crypto.Hash import keccak

from src.addr import public_key_to_asi_address

# Public secp256k1 generator point; this is a public test vector, not a secret.
PUBLIC_KEY = (
    "0479be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"
    "483ada7726a3c4655da4fbfc0e1108a8fd17b448a68554199c47d08ffb10d4b8"
)


def test_keccak_256_vector():
    # Ethereum/Rholang use Keccak, whose padding differs from SHA3-256.
    assert keccak.new(digest_bits=256).hexdigest() == (
        "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
    )


def test_address_derivation_preserves_legacy_result():
    # Captured with pycryptodome 3.20.0 before the major dependency update.
    assert public_key_to_asi_address(PUBLIC_KEY) == (
        "1111PXDQTDEd4XNuX4YWoB6XeL7ssWvhePGD2XmkENkG5sHfAMW9Q"
    )
