import pytest

from instance import config as instance_config

TEST_INSTANCE = """
name = "Test Librarian"
label = "TEST"
command_prefix = "test"
id_prefix = "test"
"""


@pytest.fixture(autouse=True)
def test_instance(tmp_path, monkeypatch):
    """Run every test against a neutral instance unless a test overrides it."""
    path = tmp_path / "test_instance.toml"
    path.write_text(TEST_INSTANCE)
    monkeypatch.setenv(instance_config.INSTANCE_ENV, str(path))
    monkeypatch.delenv(instance_config.REGISTRY_DIR_ENV, raising=False)
    instance_config.clear_cache()
    yield path
    instance_config.clear_cache()
