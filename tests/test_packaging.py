import hashlib
import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPTS=Path(__file__).resolve().parents[1]/'scripts'
sys.path.insert(0,str(SCRIPTS))
from release_version import choose_version, parse_version
from build_deb import build
from verify_deb import verify

class VersionTests(unittest.TestCase):
    def version(self,**args):
        values=dict(base='0.1.0',tags=[],head_tags=[],count=7,sha='1234abcd1234abcd',release=True)
        values.update(args)
        return choose_version(**values)
    def test_first_release_and_semantic_patch_order(self):
        self.assertEqual(self.version(),'0.1.0')
        self.assertEqual(self.version(tags=['v0.1.9','v0.1.10','v0.1.2','unrelated']),'0.1.11')
    def test_reruns_do_not_bump_existing_source_release(self):
        self.assertEqual(self.version(tags=['v0.1.1'],head_tags=['v0.1.1']),'0.1.1')
    def test_explicit_minor_and_major_bumps(self):
        self.assertEqual(self.version(tags=['v0.1.1'],bump='minor',force_bump=True),'0.2.0')
        self.assertEqual(self.version(tags=['v0.1.1'],head_tags=['v0.1.1'],bump='major',force_bump=True),'1.0.0')
        self.assertEqual(self.version(tags=['v1.0.0'],head_tags=['v1.0.0'],bump='major'),'1.0.0')
    def test_new_baseline_and_preview_versions(self):
        self.assertEqual(self.version(base='0.2.0',tags=['v0.1.9']),'0.2.0')
        self.assertEqual(self.version(release=False),'0.1.0+git7.g1234abcd')
        self.assertEqual(self.version(tags=['v0.2.0'],release=False),'0.2.0+git7.g1234abcd')
    def test_reject_invalid_versions(self):
        for value in ('v0.1.0; echo nope','1.2','01.2.3'):
            with self.assertRaises(ValueError):parse_version(value)
        with self.assertRaises(ValueError):self.version(release=False,sha='bad/sha')

@unittest.skipUnless(shutil.which('dpkg-deb'),'dpkg-deb is required for archive tests')
class PackageTests(unittest.TestCase):
    def test_reproducible_package_contains_working_cli_and_documentation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            first=build('0.1.0',root/'first')
            second=build('0.1.0',root/'second')
            self.assertEqual(hashlib.sha256(first.read_bytes()).digest(),hashlib.sha256(second.read_bytes()).digest())
            self.assertEqual(verify(first),'0.1.0')
            self.assertTrue(first.with_suffix('.deb.sha256').exists())

if __name__=='__main__':unittest.main()
