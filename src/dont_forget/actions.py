from __future__ import annotations

import re
import tomllib
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
        readme = path / "README.md"
        readme_text = readme.read_text(encoding="utf-8") if readme.exists() else ""
        setup_commands = self._derive_setup_commands(path)
        return RepositoryEvidence(
            repository=str(path),
            is_public=(path / ".public").exists(),
            has_demo=any((path / name).exists() for name in demo_names),
            has_readme=readme.exists(),
            has_useful_setup=bool(
                re.search(r"^#{1,6}\s+Setup\s*$", readme_text, re.MULTILINE | re.IGNORECASE)
                and setup_commands
                and all(command in readme_text for command in setup_commands)
            ),
            setup_commands=setup_commands,
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

    def repair_readme_setup(self, repository: str | Path) -> tuple[Path, bool]:
        repository_path = self._allowed_path(repository)
        readme = repository_path / "README.md"
        contents = readme.read_text(encoding="utf-8") if readme.exists() else ""
        commands = self._derive_setup_commands(repository_path)
        if not commands:
            raise ValueError("No setup commands could be derived from the project configuration.")
        has_setup = bool(
            re.search(r"^#{1,6}\s+Setup\s*$", contents, re.MULTILINE | re.IGNORECASE)
        )
        missing_commands = [command for command in commands if command not in contents]
        if has_setup and not missing_commands:
            return readme, False

        if not contents:
            project_name = self._python_project_name(repository_path) or repository_path.name
            contents = f"# {project_name.replace('-', ' ').title()}\n"
        contents = contents.rstrip() + "\n\n"
        if not has_setup:
            contents += "## Setup\n\n"
        commands_to_add = missing_commands if has_setup else commands
        contents += "```text\n" + "\n".join(commands_to_add) + "\n```\n"
        readme.write_text(contents, encoding="utf-8")
        return readme, True

    def _derive_setup_commands(self, repository: Path) -> list[str]:
        if not (repository / "pyproject.toml").exists() or not (repository / "uv.lock").exists():
            return []
        project = self._read_pyproject(repository)
        test_dependencies = project.get("project", {}).get("optional-dependencies", {}).get(
            "test", []
        )
        commands = ["uv sync --extra test"] if test_dependencies else ["uv sync"]
        if any(str(dependency).split("[")[0].lower().startswith("pytest") for dependency in test_dependencies):
            commands.append("uv run pytest")
        return commands

    def _python_project_name(self, repository: Path) -> str | None:
        if not (repository / "pyproject.toml").exists():
            return None
        name = self._read_pyproject(repository).get("project", {}).get("name")
        return str(name) if name else None

    @staticmethod
    def _read_pyproject(repository: Path) -> dict[str, object]:
        with (repository / "pyproject.toml").open("rb") as project_file:
            return tomllib.load(project_file)

    def _allowed_path(self, path: str | Path) -> Path:
        resolved = Path(path).resolve()
        if not any(
            resolved == workspace or resolved.is_relative_to(workspace)
            for workspace in self.allowed_workspaces
        ):
            raise PermissionError(f"Path is outside the allowed workspaces: {resolved}")
        return resolved
