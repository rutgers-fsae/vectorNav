from pathlib import Path
import unittest


SCRIPT = (Path(__file__).parents[1] / "deploy" / "install-rpi.sh").read_text()


class InstallRpiTests(unittest.TestCase):
    def test_native_build_uses_fast_pi_compiler_flags(self):
        self.assertIn('CXXFLAGS="-O0 -g0"', SCRIPT)
        self.assertIn('/usr/local/bin/uv --verbose pip install', SCRIPT)

    def test_native_build_is_fingerprinted_and_can_be_forced(self):
        self.assertIn('VECTORNAV_BUILD_STAMP=', SCRIPT)
        self.assertIn('VECTORNAV_BUILD_HASH=', SCRIPT)
        self.assertIn('"${VENV_DIR}/bin/python" -c "import vectornav"', SCRIPT)
        self.assertIn('--rebuild', SCRIPT)
        self.assertIn('FORCE_REBUILD=1', SCRIPT)


if __name__ == "__main__":
    unittest.main()
