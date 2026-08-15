"""mini-container-runtime -- containers from namespaces, cgroups and overlayfs.

    from minicon import Container, ContainerConfig, ImageStore, Limits

    store = ImageStore("./images")            # an OCI layout, e.g. from #18
    config = ContainerConfig(image="serve:v1", limits=Limits(memory_bytes=256 << 20))
    with Container(config, store, "./run").create() as container:
        container.start()
        print(container.phases.table())
        code = container.wait()

No runc, no Docker, no privileged helper. Everything runs as an ordinary user
through unprivileged user namespaces.
"""

from .cgroups import Cgroup, CgroupUnavailable, Limits, Usage, delegated_root
from .container import Container, ContainerConfig, ContainerError, ContainerState, Phases
from .idmap import IdMapping, IdRange, plan_for_user, root_mapping, single_mapping, subid_mapping
from .image import Image, ImageStore, LayerCache, run_in_userns, unpack_layer
from .linux import NotSupported, namespace_ids, overlayfs_in_userns, userns_available
from .mounts import Mount, OverlayRoot, default_mounts
from .netlink import Netlink
from .network import Bridge, connect

__version__ = "1.0.0"

__all__ = [
    "Cgroup", "CgroupUnavailable", "Limits", "Usage", "delegated_root",
    "Container", "ContainerConfig", "ContainerError", "ContainerState", "Phases",
    "IdMapping", "IdRange", "plan_for_user", "root_mapping", "single_mapping", "subid_mapping",
    "Image", "ImageStore", "LayerCache", "run_in_userns", "unpack_layer",
    "NotSupported", "namespace_ids", "overlayfs_in_userns", "userns_available",
    "Mount", "OverlayRoot", "default_mounts",
    "Netlink",
    "Bridge", "connect",
]
