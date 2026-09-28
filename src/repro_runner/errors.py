"""Expected workflow validation failures."""


class ValidationError(ValueError):
    """A workflow violates the declared schema or filesystem contract."""
