"""I7: the safety exports in /data/backups survive a container recreate."""
import xml.etree.ElementTree as ET
from pathlib import Path

from bot.backup.export import DEFAULT_BACKUP_DIR

ROOT = Path(__file__).resolve().parent.parent


def test_unraid_template_maps_the_backup_folder() -> None:
    tree = ET.parse(ROOT / "unraid" / "ravebot.xml")
    paths = [c for c in tree.getroot().iter("Config") if c.get("Type") == "Path"]
    assert [c.get("Target") for c in paths] == [DEFAULT_BACKUP_DIR]
    assert paths[0].get("Default", "").startswith("/mnt/user/appdata/")


def test_image_has_a_writable_backup_folder() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert f"mkdir -p {DEFAULT_BACKUP_DIR}" in dockerfile
    assert f"chown ravebot {DEFAULT_BACKUP_DIR}" in dockerfile
    assert f'VOLUME ["{DEFAULT_BACKUP_DIR}"]' in dockerfile


def test_docs_name_backup_dir_and_the_volume() -> None:
    readme = (ROOT / "README.md").read_text()
    assert "| `BACKUP_DIR` |" in readme
    assert f"-v ravebot-backups:{DEFAULT_BACKUP_DIR}" in readme
    for guide in ("developer-guide.md", "user-guide.md"):
        assert "BACKUP_DIR" in (ROOT / "docs" / guide).read_text(), guide
