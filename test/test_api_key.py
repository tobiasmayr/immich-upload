"""Where the API key comes from: option, environment, key file."""

import os

import pytest

import media
from conftest import write
from fake_immich import API_KEY


@pytest.fixture
def server_only(immich, monkeypatch):
    """The fake server, but no key in the environment."""
    monkeypatch.delenv("IMMICH_API_KEY")
    return immich


def key_file(path, text=API_KEY + "\n"):
    write(path, text.encode())
    path.chmod(0o600)
    return path


def test_no_key_anywhere(run, server_only, tmp_path):
    rc, _ = run("--list-albums")
    assert rc == 2


def test_api_key_file_option(run, server_only, tmp_path):
    rc, _ = run("--api-key-file", key_file(tmp_path / "k"), "--list-albums")
    assert rc == 0


def test_api_key_file_env(run, server_only, tmp_path, monkeypatch):
    monkeypatch.setenv("IMMICH_API_KEY_FILE", str(key_file(tmp_path / "k")))
    assert run("--list-albums")[0] == 0


def test_default_key_file(run, server_only, own_config_dir):
    key_file(own_config_dir / "immich-upload" / "api-key")
    assert run("--list-albums")[0] == 0


def test_option_beats_environment(run, immich, tmp_path, monkeypatch):
    monkeypatch.setenv("IMMICH_API_KEY", "wrong")
    assert run("--api-key-file", key_file(tmp_path / "k"), "--list-albums")[0] == 0


def test_environment_beats_default_file(run, immich, own_config_dir):
    key_file(own_config_dir / "immich-upload" / "api-key", "wrong\n")
    assert run("--list-albums")[0] == 0  # IMMICH_API_KEY from the fixture wins


def test_first_line_only_and_bom(run, server_only, tmp_path):
    k = key_file(tmp_path / "k", "﻿  " + API_KEY + "  \nsecond line\n")
    assert run("--api-key-file", k, "--list-albums")[0] == 0


@pytest.mark.parametrize("text", ["", "\n\n  \n"])
def test_empty_key_file(run, server_only, tmp_path, text):
    k = key_file(tmp_path / "k", text)
    assert run("--api-key-file", k, "--list-albums")[0] == 2


def test_missing_key_file(run, server_only, tmp_path):
    assert run("--api-key-file", tmp_path / "nope", "--list-albums")[0] == 2


def test_both_options(run, server_only, tmp_path):
    assert run("--api-key", API_KEY, "--api-key-file", key_file(tmp_path / "k"), "--list-albums")[0] == 2


@pytest.mark.skipif(os.name == "nt", reason="file modes are a POSIX thing")
def test_warns_about_readable_key_file(run, server_only, tmp_path):
    k = key_file(tmp_path / "k")
    k.chmod(0o644)
    rc, _ = run("--api-key-file", k, "--list-albums")
    assert rc == 0 and "others on this computer can read" in run.err


def test_list_files_works_without_key(run, no_server, tmp_path):
    write(tmp_path / "f" / "a.jpg", media.jpeg(media.tiff("2019:07:14 15:30:00")))
    assert run("-L", tmp_path / "f")[0] == 0


def test_missing_url_is_reported_on_its_own(run, monkeypatch, tmp_path):
    monkeypatch.delenv("IMMICH_URL", raising=False)
    monkeypatch.setenv("IMMICH_API_KEY", API_KEY)
    rc, _ = run("--list-albums")
    assert rc == 2 and "no Immich URL" in run.err and "API key" not in run.err


def test_missing_key_is_reported_on_its_own(run, server_only, own_config_dir):
    rc, _ = run("--list-albums")
    assert rc == 2 and "no API key" in run.err and "URL" not in run.err
    assert str(own_config_dir / "immich-upload" / "api-key") in run.err
