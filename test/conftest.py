from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from fake_immich import API_KEY, FakeImmich  # noqa: E402

SCRIPT = Path(__file__).resolve().parent.parent / "immich-upload.py"


def load_script():
    spec = importlib.util.spec_from_file_location("immich_upload", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["immich_upload"] = mod  # dataclasses look their module up here
    spec.loader.exec_module(mod)
    return mod


iu = load_script()


@pytest.fixture(autouse=True)
def own_config_dir(monkeypatch, tmp_path):
    """Each test gets an empty config folder, so a real key file on this machine is never read."""
    cfg = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg))
    monkeypatch.setenv("APPDATA", str(cfg))
    monkeypatch.delenv("IMMICH_API_KEY_FILE", raising=False)
    return cfg


@pytest.fixture
def mod():
    return iu


@pytest.fixture
def immich(monkeypatch):
    fake = FakeImmich(iu.MEDIA_EXT).start()
    monkeypatch.setenv("IMMICH_URL", fake.url)
    monkeypatch.setenv("IMMICH_API_KEY", API_KEY)
    yield fake
    fake.stop()


@pytest.fixture
def no_server(monkeypatch):
    monkeypatch.delenv("IMMICH_URL", raising=False)
    monkeypatch.delenv("IMMICH_API_KEY", raising=False)


class Run:
    """Runs the script's main() in this process. `answer` is what the user says when asked
    (None: no question may come up, the test fails if one does)."""

    def __init__(self, monkeypatch, capsys, log: Path):
        self.monkeypatch, self.capsys, self.log = monkeypatch, capsys, log
        self.questions: list[str] = []

    def __call__(self, *args, answer: bool | None = None) -> tuple[int, str]:
        def confirm(question):
            self.questions.append(question)
            if answer is None:
                raise AssertionError(f"unexpected question: {question}")
            return answer

        self.monkeypatch.setattr(iu, "confirm", confirm)
        self.monkeypatch.setattr(sys, "argv", ["immich-upload.py", "--log", str(self.log), *map(str, args)])
        try:
            rc = iu.main()
        except SystemExit as e:
            rc = e.code if isinstance(e.code, int) else 2
        out = self.capsys.readouterr()
        self.err = out.err  # warnings and errors
        return rc, out.out


@pytest.fixture
def run(monkeypatch, capsys, tmp_path):
    return Run(monkeypatch, capsys, tmp_path / "log.md")


def write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path
