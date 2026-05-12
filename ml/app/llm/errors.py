"""LLM client error type — carries an HTTP status code so FastAPI
handlers can re-raise with the original upstream semantics."""


class LLMClientError(Exception):
    def __init__(self, message: str, status_code: int = 502):
        self.message = message
        self.status_code = status_code
        super().__init__(message)
