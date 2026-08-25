# SPDX-License-Identifier: MPL-2.0

"""Minimal, strict OpenPGP detached-signature verifier for release manifests.

The Browser3 release key is Ed25519 and releases use version-4 detached
signatures. This verifier intentionally supports only that narrow format and
SHA-256/SHA-512; it has no unsigned or weak-hash fallback. ``cryptography`` is
used for the audited Ed25519 primitive while packet framing is parsed locally
so verification does not depend on a user's ``gpg.exe`` installation.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
import struct
from dataclasses import dataclass


PINNED_FINGERPRINT = "138AE85373688ADFFDD005A27439B75BE8645184"
_ARMOR_BEGIN = "-----BEGIN PGP PUBLIC KEY BLOCK-----"
_ARMOR_SIG_BEGIN = "-----BEGIN PGP SIGNATURE-----"
_ARMOR_END = "-----END PGP "


class OpenPGPError(ValueError):
    """The supplied key or signature is malformed or not trusted."""


@dataclass(frozen=True)
class VerificationResult:
    fingerprint: str
    digest_algorithm: str
    key_id: str


def _armor_decode(value: bytes, begin: str) -> bytes:
    try:
        text = value.decode("ascii")
    except UnicodeDecodeError as exc:
        raise OpenPGPError("OpenPGP armor is not ASCII") from exc
    lines = text.splitlines()
    try:
        start = lines.index(begin)
    except ValueError as exc:
        raise OpenPGPError("OpenPGP armor header is missing") from exc
    footer = begin.replace("BEGIN", "END")
    payload = []
    saw_blank = False
    checksum = None
    saw_footer = False
    for line in lines[start + 1:]:
        if line == footer:
            saw_footer = True
            break
        if line.startswith(_ARMOR_END):
            raise OpenPGPError("OpenPGP armor footer does not match its header")
        if not saw_blank:
            if line == "":
                saw_blank = True
            elif ":" in line:
                # Armor headers are metadata, never signed payload.
                continue
            else:
                raise OpenPGPError("Malformed OpenPGP armor headers")
            continue
        if line.startswith("="):
            checksum = line[1:]
        elif line.strip():
            payload.append(line.strip())
    if not saw_footer:
        raise OpenPGPError("OpenPGP armor footer is missing")
    if any(line.strip() for line in lines[lines.index(footer, start + 1) + 1:]):
        raise OpenPGPError("OpenPGP armor has data after its footer")
    if not payload:
        raise OpenPGPError("OpenPGP armor payload is empty")
    try:
        raw = base64.b64decode("".join(payload), validate=True)
    except (ValueError, binascii.Error) as exc:
        raise OpenPGPError("OpenPGP armor payload is invalid") from exc
    if checksum:
        try:
            crc = base64.b64decode(checksum, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise OpenPGPError("OpenPGP armor CRC is invalid") from exc
        if len(crc) != 3:
            raise OpenPGPError("OpenPGP armor CRC length is invalid")
        actual = _crc24(raw).to_bytes(3, "big")
        if actual != crc:
            raise OpenPGPError("OpenPGP armor CRC mismatch")
    return raw


def _crc24(data: bytes) -> int:
    crc = 0xB704CE
    for byte in data:
        crc ^= byte << 16
        for _ in range(8):
            crc <<= 1
            if crc & 0x1000000:
                crc ^= 0x1864CFB
            crc &= 0xFFFFFF
    return crc


def _read_new_length(data: bytes, offset: int) -> tuple[int, int]:
    if offset >= len(data):
        raise OpenPGPError("Truncated OpenPGP packet length")
    first = data[offset]
    if first < 192:
        return first, offset + 1
    if first <= 223:
        if offset + 1 >= len(data):
            raise OpenPGPError("Truncated OpenPGP packet length")
        return ((first - 192) << 8) + data[offset + 1] + 192, offset + 2
    if first == 255:
        if offset + 4 >= len(data):
            raise OpenPGPError("Truncated OpenPGP packet length")
        return int.from_bytes(data[offset + 1:offset + 5], "big"), offset + 5
    # Partial body lengths are not needed for the tiny key/signature objects.
    raise OpenPGPError("Partial OpenPGP packet lengths are unsupported")


def _packets(data: bytes):
    offset = 0
    while offset < len(data):
        ctb = data[offset]
        offset += 1
        if not ctb & 0x80:
            raise OpenPGPError("Invalid OpenPGP packet tag")
        if ctb & 0x40:
            tag = ctb & 0x3F
            length, offset = _read_new_length(data, offset)
        else:
            tag = (ctb >> 2) & 0x0F
            length_type = ctb & 0x03
            if length_type == 0:
                if offset >= len(data):
                    raise OpenPGPError("Truncated OpenPGP packet length")
                length = data[offset]
                offset += 1
            elif length_type == 1:
                if offset + 2 > len(data):
                    raise OpenPGPError("Truncated OpenPGP packet length")
                length = int.from_bytes(data[offset:offset + 2], "big")
                offset += 2
            elif length_type == 2:
                if offset + 4 > len(data):
                    raise OpenPGPError("Truncated OpenPGP packet length")
                length = int.from_bytes(data[offset:offset + 4], "big")
                offset += 4
            else:
                length = len(data) - offset
        end = offset + length
        if end > len(data):
            raise OpenPGPError("Truncated OpenPGP packet body")
        yield tag, data[offset:end]
        offset = end


def _mpi(data: bytes, offset: int) -> tuple[bytes, int]:
    if offset + 2 > len(data):
        raise OpenPGPError("Truncated OpenPGP MPI")
    bits = int.from_bytes(data[offset:offset + 2], "big")
    length = (bits + 7) // 8
    start = offset + 2
    end = start + length
    if end > len(data) or bits == 0:
        raise OpenPGPError("Invalid OpenPGP MPI")
    return data[start:end], end


def _public_key(key_bytes: bytes) -> tuple[object, str, str]:
    packets = list(_packets(_armor_decode(key_bytes, _ARMOR_BEGIN)))
    primary = next((body for tag, body in packets if tag == 6), None)
    if (primary is None or len(primary) < 7 or len(primary) > 0xFFFF
            or primary[0] != 4 or primary[5] != 22):
        raise OpenPGPError("Pinned release key is not a version-4 Ed25519 key")
    fingerprint = hashlib.sha1(b"\x99" + len(primary).to_bytes(2, "big") + primary).hexdigest().upper()
    if fingerprint != PINNED_FINGERPRINT:
        raise OpenPGPError("Pinned release key fingerprint mismatch")
    # Ed25519's OpenPGP MPI includes the 0x40 native-format prefix.
    offset = 6
    if offset >= len(primary):
        raise OpenPGPError("Ed25519 public-key OID is missing")
    oid_len = primary[offset]
    offset += 1
    if offset + oid_len > len(primary):
        raise OpenPGPError("Ed25519 public-key OID is truncated")
    offset += oid_len
    mpi, _ = _mpi(primary, offset)
    if len(mpi) != 33 or mpi[0] != 0x40:
        raise OpenPGPError("Unsupported Ed25519 public-key MPI")
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        public = Ed25519PublicKey.from_public_bytes(mpi[1:])
    except ImportError as exc:
        raise OpenPGPError("cryptography is required for release verification") from exc
    key_id = hashlib.sha1(b"\x99" + len(primary).to_bytes(2, "big") + primary).digest()[-8:].hex().upper()
    return public, fingerprint, key_id


def _signature_packet(signature_bytes: bytes) -> tuple[bytes, bytes, str, str]:
    packets = list(_packets(_armor_decode(signature_bytes, _ARMOR_SIG_BEGIN)))
    signature_packets = [body for tag, body in packets if tag == 2]
    if len(signature_packets) != 1:
        raise OpenPGPError("Detached OpenPGP signature packet is missing")
    body = signature_packets[0]
    if len(body) < 10:
        raise OpenPGPError("Detached OpenPGP signature packet is truncated")
    if body[0] != 4:
        raise OpenPGPError("Only version-4 OpenPGP signatures are accepted")
    sig_type, public_algo, hash_algo = body[1:4]
    if sig_type != 0:
        raise OpenPGPError("Only binary-document release signatures are accepted")
    if public_algo != 22:
        raise OpenPGPError("Release signature is not Ed25519")
    hash_names = {8: "sha256", 10: "sha512"}
    digest_name = hash_names.get(hash_algo)
    if digest_name not in ("sha256", "sha384", "sha512"):
        raise OpenPGPError("Release signature uses a weak or unsupported digest")
    hashed_len = int.from_bytes(body[4:6], "big")
    hashed_end = 6 + hashed_len
    if hashed_end + 4 > len(body):
        raise OpenPGPError("Truncated OpenPGP hashed signature data")
    hashed = body[:hashed_end]
    unhashed_len_offset = hashed_end
    unhashed_len = int.from_bytes(body[unhashed_len_offset:unhashed_len_offset + 2], "big")
    mpi_offset = unhashed_len_offset + 2 + unhashed_len
    if mpi_offset + 2 > len(body):
        raise OpenPGPError("Truncated OpenPGP unhashed signature data")
    digest_prefix = body[mpi_offset:mpi_offset + 2]
    mpi_offset += 2
    r, mpi_offset = _mpi(body, mpi_offset)
    s, mpi_offset = _mpi(body, mpi_offset)
    if mpi_offset != len(body) or len(r) > 32 or len(s) > 32:
        raise OpenPGPError("Invalid Ed25519 signature MPIs")
    # GnuPG's OpenPGP Ed25519 profile stores the two 32-byte native-format
    # components directly in the MPIs. Do not reverse them as generic MPI
    # integer conversion would suggest.
    if len(r) != 32 or len(s) != 32:
        raise OpenPGPError("Invalid Ed25519 signature MPI length")
    raw_signature = r + s
    trailer = b"\x04\xff" + len(hashed).to_bytes(4, "big")
    signed_suffix = hashed + trailer
    return raw_signature, signed_suffix, digest_name, digest_prefix.hex().upper()


def verify_detached(data: bytes, signature: bytes, key: bytes) -> VerificationResult:
    """Verify a detached release signature against the pinned Browser3 key."""
    public, fingerprint, key_id = _public_key(key)
    raw_signature, suffix, digest_name, prefix = _signature_packet(signature)
    digest = hashlib.new(digest_name, data + suffix).digest()
    if digest[:2].hex().upper() != prefix:
        raise OpenPGPError("Detached signature digest prefix mismatch")
    try:
        # OpenPGP's EdDSA profile signs the selected OpenPGP digest, rather than
        # feeding the complete document directly to the Ed25519 primitive.
        public.verify(raw_signature, digest)
    except Exception as exc:
        raise OpenPGPError("Detached OpenPGP signature verification failed") from exc
    return VerificationResult(fingerprint, digest_name, key_id)
