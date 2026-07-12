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
JOLINK_WINDOWS_INSTALLER = "https://7355608.net/jolink/install.ps1"


def test_installers_clone_the_jolink_repository() -> None:
    assert f'REPO_URL_HTTPS="{JOLINK_HTTPS}"' in INSTALL_SH
    assert f'REPO_URL_SSH="{JOLINK_SSH}"' in INSTALL_SH
    assert f'$RepoUrlHttps = "{JOLINK_HTTPS}"' in INSTALL_PS1
    assert f'$RepoUrlSsh = "{JOLINK_SSH}"' in INSTALL_PS1


def test_installers_publish_jolink_one_liners() -> None:
    assert f"{JOLINK_RAW}/install.sh" in INSTALL_SH
    assert JOLINK_WINDOWS_INSTALLER in INSTALL_PS1
    assert JOLINK_WINDOWS_INSTALLER in INSTALL_SH
    assert JOLINK_WINDOWS_INSTALLER in DOGFOOD_GUIDE
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
    archive_base = "https://codeload.github.com/L1ch404/hermes-agent/zip"
    assert f'{archive_base}/$Commit' in INSTALL_PS1
    assert f'{archive_base}/refs/tags/$Tag' in INSTALL_PS1
    assert f'{archive_base}/refs/heads/$Branch' in INSTALL_PS1
    assert "-TimeoutSec 180" in INSTALL_PS1
    assert (
        f'{archive_base}/refs/heads/{{branch}}' in
        Path(hermes_main.__file__).read_text(encoding="utf-8")
    )
    mirror_base = "https://7355608.net/jolink"
    assert f'$RepoArchiveMirror = "{mirror_base}/main.zip"' in INSTALL_PS1
    assert f'$RepoArchiveMirrorSha256 = "{mirror_base}/main.zip.sha256"' in INSTALL_PS1
    main_source = Path(hermes_main.__file__).read_text(encoding="utf-8")
    assert f'mirror_url = "{mirror_base}/main.zip"' in main_source
    assert f'mirror_hash_url = "{mirror_base}/main.zip.sha256"' in main_source
    assert "SHA-256 verified" in INSTALL_PS1
    assert "SHA-256 verified" in main_source


def test_windows_repository_fallback_order_avoids_unnecessary_ssh_errors() -> None:
    https_pos = INSTALL_PS1.index('Write-Info "Trying HTTPS clone..."')
    mirror_pos = INSTALL_PS1.index('Write-Info "Trying joLink China mirror..."')
    codeload_pos = INSTALL_PS1.index('Write-Info "Trying GitHub codeload..."')
    ssh_pos = INSTALL_PS1.index("trying SSH clone as the final fallback")
    assert https_pos < mirror_pos < codeload_pos < ssh_pos


def test_windows_zip_fallback_creates_a_valid_git_head() -> None:
    assert "git add -A" in INSTALL_PS1
    assert '"user.name=joLink Installer"' in INSTALL_PS1
    assert '-m "Bootstrap joLink from codeload archive"' in INSTALL_PS1
    assert "$usedZipFallback = $true" in INSTALL_PS1
    assert "-not $usedZipFallback" in INSTALL_PS1


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


def test_default_install_does_not_request_ffmpeg() -> None:
    assert "function Install-SystemPackages {\n    param([switch]$IncludeFfmpeg)" in INSTALL_PS1
    assert "function Stage-SystemPackages   { Install-SystemPackages }" in INSTALL_PS1
    assert "Install-SystemPackages -IncludeFfmpeg" in INSTALL_PS1
    assert 'Title = "Installing ripgrep"' in INSTALL_PS1

    assert 'local include_ffmpeg="${1:-false}"' in INSTALL_SH
    assert "install_system_packages true" in INSTALL_SH
    assert "    install_system_packages\n" in INSTALL_SH


def test_default_install_does_not_download_chromium() -> None:
    assert "[switch]$IncludeBrowser" in INSTALL_PS1
    assert "if ($browserNpmOk -and $IncludeBrowser)" in INSTALL_PS1
    assert "function Stage-NodeDeps         { Install-NodeDeps }" in INSTALL_PS1
    assert "pass -IncludeBrowser to install" in INSTALL_PS1

    assert "SKIP_BROWSER=true" in INSTALL_SH
    assert "--with-browser)" in INSTALL_SH
    assert "SKIP_BROWSER=false" in INSTALL_SH
    assert "use --with-browser to install" in INSTALL_SH
