"""Read SolidWorks part files without SolidWorks."""

from .container import Blob, Container, ContainerError, open_container

__version__ = "0.0.1"
__all__ = ["Blob", "Container", "ContainerError", "open_container"]
