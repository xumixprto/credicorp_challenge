class ApplicationError(Exception):
    """An expected failure whose message is safe to return to the frontend."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
