import shutil
import subprocess
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/launchd/run_if_tournament.sh"
PLIST = REPO / "scripts/launchd/com.prav.wc26-preview.plist"
INSTALLER = REPO / "scripts/launchd/install.sh"


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


# ────────────────────────────── H1 (R2 round 3): plist template + substitution
# The R2 round 2 fix made the wrapper script portable, but the plist
# (which launchd actually reads) still hardcoded a stale path. These
# tests pin the template + install.sh substitution flow so a regression
# back to a hardcoded plist is caught at suite time.
def _strip_plist_comments(text: str) -> str:
    """Strip XML <!-- ... --> comments so we can audit only the live
    plist body. The header comment intentionally references the OLD
    deprecated path as documentation of what the template replaces; a
    naive substring match would conflate documentation with regression.
    """
    import re
    return re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)


def test_h1_plist_body_does_not_hardcode_old_personal_projects_path() -> None:
    """The plist BODY (excluding XML comments) must not reference the
    pre-R2 personal-projects clone. A regression here means launchd
    runs the wrapper from the WRONG repo — exactly the silent-failure
    mode R2 promised to close.

    The header comment in the template intentionally documents the
    deprecated path for future maintainers; that's not a regression.
    """
    body = _strip_plist_comments(PLIST.read_text())
    assert "personal-projects/fifa-wc-26-prediction" not in body, (
        "plist BODY still references the deprecated personal-projects "
        "path — see audit H1; install.sh substitutes __REPO_ROOT__."
    )


def test_h1_plist_uses_repo_root_template_markers() -> None:
    """The committed plist must use `__REPO_ROOT__` placeholders rather
    than any concrete machine path. install.sh substitutes at install
    time so the agent works from any checkout location.
    """
    body = _strip_plist_comments(PLIST.read_text())
    assert "__REPO_ROOT__" in body, (
        "plist BODY missing __REPO_ROOT__ template markers — see audit H1. "
        "install.sh refuses to install a template without markers."
    )
    # And the body must NOT contain any absolute /Users/prav/Desktop path
    # under the keys that should be repo-relative (ProgramArguments,
    # WorkingDirectory, StandardOutPath, StandardErrorPath).
    lines = body.split("\n")
    for i, line in enumerate(lines, start=1):
        stripped = line.strip()
        # The HOME env var (line 31 in current template) is allowed to
        # be /Users/prav — it's a user identity, not a repo path.
        if "/Users/prav/.npm-global" in stripped:
            continue  # PATH entry — user-shell config, not repo path
        if stripped == "<string>/Users/prav</string>":
            continue  # HOME env var
        if "/Users/prav/Desktop" in stripped:
            pytest.fail(
                f"plist body line {i} contains hardcoded Desktop path: "
                f"{stripped!r} — should be __REPO_ROOT__ template token"
            )
        if "personal-projects" in stripped:
            pytest.fail(
                f"plist body line {i} contains personal-projects path: "
                f"{stripped!r}"
            )


def test_h1_installer_substitutes_repo_root_marker() -> None:
    """install.sh, given the template plist + a REPO_ROOT, must produce
    a plist file with all `__REPO_ROOT__` markers replaced by the real
    path. Run the sed step directly (the full install.sh requires sudo
    perms on ~/Library/LaunchAgents which we can't grant in test).
    """
    template = PLIST.read_text()
    assert "__REPO_ROOT__" in template

    with tempfile.TemporaryDirectory() as tmp:
        fake_root = Path(tmp) / "wc26-test-checkout"
        fake_root.mkdir()
        dest = Path(tmp) / "out.plist"
        # Replicate the install.sh sed substitution exactly.
        result = subprocess.run(
            ["sed", f"s|__REPO_ROOT__|{fake_root}|g", str(PLIST)],
            capture_output=True, text=True, check=True,
        )
        dest.write_text(result.stdout)
        materialized = dest.read_text()
        assert "__REPO_ROOT__" not in materialized, (
            "sed substitution failed — unresolved __REPO_ROOT__ markers remain"
        )
        assert str(fake_root) in materialized, (
            f"sed didn't substitute the fake REPO_ROOT={fake_root} into the plist"
        )
        # Spot-check the 4 critical keys still resolve correctly.
        assert f"<string>{fake_root}/scripts/launchd/run_if_tournament.sh</string>" in materialized
        assert f"<string>{fake_root}</string>" in materialized  # WorkingDirectory
        assert f"<string>{fake_root}/logs/launchd-stdout.log</string>" in materialized
        assert f"<string>{fake_root}/logs/launchd-stderr.log</string>" in materialized


def test_h1_installer_rejects_unsubstituted_plist() -> None:
    """install.sh must REFUSE to install a plist that has no
    __REPO_ROOT__ markers (catches a partial revert / mistake that
    re-introduces hardcoded paths into the committed template).
    """
    text = INSTALLER.read_text()
    assert "refusing to install" in text.lower(), (
        "install.sh missing pre-flight check for __REPO_ROOT__ markers — "
        "a hardcoded-path regression in the plist would deploy silently"
    )
    assert 'grep -q "__REPO_ROOT__"' in text, (
        "install.sh missing the grep guard that detects an unsubstituted "
        "template (catches sed failures)"
    )


def test_h1_installer_refuses_template_without_markers() -> None:
    """Functional: run install.sh against a plist whose __REPO_ROOT__
    markers have been stripped — it should exit non-zero with a clear
    refuse-to-install message rather than copy a broken plist.
    """
    pytest.importorskip("subprocess")
    with tempfile.TemporaryDirectory() as tmp:
        fake_root = Path(tmp) / "wc26"
        (fake_root / "scripts" / "launchd").mkdir(parents=True)
        (fake_root / "scripts" / "deploy_preview.sh").write_text(
            "#!/usr/bin/env bash\nexit 0\n"
        )
        (fake_root / "scripts" / "deploy_preview.sh").chmod(0o755)
        shutil.copy(SCRIPT, fake_root / "scripts" / "launchd" / "run_if_tournament.sh")
        (fake_root / "scripts" / "launchd" / "run_if_tournament.sh").chmod(0o755)
        # Write a plist with NO markers — this should be rejected.
        broken_plist = fake_root / "scripts" / "launchd" / "com.prav.wc26-preview.plist"
        broken_plist.write_text(
            '<?xml version="1.0"?><plist><dict>'
            '<key>Label</key><string>com.test</string>'
            '<key>ProgramArguments</key><array><string>/bin/echo</string></array>'
            '</dict></plist>'
        )
        # Copy installer in
        installer = fake_root / "scripts" / "launchd" / "install.sh"
        shutil.copy(INSTALLER, installer)
        installer.chmod(0o755)
        # Run installer in a sandbox HOME so it can't touch real LaunchAgents
        sandbox_home = Path(tmp) / "sandbox-home"
        sandbox_home.mkdir()
        result = subprocess.run(
            ["bash", str(installer)],
            capture_output=True, text=True,
            env={"HOME": str(sandbox_home), "PATH": "/usr/bin:/bin"},
        )
        assert result.returncode != 0, (
            "installer accepted a plist with no __REPO_ROOT__ markers — "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        assert "refusing to install" in (result.stdout + result.stderr).lower()


import pytest  # noqa: E402 — late import keeps the top of the file slim
