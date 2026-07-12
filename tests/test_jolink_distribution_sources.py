from pathlib import Path

from hermes_cli import banner
from hermes_cli import main as hermes_main


REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = (REPO_ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
INSTALL_PS1 = (REPO_ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")
DOGFOOD_GUIDE = (REPO_ROOT / "JOLINK_DOGFOOD.md").read_text(encoding="utf-8")

JOLINK_HTTPS = "https://github.com/L1ch404/hermes-agent.git"
JOLINK_SSH = "git@github.com:L1ch404/hermes-agent.git"
JOLINK_RAW = "https://raw.githubusercontent.com/L1ch404/hermes-agent/main/scripts"


def test_installers_clone_the_jolink_repository() -> None:
    assert f'REPO_URL_HTTPS="{JOLINK_HTTPS}"' in INSTALL_SH
    assert f'REPO_URL_SSH="{JOLINK_SSH}"' in INSTALL_SH
    assert f'$RepoUrlHttps = "{JOLINK_HTTPS}"' in INSTALL_PS1
    assert f'$RepoUrlSsh = "{JOLINK_SSH}"' in INSTALL_PS1


def test_installers_publish_jolink_one_liners() -> None:
    assert f"{JOLINK_RAW}/install.sh" in INSTALL_SH
    assert f"{JOLINK_RAW}/install.ps1" in INSTALL_PS1
    assert "joLink Installer" in INSTALL_SH
    assert "joLink Installer" in INSTALL_PS1


def test_installers_convert_existing_hermes_checkouts_to_jolink() -> None:
    assert 'git remote set-url origin "$REPO_URL_HTTPS"' in INSTALL_SH
    assert 'git checkout -B "$BRANCH" "origin/$BRANCH"' in INSTALL_SH
    assert "pre-jolink-" in INSTALL_SH
    assert "Switching existing installation to the joLink release repository." in INSTALL_SH
    assert "remote set-url origin $RepoUrlHttps" in INSTALL_PS1
    assert 'checkout -B $Branch "origin/$Branch"' in INSTALL_PS1
    assert "pre-jolink-" in INSTALL_PS1
    assert "Switching existing installation to the joLink release repository." in INSTALL_PS1


def test_windows_zip_fallback_uses_jolink_archives() -> None:
    archive_base = "https://github.com/L1ch404/hermes-agent/archive"
    assert f'{archive_base}/$Commit.zip' in INSTALL_PS1
    assert f'{archive_base}/refs/tags/$Tag.zip' in INSTALL_PS1
    assert f'{archive_base}/refs/heads/$Branch.zip' in INSTALL_PS1


def test_runtime_update_sources_are_jolink() -> None:
    assert banner._UPSTREAM_REPO_URL == JOLINK_HTTPS
    assert banner._OFFICIAL_REPO_CANONICAL == "github.com/l1ch404/hermes-agent"
    assert banner._RELEASE_URL_BASE == "https://github.com/L1ch404/hermes-agent/releases/tag"
    assert JOLINK_HTTPS in hermes_main.OFFICIAL_REPO_URLS
    assert JOLINK_SSH in hermes_main.OFFICIAL_REPO_URLS
    assert hermes_main._is_fork(JOLINK_HTTPS) is False
    assert hermes_main._is_fork(JOLINK_SSH) is False


def test_dogfood_guide_includes_first_time_quick_start() -> None:
    assert "# joLink 内测与快速开始指南" in DOGFOOD_GUIDE
    assert "hermes setup model" in DOGFOOD_GUIDE
    assert "hermes tools --summary" in DOGFOOD_GUIDE
    assert "`/new`" in DOGFOOD_GUIDE
    assert "`java_runtime`" in DOGFOOD_GUIDE
