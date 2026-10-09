import os


def pytest_addoption(parser):
    parser.addoption("--alloy-binary", default=os.environ.get("ALLOY_BINARY", ""))


def pytest_configure(config):
    binary = config.getoption("--alloy-binary")
    if binary:
        os.environ["ALLOY_BINARY"] = binary
