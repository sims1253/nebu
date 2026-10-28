"""Errors that cross module boundaries."""


class KittumError(Exception):
    """Base class for errors a caller is expected to handle."""


class NotFoundError(KittumError):
    pass
