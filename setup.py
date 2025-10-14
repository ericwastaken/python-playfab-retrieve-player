#!/usr/bin/env python3
import os
import sys
import subprocess
from pathlib import Path
from setuptools import setup, find_packages
from setuptools.command.install import install as _install

ROOT = Path(__file__).parent
REQ_FILE = ROOT / "requirements.txt"
VENV_DIR = ROOT / ".venv"


def read_requirements():
    if REQ_FILE.exists():
        try:
            lines = REQ_FILE.read_text(encoding="utf-8").splitlines()
            return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]
        except Exception:
            pass
    # Fallback to the minimal known runtime deps
    return [
        "click>=8.0",
        "PyYAML>=6.0",
        "requests>=2.25",
    ]


def read_readme():
    for name in ("README.md", "README.rst", "README.txt"):
        p = ROOT / name
        if p.exists():
            try:
                return p.read_text(encoding="utf-8")
            except Exception:
                break
    return (
        "python-playfab-retrieve-player: CLI to retrieve PlayFab player information via "
        "LoginWithCustomID for a list of custom IDs from a CSV."
    )


class install(_install):
    """Custom install that also creates a local venv and installs requirements into it.

    Note: Creating a venv at install time is unconventional, but provided here to mirror
    the referenced example. Any failure here will not fail the package installation.
    """

    def run(self):
        super().run()
        try:
            self._create_venv()
        except Exception as e:
            print(f"[playfab-retrieve] Warning: Failed to create local .venv: {e}", file=sys.stderr)

    def _create_venv(self):
        import venv

        if VENV_DIR.exists():
            print(f"[playfab-retrieve] Existing venv found at {VENV_DIR}")
            return
        print(f"[playfab-retrieve] Creating venv at {VENV_DIR} ...")
        builder = venv.EnvBuilder(with_pip=True, clear=False)
        builder.create(str(VENV_DIR))

        # Determine python/pip inside the venv
        if os.name == "nt":
            pip_bin = VENV_DIR / "Scripts" / "pip.exe"
        else:
            pip_bin = VENV_DIR / "bin" / "pip"

        # Upgrade pip and install requirements from requirements.txt when present
        try:
            subprocess.check_call([str(pip_bin), "install", "--upgrade", "pip", "setuptools", "wheel"]) 
            if REQ_FILE.exists():
                print(f"[playfab-retrieve] Installing requirements from {REQ_FILE} into .venv ...")
                subprocess.check_call([str(pip_bin), "install", "-r", str(REQ_FILE)])
        except subprocess.CalledProcessError as e:
            print(f"[playfab-retrieve] Warning: pip install inside .venv failed: {e}", file=sys.stderr)


setup(
    name="playfab-retrieve-player",
    version="0.1.0",
    description=(
        "CLI to retrieve PlayFab player information via LoginWithCustomID for a list of custom IDs from a CSV"
    ),
    long_description=read_readme(),
    long_description_content_type="text/markdown",
    author="",
    author_email="",
    url="https://github.com/ericwastaken/python-playfab-retrieve-player",
    project_urls={
        "Issue Tracker": "https://github.com/ericwastaken/python-playfab-retrieve-player/issues",
        "Source": "https://github.com/ericwastaken/python-playfab-retrieve-player",
    },
    license="MIT",
    packages=find_packages(include=["playfab_retrieve_player", "playfab_retrieve_player.*"], exclude=["tests*", "data*"] ),
    python_requires=">=3.8",
    install_requires=read_requirements(),
    include_package_data=True,
    entry_points={
        "console_scripts": [
            "playfab-retrieve-player=playfab_retrieve_player.cli:main",
        ]
    },
    cmdclass={
        "install": install,
    },
    keywords=[
        "PlayFab",
        "LoginWithCustomID",
    ],
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "Topic :: Software Development :: Libraries :: Application Frameworks",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3 :: Only",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Operating System :: OS Independent",
    ],
    zip_safe=False,
)
