from pathlib import Path


_REPO = Path(__file__).resolve().parent.parent
_INSTALLER = _REPO / "scripts" / "install_tunnel.sh"


def test_posix_tunnel_launcher_is_private_before_it_receives_the_token() -> None:
    source = _INSTALLER.read_text(encoding="utf-8")

    umask = source.index("umask 077")
    directory_create = source.index('mkdir -p "$MERIDIAN_DIR"')
    directory_harden = source.index('chmod 700 "$MERIDIAN_DIR"')
    old_launcher_harden = source.index('chmod 600 "$LAUNCHER"')
    write_launcher = source.index('} > "$LAUNCHER"')
    launcher_executable = source.index('chmod 700 "$LAUNCHER"', write_launcher)

    assert umask < directory_create < directory_harden
    assert directory_harden < old_launcher_harden < write_launcher < launcher_executable
    assert "printf 'export MERIDIAN_API_KEY=%q\\n'" in source
    assert "printf 'export MERIDIAN_URL=%q\\n'" in source
    assert 'export MERIDIAN_API_KEY="${MERIDIAN_API_KEY}"' not in source
