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
    assert f"chown ravebot:ravebot {DEFAULT_BACKUP_DIR}" in dockerfile
    assert f'VOLUME ["{DEFAULT_BACKUP_DIR}"]' in dockerfile
    # A mapped host folder created as root is handed to ravebot before the bot starts
    assert 'ENTRYPOINT ["ravebot-entrypoint"]' in dockerfile
    entrypoint = (ROOT / "docker" / "entrypoint.sh").read_text()
    assert 'chown ravebot:ravebot "$dir"' in entrypoint
    assert "setpriv --reuid=ravebot --regid=ravebot --init-groups" in entrypoint
    # HOME must point at ravebot's own folder, not /root (asyncpg stats ~/.postgresql)
    assert "export HOME=/home/ravebot" in entrypoint
    assert entrypoint.index("export HOME=/home/ravebot") < entrypoint.index("exec setpriv")
    assert "--create-home --home-dir /home/ravebot" in dockerfile


def test_docs_name_backup_dir_and_the_volume() -> None:
    readme = (ROOT / "README.md").read_text()
    assert "| `BACKUP_DIR` |" in readme
    assert f"-v ravebot-backups:{DEFAULT_BACKUP_DIR}" in readme
    for guide in ("developer-guide.md", "user-guide.md"):
        assert "BACKUP_DIR" in (ROOT / "docs" / guide).read_text(), guide


def test_no_image_is_published_before_it_has_started_like_in_production() -> None:
    """A release needs the container smoke test and the real-SQL tests first."""
    release = (ROOT / "scripts" / "release.sh").read_text()
    assert "scripts/smoke-container.sh" in release
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text()
    image_job = workflow.split("\n  image:\n", 1)[1].split("\n  release:\n", 1)[0]
    assert "needs: [test, real-sql, smoke]" in image_job
    assert "scripts/smoke-container.sh" in workflow
    assert "pytest -m pg" in workflow
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "scripts/smoke-container.sh" in ci


def test_smoke_script_covers_the_production_start() -> None:
    script = (ROOT / "scripts" / "smoke-container.sh").read_text()
    for needle in (
        "uid 1000",  # runs as the non-root user
        "/data/backups",  # the mapped, root-owned folder
        "alembic",  # migrations in the image
        "write_safety_export",  # a safety export is written
        "Migrations complete",  # the real start-up
        "improper token",  # reaches the Discord login
    ):
        assert needle in script, needle
