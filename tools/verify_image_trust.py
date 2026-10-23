"""Offline image gate: inspect PEM bytes, never SSLContext's lazy CA cache."""
import hashlib
import importlib.metadata
import json
from pathlib import Path
import re
import ssl

BAD_SHA256 = "86a1ecba089c4a8d3bbe2734c612ba341d813e043cf9e8a862cd5c57a36bbe6b"


def fingerprints(pem):
    blocks = re.findall(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", pem, re.S)
    if not blocks:
        raise ValueError("CA bundle contains no PEM certificates")
    return {hashlib.sha256(ssl.PEM_cert_to_DER_cert(block)).hexdigest() for block in blocks}


def main():
    import certifi
    from pip._vendor import certifi as pip_certifi
    from pip._vendor.packaging.version import Version

    for package, minimum in (("pip", "26.2.2"), ("certifi", "2026.10.19"), ("aiohttp", "3.14.5")):
        version = importlib.metadata.version(package)
        if Version(version) < Version(minimum):
            raise RuntimeError(f"{package} {version} < {minimum}")
        print(f"{package}={version}")
    paths = {
        "system": Path("/etc/ssl/certs/ca-certificates.crt"),
        "certifi": Path(certifi.where()),
        "pip-vendored-certifi": Path(pip_certifi.where()),
    }
    for name, path in paths.items():
        data = path.read_bytes()
        hashes = fingerprints(data.decode("ascii"))
        if BAD_SHA256 in hashes:
            raise RuntimeError(f"{name}: affected emSign ECC Root CA - G3 still trusted")
        print(json.dumps({"bundle": name, "path": str(path), "count": len(hashes),
                          "sha256": hashlib.sha256(data).hexdigest(), "affected_root": "absent"}))


if __name__ == "__main__":
    main()
