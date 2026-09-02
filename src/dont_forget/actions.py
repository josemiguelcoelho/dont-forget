from __future__ import annotations

from pathlib import Path
from urllib.request import urlopen

from .models import RepositoryEvidence


class LocalActions:
    def __init__(self, allowed_workspaces: list[str | Path]) -> None:
        self.allowed_workspaces = [Path(path).resolve() for path in allowed_workspaces]

    def read_source(self, source_url: str) -> str:
        with urlopen(source_url, timeout=5) as response:  # noqa: S310 - explicit user source
            return response.read().decode("utf-8")

    def inspect_repository(self, repository: str | Path) -> RepositoryEvidence:
        path = self._allowed_path(repository)
        demo_names = {"DEMO.md", "demo.mp4", "demo.mov", "demo.webm"}
        return RepositoryEvidence(
            repository=str(path),
            is_public=(path / ".public").exists(),
            has_demo=any((path / name).exists() for name in demo_names),
        )

    def create_demo_checklist(self, repository: str | Path) -> tuple[Path, bool]:
        path = self._allowed_path(repository) / "DEMO_CHECKLIST.md"
        if path.exists():
            return path, False
        path.write_text(
            "# Demo checklist\n\n"
            "- [ ] Record the demo video\n"
            "- [ ] Show the REMEMBER → CHECK → ACT flow\n"
            "- [ ] Add the demo link to the README\n"
            "- [ ] Verify the final submission\n",
            encoding="utf-8",
        )
        return path, True

    def _allowed_path(self, path: str | Path) -> Path:
        resolved = Path(path).resolve()
        if not any(
            resolved == workspace or resolved.is_relative_to(workspace)
            for workspace in self.allowed_workspaces
        ):
            raise PermissionError(f"Path is outside the allowed workspaces: {resolved}")
        return resolved
