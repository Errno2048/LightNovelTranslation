"""Bundle the canonical Agent Skill in wheels; metadata lives in pyproject.toml."""
from pathlib import Path
import shutil

from setuptools import setup
from setuptools.command.build_py import build_py


class BuildWithSkill(build_py):
    def run(self):
        super().run()
        if not self.editable_mode:
            source = Path(__file__).parent / "skills"
            build_root = Path(self.build_lib).resolve()
            target = build_root / "lightnovel_translation/resources/skills"
            if not target.resolve().is_relative_to(build_root):
                raise ValueError("Skill build destination escapes the build directory")
            # Regenerate this directory so deleted skill files cannot leak into a rebuild.
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(source, target)


setup(cmdclass={"build_py": BuildWithSkill})
