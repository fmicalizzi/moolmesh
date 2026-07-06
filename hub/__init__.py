"""MoolMesh — the context mesh for AI coding agents."""

try:
    from importlib.metadata import version as _v
    __version__ = _v("moolmesh")
except Exception:
    __version__ = "dev"
USER_AGENT = f"moolmesh/{__version__}"
