import importlib


def test_watchdog_engine_imports_after_reorg():
    module = importlib.import_module("core.watchdog.engine")
    assert hasattr(module, "WatchdogEngine")


def test_new_cli_modules_import():
    negrisk_cli = importlib.import_module("apps.negrisk.__main__")
    watchdog_cli = importlib.import_module("apps.watchdog.__main__")
    assert hasattr(negrisk_cli, "main")
    assert hasattr(watchdog_cli, "main")


def test_new_watchdog_platform_package_imports():
    module = importlib.import_module("core.watchdog.platforms.kalshi")
    assert hasattr(module, "KalshiWatchdogEngine")
