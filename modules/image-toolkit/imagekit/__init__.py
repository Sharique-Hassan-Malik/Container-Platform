"""image-toolkit -- OCI images for model servers, built from scratch.

    from imagekit import ImageStore, Builder

    store = ImageStore("./images")
    result = Builder(store, context=".").build(open("Dockerfile").read(), "serve:v1")
    print(result.summary())

No Docker daemon, no registry client, no container library: layers are tar+gzip,
names are SHA-256, and the manifest is JSON. That is the entire format.
"""

from .build import BuildCache, Builder, BuildError, BuildResult, HostExecutor, human, lint
from .coldstart import ColdStartResult, drop_page_cache, measure
from .config import HealthCheck, ImageConfig
from .digest import Descriptor, chain_id, digest_of
from .dockerfile import parse as parse_dockerfile
from .layer import Layer, extract_layer, layer_from_directory, make_layer
from .manifest import Index, Manifest
from .store import ImageStore, TransferStats, copy_image

__version__ = "1.0.0"

__all__ = [
    "BuildCache", "Builder", "BuildError", "BuildResult", "HostExecutor", "human", "lint",
    "ColdStartResult", "drop_page_cache", "measure",
    "HealthCheck", "ImageConfig",
    "Descriptor", "chain_id", "digest_of",
    "parse_dockerfile",
    "Layer", "extract_layer", "layer_from_directory", "make_layer",
    "Index", "Manifest",
    "ImageStore", "TransferStats", "copy_image",
]
