import hashlib
import logging
from typing import Optional, Dict

import base58
from Crypto.Hash import keccak

logger = logging.getLogger(__name__)

TOKENS = {
    "firecap": {
        "id": "000000",
        "version": "00"
    }
}


def is_likely_public_key(address: str) -> bool:
    """
    Heuristically determine whether the input string looks like
    a secp256k1 public key in hex form.
    """
    if not address:
        return False

    clean_address = address.lower().replace('0x', '').strip()

    # Public keys: 64 bytes (128 hex chars) or 65 bytes (130 chars with prefix)
    if len(clean_address) in [128, 130]:
        try:
            # Must be valid hex
            bytes.fromhex(clean_address)

            # Uncompressed public key: 0x04 + 64 bytes
            if len(clean_address) == 130 and clean_address.startswith('04'):
                return True

            # Raw key without prefix (rare, but allowed here)
            elif len(clean_address) == 128:
                return True

            # Compressed formats (0x02 / 0x03)
            elif len(clean_address) == 130 and clean_address.startswith(('02', '03')):
                return True

        except ValueError:
            return False

    return False


def detect_address_type(address: str) -> str:
    """
    Detect whether the string is:
      - an ASI address
      - a public key
      - unknown
    """
    if not address:
        return "unknown"

    clean_address = address.strip()

    # ASI addresses always start with '1111' and have ~52–57 chars
    if clean_address.startswith('1111') and len(clean_address) in range(52, 58):
        return "asi_address"

    elif is_likely_public_key(clean_address):
        return "public_key"

    else:
        return "unknown"


def public_key_to_asi_address(public_key_hex: str) -> str:
    """
    Convert a secp256k1 public key (hex string, compressed or uncompressed)
    into an ASI address.

    This implementation strictly mirrors the TypeScript reference version.
    """
    # Normalize input: strip 0x prefix
    clean_pk = public_key_hex.lower().replace('0x', '').strip()

    # If key is 64 bytes (128 chars), prepend 0x04 (uncompressed prefix)
    if len(clean_pk) == 128:
        public_key_bytes = bytes.fromhex('04' + clean_pk)

    # If key is already 65 bytes (130 chars) and starts with 0x04
    elif len(clean_pk) == 130 and clean_pk.startswith('04'):
        public_key_bytes = bytes.fromhex(clean_pk)

    else:
        raise ValueError(f"Unsupported public key format: {public_key_hex}")

    # Strip first byte (0x04) → get the raw 64-byte X,Y coordinates
    public_key_no_prefix = public_key_bytes[1:]

    # First Keccak-256: keccak256(pubkey[1:])
    keccak_hash1 = keccak.new(digest_bits=256)
    keccak_hash1.update(public_key_no_prefix)
    hash1_result = keccak_hash1.hexdigest()

    # Take last 20 bytes (40 hex chars) — Ethereum-style address derivation
    public_key_hash = hash1_result[-40:].upper()

    # Second Keccak-256: keccak256(publicKeyHashBytes)
    keccak_hash2 = keccak.new(digest_bits=256)
    public_key_hash_bytes = bytes.fromhex(public_key_hash)
    keccak_hash2.update(public_key_hash_bytes)
    eth_hash = keccak_hash2.hexdigest().upper()

    # Token namespace prefix: "00000000" etc.
    TOKEN_ID = TOKENS["firecap"]["id"]
    VERSION = TOKENS["firecap"]["version"]

    # Payload = <token><version><ethHash>
    payload = TOKEN_ID + VERSION + eth_hash

    # Blake2b checksum over payload (first 4 bytes = 8 hex chars)
    payload_bytes = bytes.fromhex(payload)
    blake_hash = hashlib.blake2b(payload_bytes, digest_size=32).hexdigest().upper()
    checksum = blake_hash[:8]

    # Final payload
    final_payload = payload + checksum

    # Base58 encoding of the final hex bytes
    final_payload_bytes = bytes.fromhex(final_payload)
    asi_address = base58.b58encode(final_payload_bytes).decode('ascii')

    return asi_address


