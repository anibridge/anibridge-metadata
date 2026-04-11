"""Pytest configuration for the test suite."""

from anibridge.utils.limiter import Limiter


def pytest_configure() -> None:
    """Disable rate limiting across the full test run."""
    Limiter.DISABLED = True


def pytest_unconfigure() -> None:
    """Restore the limiter default after tests finish."""
    Limiter.DISABLED = False
