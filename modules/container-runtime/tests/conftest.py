"""Pytest fixtures for the container tests.

The helpers and fixtures live in `container_testcaps` at the module root rather
than in this file. They were here, imported as `from conftest import ...`, which
resolves to whichever conftest is nearest — the repository root's, once every
module is collected in one run. A uniquely named module at the module root has
one meaning in both contexts.

Fixtures must be visible in a conftest to be discovered, so they are imported
here by name.
"""

from container_testcaps import (  # noqa: F401
    busybox_layer,
    find_busybox,
    image_store,
    make_layer,
    requires_busybox,
    requires_overlayfs,
    userns_capable,
    requires_userns,
    workspace,
    write_image,
)