def convert_to_asi_address(address: str, deploy_data: Optional[Dict] = None) -> str:
    """
    Convert any input into an ASI address:
      - If already ASI, return as-is.
      - If public key, convert via public_key_to_asi_address().
      - If unknown format, return original address.
    """
    if not address:
        return address

    address_type = detect_address_type(address)

    # Already an ASI address
    if address_type == "asi_address":
        return address

    # Convert from public key
    elif address_type == "public_key":
        try:
            return public_key_to_asi_address(address)

        except Exception as e:
            logger.warning(f"Failed to convert public key to ASI address: {e}")

            # Fallback: try deployer/sender public key
            if deploy_data:
                sender = deploy_data.get("deployer") or deploy_data.get("sender")
                if sender and detect_address_type(sender) == "public_key":
                    try:
                        return public_key_to_asi_address(sender)
                    except Exception as fallback_error:
                        logger.warning(f"Fallback conversion failed: {fallback_error}")

            return address

    # Unknown format — cannot convert
    else:
        logger.warning(f"Unknown address format, cannot convert: {address[:20]}...")
        return address


def test_conversion():
    """Simple test to verify that conversion works as expected."""
    test_public_key = (
        "04ed6192b1e57af0576039b1157fc359d49ad5d19c3e205a2493199ec18175a2"
        "e822969550504bf1c85e7669c44ca681bf94ba45a4cef5deba9d9b577e9e14563f"
    )

    print("Testing address conversion:")
    print(f"Public key: {test_public_key}")
    print(f"Public key type: {detect_address_type(test_public_key)}")

    try:
        converted = public_key_to_asi_address(test_public_key)
        print(f"✓ Converted ASI address: {converted}")
        print(f"✓ Detected type: {detect_address_type(converted)}")
        print(f"✓ Starts with 1111: {converted.startswith('1111')}")
        print(f"✓ Length: {len(converted)}")
    except Exception as e:
        print(f"✗ Conversion error: {e}")


def compare_with_typescript():
    """
    Compare all intermediate values against the TypeScript implementation.
    Useful for debugging and ensuring algorithm parity.
    """
    test_public_key = (
        "04ed6192b1e57af0576039b1157fc359d49ad5d19c3e205a2493199ec18175a2"
        "e822969550504bf1c85e7669c44ca681bf94ba45a4cef5deba9d9b577e9e14563f"
    )

    print("\n=== COMPARISON WITH TYPESCRIPT ===")

    clean_pk = test_public_key.lower().replace('0x', '').strip()
    public_key_bytes = bytes.fromhex(clean_pk)
    public_key_no_prefix = public_key_bytes[1:]

    # First Keccak
    keccak_hash1 = keccak.new(digest_bits=256)
    keccak_hash1.update(public_key_no_prefix)
    public_key_hash = keccak_hash1.hexdigest()[-40:].upper()
    print(f"publicKeyHash: {public_key_hash}")

    # Second Keccak
    keccak_hash2 = keccak.new(digest_bits=256)
    keccak_hash2.update(bytes.fromhex(public_key_hash))
    eth_hash = keccak_hash2.hexdigest().upper()
    print(f"ethHash: {eth_hash}")

    # Payload
    payload = "000000" + "00" + eth_hash
    print(f"payload: {payload}")

    # Blake2b checksum
    payload_bytes = bytes.fromhex(payload)
    checksum = hashlib.blake2b(payload_bytes, digest_size=32).hexdigest().upper()[:8]
    print(f"checksum: {checksum}")

    # Final output
    final_payload = payload + checksum
    print(f"final_payload: {final_payload}")

    asi_address = base58.b58encode(bytes.fromhex(final_payload)).decode('ascii')
    print(f"Final address: {asi_address}")


if __name__ == "__main__":
    test_conversion()
    compare_with_typescript()
