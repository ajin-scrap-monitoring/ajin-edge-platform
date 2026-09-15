"""Exercise the release shell sequence against an immutable-release CLI boundary."""

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GIT_BASH = Path("C:/Program Files/Git/bin/bash.exe")
BASH = str(GIT_BASH) if os.name == "nt" and GIT_BASH.exists() else shutil.which("bash")


@pytest.mark.skipif(not BASH, reason="release workflow requires bash")
@pytest.mark.parametrize("fail_upload", [False, True])
def test_release_remains_draft_until_assets_uploaded(fail_upload):
    workflow = (ROOT / ".github/workflows/release-images.yml").read_text()
    step = workflow.split("      - name: Attach deployment references to GitHub release\n", 1)[1]
    script = textwrap.dedent(step.split("        run: |\n", 1)[1])
    # Actual GitHub rejected upload after publication with HTTP 422. Model that boundary
    # while running the real workflow shell, including its fail-fast behavior.
    boundary = r"""
    release_state=missing
    assets=missing
    trap 'echo "FINAL:$release_state:$assets"' EXIT
    gh() {
      case "$1 $2" in
        "release view") test "$release_state" != missing ;;
        "release create")
          case " $* " in
            *" --draft "*) release_state=draft ;;
            *) release_state=published ;;
          esac ;;
        "release upload")
          test "$release_state" = draft || return 22
          test "$FAIL_UPLOAD" != yes || return 23
          assets=attached ;;
        "release edit")
          test "$release_state" = draft && test "$assets" = attached || return 24
          case " $* " in
            *" --draft=false "*) release_state=published ;;
            *) return 25 ;;
          esac ;;
        *) return 26 ;;
      esac
    }
    """
    result = subprocess.run(
        [BASH, "-e", "-c", textwrap.dedent(boundary) + script],
        env={**os.environ, "VERSION": "v9.8.7", "FAIL_UPLOAD": "yes" if fail_upload else "no"},
        capture_output=True,
        text=True,
        timeout=10,
    )
    if fail_upload:
        assert result.returncode == 23, result.stdout + result.stderr
        assert "FINAL:draft:missing" in result.stdout
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert "FINAL:published:attached" in result.stdout
