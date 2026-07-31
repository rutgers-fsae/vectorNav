from pathlib import Path
import unittest


SCRIPT = (Path(__file__).parents[1] / "deploy" / "install-rpi.sh").read_text()
DOCKERFILE = (Path(__file__).parents[1] / "deploy" / "Dockerfile.wheel").read_text()


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

    def test_prebuilt_wheel_is_validated_and_installed(self):
        self.assertIn('--vectornav-wheel', SCRIPT)
        self.assertIn('linux_aarch64|manylinux_', SCRIPT)
        self.assertIn('wheel:$(sha256sum', SCRIPT)
        self.assertIn('Installing the prebuilt VectorNav extension', SCRIPT)

    def test_docker_build_targets_arm64_cpython_313(self):
        self.assertIn('ARG PYTHON_VERSION=3.13', DOCKERFILE)
        self.assertIn('python:${PYTHON_VERSION}-slim-bookworm', DOCKERFILE)
        self.assertIn('VECTORNAV_BUILD_PLUGINS=0', DOCKERFILE)


if __name__ == "__main__":
    unittest.main()
