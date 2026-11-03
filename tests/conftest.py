"""Keep the module-level ASGI app away from the user's catalogue during collection."""

import os
from tempfile import TemporaryDirectory


def pytest_configure(config):
    config._rewear_previous_data_dir = os.environ.get("REWEAR_DATA_DIR")
    config._rewear_import_directory = TemporaryDirectory(prefix="rewear-test-import-")
    os.environ["REWEAR_DATA_DIR"] = config._rewear_import_directory.name
    config._rewear_image_search_env = {
        key: os.environ.pop(key, None)
        for key in ("REWEAR_IMAGE_SEARCH", "REWEAR_VISION_API_KEY", "REWEAR_VISION_PROJECT")
    }


def pytest_unconfigure(config):
    directory = getattr(config, "_rewear_import_directory", None)
    if directory is None:
        return
    previous = config._rewear_previous_data_dir
    if previous is None:
        os.environ.pop("REWEAR_DATA_DIR", None)
    else:
        os.environ["REWEAR_DATA_DIR"] = previous
    for key, value in config._rewear_image_search_env.items():
        if value is not None:
            os.environ[key] = value
        else:
            os.environ.pop(key, None)
    directory.cleanup()
