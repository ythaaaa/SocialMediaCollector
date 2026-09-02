__all__ = ["DownloadEngine"]


def __getattr__(name: str):
    if name == "DownloadEngine":
        from .engine import DownloadEngine
        return DownloadEngine
    raise AttributeError(name)
