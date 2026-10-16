"""Package-manager uninstall detection remains independent of scanner availability."""
import pytest

from tools.approval import detect_dangerous_command


class TestPackageManagerUninstallApproval:
    """Package-manager removal verbs remove software outside the project (#10199)."""

    @pytest.mark.parametrize("command", [
        "npm uninstall -g left-pad", "npm r left-pad", "pnpm un -g left-pad",
        "yarn global remove left-pad", "pip3 uninstall left-pad", "brew rm left-pad",
        "npm --prefix ./app uninstall left-pad", "pip --proxy http://p:1 uninstall -y requests",
        "cd app && yarn --cwd ./app remove left-pad",
    ])
    def test_uninstall_requires_approval(self, command):
        dangerous, key, _ = detect_dangerous_command(command)
        assert dangerous and key == "package manager uninstall"

    @pytest.mark.parametrize("command", [
        "npm update -g left-pad", "pnpm add left-pad", "yarn install", "pip install left-pad", "brew upgrade left-pad",
        'git commit -m "document npm uninstall usage"', 'echo "pip uninstall foo"',
    ])
    def test_install_and_update_stay_unprompted(self, command):
        assert detect_dangerous_command(command) == (False, None, None)
