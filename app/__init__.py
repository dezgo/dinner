"""Dinner Tab — read the menu, record orders, split the bill, get paid back."""

import shutil
import subprocess
from pathlib import Path


def _release() -> str:
    """v<number of commits>: goes up by one with every change pushed, no bumping by hand.

    Shown at the foot of the organiser app so it's easy to tell a deploy has landed,
    and used to make browsers fetch fresh CSS/JS after each deploy.
    """
    git = shutil.which("git")
    if not git:
        return "dev"
    try:
        out = subprocess.run(  # noqa: S603 - fixed arguments, nothing from outside
            [git, "-c", "safe.directory=*", "rev-list", "--count", "HEAD"],
            cwd=Path(__file__).resolve().parent.parent,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        return f"v{int(out.stdout.strip())}"
    except (OSError, ValueError, subprocess.SubprocessError):
        return "dev"


__version__ = _release()
