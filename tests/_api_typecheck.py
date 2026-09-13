"""Run the pinned checker outside the checkout against one consumer's imports."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

FIXTURES = Path(__file__).parent / "fixtures" / "api_typing"


def check_api_consumer(tmp_path: Path, *, python: Path | str = sys.executable) -> None:
    project = tmp_path / "api-consumer"
    project.mkdir()
    for source in FIXTURES.glob("*.py"):
        shutil.copyfile(source, project / source.name)
    config = project / "pyrightconfig.json"
    config.write_text(
        json.dumps(
            {
                "include": ["valid.py", "invalid.py"],
                "typeCheckingMode": "standard",
                "pythonVersion": "3.12",
                "extraPaths": [],
                "useLibraryCodeForTypes": False,
                "reportMissingTypeStubs": "error",
            }
        )
    )
    # Ignore developer overrides that can switch the checker version, trigger
    # a runtime download, or make wheel imports resolve to the source checkout.
    env = {key: value for key, value in os.environ.items() if not key.startswith("PYRIGHT_") and key not in {"PYTHONPATH", "VIRTUAL_ENV"}}
    result = subprocess.run(
        [sys.executable, "-m", "pyright", "--project", str(config), "--pythonpath", str(python), "--outputjson"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert not result.stderr, result.stderr
    report = json.loads(result.stdout)
    expected = {
        ("invalid.py", index, line.split("# expect: ")[1].strip())
        for index, line in enumerate((project / "invalid.py").read_text().splitlines())
        if "# expect: " in line
    }
    diagnostics = report["generalDiagnostics"]
    actual = {(Path(item["file"]).name, item["range"]["start"]["line"], item.get("rule")) for item in diagnostics}
    assert actual == expected, result.stdout + result.stderr
    assert len(diagnostics) == len(expected)
    assert all(item["severity"] == "error" for item in diagnostics)
