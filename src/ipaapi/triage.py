"""Sort input files into ``submitted/`` and ``failed/`` as a batch is processed.

Submitting dozens of files against a finite analysis allowance means a run can
stop partway through for a reason that is nobody's fault. Re-running then risks
resubmitting work that already succeeded, burning allowance twice.

So each file is moved as its fate becomes known:

``submitted/``
    IPA accepted it and returned analysis IDs. Done; do not resubmit.

``failed/``
    The file itself is the problem -- it failed validation, or IPA rejected it
    for a reason that will recur. A ``.error.txt`` note is written beside it
    explaining what went wrong.

left in place
    The allowance was exhausted. Nothing is wrong with the file, so it stays
    where it is and is picked up by the next run.

Files are moved, never copied, so the source directory shrinks to exactly the
work still outstanding.
"""

from __future__ import annotations

import pathlib
import shutil
from typing import List, Optional

__all__ = ["Triage", "SUBMITTED_DIRNAME", "FAILED_DIRNAME", "TRIAGE_DIRNAMES"]

SUBMITTED_DIRNAME = "submitted"
FAILED_DIRNAME = "failed"

#: Never treated as input, so a second run does not pick up its own output.
TRIAGE_DIRNAMES = frozenset({SUBMITTED_DIRNAME, FAILED_DIRNAME})


class Triage:
    """Moves files into ``submitted/`` and ``failed/`` under *root*.

    Args:
        root: Directory the files were found in. The destination folders are
            created inside it, on first use only -- a run that triages nothing
            leaves no empty directories behind.
        dry_run: Report the moves that would happen without performing any.
    """

    def __init__(self, root: pathlib.Path, dry_run: bool = False) -> None:
        self.root = pathlib.Path(root)
        self.dry_run = dry_run
        self.submitted_dir = self.root / SUBMITTED_DIRNAME
        self.failed_dir = self.root / FAILED_DIRNAME
        self.submitted: List[pathlib.Path] = []
        self.failed: List[pathlib.Path] = []
        self.left: List[pathlib.Path] = []

    # -- outcomes ----------------------------------------------------------

    def mark_submitted(self, path: pathlib.Path) -> Optional[pathlib.Path]:
        """Move *path* into ``submitted/``.

        Recorded only if the move actually happened. Appending first meant
        ``summary()`` reported files as moved directly beneath the warnings
        saying they had been left in place -- and the summary is the run's
        statement of what still needs doing.
        """
        destination = self._move(path, self.submitted_dir)
        if destination is not None:
            self.submitted.append(pathlib.Path(path))
        else:
            self.left.append(pathlib.Path(path))
        return destination

    def mark_failed(
        self, path: pathlib.Path, reason: str
    ) -> Optional[pathlib.Path]:
        """Move *path* into ``failed/`` and write a note explaining *reason*."""
        destination = self._move(path, self.failed_dir)
        if destination is None:
            self.left.append(pathlib.Path(path))
            return None
        self.failed.append(pathlib.Path(path))
        if not self.dry_run:
            self._write_note(destination, reason)
        return destination

    def mark_left(self, path: pathlib.Path) -> None:
        """Record that *path* stays put, for the next run to pick up."""
        self.left.append(pathlib.Path(path))

    # -- mechanics ---------------------------------------------------------

    def _move(
        self, path: pathlib.Path, destination_dir: pathlib.Path
    ) -> Optional[pathlib.Path]:
        path = pathlib.Path(path)
        target = destination_dir / path.name
        if self.dry_run:
            return target
        try:
            destination_dir.mkdir(parents=True, exist_ok=True)
            target = _unique(target)
            shutil.move(str(path), str(target))
            return target
        except OSError as exc:
            # Never let a filing problem lose an analysis IPA has accepted.
            print(
                f"Warning: could not move {path.name!r} to "
                f"{destination_dir.name}/ ({exc}). The file has been left in place."
            )
            return None

    @staticmethod
    def _write_note(moved_to: pathlib.Path, reason: str) -> None:
        # _unique, like the move itself: TABLE_PATTERNS matches *.error.txt, so
        # a note dragged back out of failed/ can be re-ingested as input on a
        # later run, quarantined, and then have its own note written over it.
        note = _unique(moved_to.with_suffix(moved_to.suffix + ".error.txt"))
        try:
            note.write_text(reason.rstrip() + "\n", encoding="utf-8")
        except OSError:
            pass  # The message was printed too; the note is a convenience.

    # -- reporting ---------------------------------------------------------

    def summary(self) -> str:
        """One-line-per-category summary, suitable for the end of a run."""
        verb = "would be moved" if self.dry_run else "moved"
        lines = []
        if self.submitted:
            lines.append(f"{len(self.submitted)} file(s) {verb} to {SUBMITTED_DIRNAME}/")
        if self.failed:
            lines.append(f"{len(self.failed)} file(s) {verb} to {FAILED_DIRNAME}/")
        if self.left:
            lines.append(
                f"{len(self.left)} file(s) left in place for the next run"
            )
        return "\n".join(lines)


def _unique(target: pathlib.Path) -> pathlib.Path:
    """Return a path that does not exist, suffixing ``-1``, ``-2`` as needed."""
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    for n in range(1, 1000):
        candidate = target.with_name(f"{stem}-{n}{suffix}")
        if not candidate.exists():
            return candidate
    return target.with_name(f"{stem}-{id(target)}{suffix}")
