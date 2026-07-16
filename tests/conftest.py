from __future__ import annotations

from collections.abc import Callable
import errno
from pathlib import Path

import pytest


@pytest.fixture
def symlink_or_skip() -> Callable[..., None]:
    """Create a symlink or skip when the host does not grant that capability."""

    def create(
        link: Path,
        target: Path,
        *,
        target_is_directory: bool = False,
    ) -> None:
        try:
            link.symlink_to(target, target_is_directory=target_is_directory)
        except NotImplementedError as error:
            pytest.skip(f"symlink creation is unavailable on this host: {error}")
        except OSError as error:
            unsupported_errnos = {
                errno.EACCES,
                errno.ENOSYS,
                errno.EPERM,
                getattr(errno, "EOPNOTSUPP", errno.ENOTSUP),
            }
            if getattr(error, "winerror", None) == 1314 or error.errno in unsupported_errnos:
                pytest.skip(f"symlink creation is unavailable on this host: {error}")
            raise

    return create
