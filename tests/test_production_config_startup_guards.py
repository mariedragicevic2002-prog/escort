import core.production_config as pc


def test_local_secret_files_warn_but_continue_in_production(monkeypatch):
    monkeypatch.setenv("DEBUG", "false")
    monkeypatch.delenv("ALLOW_LOCAL_SECRET_FILES", raising=False)
    monkeypatch.setattr(
        pc.os.path,
        "isfile",
        lambda p: str(p).endswith("\\.env") or str(p).endswith("\\credentials.json"),
    )

    ok, msg = pc._check_local_secret_files_absent()

    # Function warns but continues startup to avoid a full-site outage
    assert ok is True
    assert msg is None


def test_local_secret_files_guard_can_be_bypassed_for_emergency(monkeypatch):
    monkeypatch.setenv("DEBUG", "false")
    monkeypatch.setenv("ALLOW_LOCAL_SECRET_FILES", "true")
    monkeypatch.setattr(pc.os.path, "isfile", lambda _p: True)

    ok, msg = pc._check_local_secret_files_absent()

    assert ok is True
    assert msg is None


def test_admin_password_rejects_placeholder(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "changeme")

    ok, msg = pc._check_admin_password()

    assert ok is False
    assert msg is not None
