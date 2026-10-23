import hashlib
import ssl
import unittest
from unittest.mock import patch

from tools.verify_image_trust import BAD_SHA256, fingerprints, main


class ImageTrustTests(unittest.TestCase):
    def test_empty_bundle_fails_closed(self):
        with self.assertRaises(ValueError):
            fingerprints("# empty")

    def test_hashes_der_not_pem_formatting(self):
        der = b"synthetic certificate bytes"
        pem = ssl.DER_cert_to_PEM_cert(der)
        expected = {hashlib.sha256(der).hexdigest()}
        self.assertEqual(fingerprints(pem + pem), expected)
        self.assertEqual(fingerprints(pem.replace('\n', '\r\n')), expected)

    def test_gate_rejects_compromised_root_in_each_bundle(self):
        for index in range(3):
            with self.subTest(bundle=index), \
                 patch('tools.verify_image_trust.importlib.metadata.version', return_value='9999.1'), \
                 patch('tools.verify_image_trust.Path.read_bytes', return_value=b'fixture'), \
                 patch('tools.verify_image_trust.fingerprints', side_effect=[{'safe'}] * index + [{BAD_SHA256}]), \
                 patch('builtins.print'):
                with self.assertRaisesRegex(RuntimeError, 'still trusted'):
                    main()
