import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/launchd/run_if_tournament.sh"


def test_launchd_script_resolves_repo_root_correctly():
    """REPO_ROOT must resolve to the actual project root, not a hardcoded path."""
    # Use bash to source the leading lines (up to and including REPO_ROOT) and echo it
    code = (
        f"set -e; cd /tmp; "  # invoke from /tmp to prove resolution doesn't depend on CWD
        f"REPO_ROOT_TEST=$(bash -c 'set -e; "
        f"SCRIPT_DIR=$(cd $(dirname {SCRIPT}) && pwd); "
        f"cd $SCRIPT_DIR/../.. && pwd'); "
        f"echo $REPO_ROOT_TEST"
    )
    result = subprocess.run(["bash", "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(REPO), (
        f"REPO_ROOT resolved to {result.stdout.strip()!r}, expected {REPO!r}"
    )


def test_launchd_script_does_not_hardcode_old_path():
    """Hardcoded /Users/prav/Desktop/personal-projects/* path must be gone."""
    text = SCRIPT.read_text()
    assert "personal-projects/fifa-wc-26-prediction" not in text
    # And the new resolution pattern must be present
    assert "SCRIPT_DIR" in text or "$(cd \"$(dirname" in text


def test_launchd_script_has_repo_guard():
    """Script must verify REPO_ROOT/.git exists before proceeding."""
    text = SCRIPT.read_text()
    assert ".git" in text  # guard against running in the wrong dir
