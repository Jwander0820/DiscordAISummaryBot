"""Exercise deployment decisions with fake Docker; never contacts a Docker daemon."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


BASH = shutil.which("bash")
if not BASH and Path("C:/Software/Git/bin/bash.exe").is_file():
    BASH = "C:/Software/Git/bin/bash.exe"
SCRIPT = Path(__file__).resolve().parents[1] / "tools/deploy/discord-bot-deploy.sh"
SHA = "a" * 40
OLD_IMAGE = "discord-summary-bot:rehearsal-b2f1ea4"

HARNESS = r'''
if command -v cygpath >/dev/null 2>&1; then
    DEPLOY_SCRIPT=$(cygpath -u "$DEPLOY_SCRIPT")
    CASE_DIR=$(cygpath -u "$CASE_DIR")
fi
source "$DEPLOY_SCRIPT"
APP_DIR="$CASE_DIR/app"
STATE_DIR="$CASE_DIR/state"
mkdir -p "$APP_DIR"
touch "$APP_DIR/compose.yaml"
id() { printf '0\n'; }
install() { mkdir -p "${@: -1}"; }
flock() { return 0; }
docker() {
    printf '%s\n' "$*" >> "$CASE_DIR/calls"
    case "$1" in
        inspect)
            if [[ "$3" == '{{.State.Running}}' ]]; then
                printf 'true\n'
            else
                printf 'discord-summary-bot:rehearsal-b2f1ea4\n'
            fi
            ;;
        pull)
            [[ "$FAILURE" != pull ]]
            ;;
        compose)
            if [[ " $* " == *' up '* && "$FAILURE" == health ]] &&
                grep -q 'ghcr.io/' "$STATE_DIR/compose.image.yaml"; then
                return 1
            fi
            ;;
        *) return 99 ;;
    esac
}
main "$TEST_COMMAND"
'''


@unittest.skipUnless(BASH, "Bash required for isolated deployment script tests")
class DeployScriptTests(unittest.TestCase):
    def run_deploy(self, command, failure=""):
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ, DEPLOY_SCRIPT=str(SCRIPT), CASE_DIR=directory,
                       TEST_COMMAND=command, FAILURE=failure)
            result = subprocess.run([BASH, "-c", HARNESS], env=env, text=True,
                                    capture_output=True, timeout=20)
            root = Path(directory)
            calls = (root / "calls").read_text() if (root / "calls").exists() else ""
            override = root / "state/compose.image.yaml"
            image = override.read_text() if override.exists() else ""
            return result, calls, image

    def test_rejects_shell_injection_before_docker(self):
        result, calls, image = self.run_deploy(f"deploy {SHA}; echo injected")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(calls, "")
        self.assertEqual(image, "")

    def test_check_does_not_pull_or_recreate(self):
        result, calls, image = self.run_deploy("check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("pull ", calls)
        self.assertNotIn(" up ", calls)
        self.assertEqual(image, "")

    def test_pull_failure_keeps_existing_deployment(self):
        result, calls, image = self.run_deploy(f"deploy {SHA}", "pull")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(" up ", calls)
        self.assertEqual(image, "")

    def test_success_only_recreates_bot_with_tested_tag(self):
        result, calls, image = self.run_deploy(f"deploy {SHA}")
        self.assertEqual(result.returncode, 0, result.stderr)
        updates = [line for line in calls.splitlines() if " up " in line]
        self.assertEqual(len(updates), 1)
        self.assertIn("--no-deps --no-build --wait --wait-timeout 120 bot", updates[0])
        self.assertIn(f"sha-{SHA}", image)
        self.assertNotIn(" down", calls)

    def test_health_failure_restores_previous_image_but_reports_failure(self):
        result, calls, image = self.run_deploy(f"deploy {SHA}", "health")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(sum(" up " in line for line in calls.splitlines()), 2)
        self.assertIn(OLD_IMAGE, image)
        self.assertIn("Previous image restored", result.stderr)


if __name__ == "__main__":
    unittest.main()
