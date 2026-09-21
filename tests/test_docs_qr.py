import re
import struct
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS = REPO_ROOT / "docs"


class LandingPageQrTests(unittest.TestCase):
    def test_qr_assets_keep_the_standard_four_module_quiet_zone(self):
        svg = (DOCS / "qr-scotus.svg").read_text()
        self.assertRegex(svg, r'<svg[^>]+width="396"[^>]+height="396"')
        self.assertRegex(svg, r'<path[^>]+d="M4 4\.5')

        png = (DOCS / "qr-scotus.png").read_bytes()
        width, height = struct.unpack(">II", png[16:24])
        self.assertEqual((width, height), (792, 792))

    def test_inline_qr_uses_the_same_quiet_zone(self):
        html = (DOCS / "index.html").read_text()
        qr = re.search(r'<svg[^>]+class="segno".*?</svg>', html)
        self.assertIsNotNone(qr)
        self.assertIn('width="264" height="264"', qr.group())
        self.assertRegex(qr.group(), r'<path[^>]+d="M4 4\.5')
        self.assertIn('.hero svg{width:264px;height:264px', html)


if __name__ == "__main__":
    unittest.main()
