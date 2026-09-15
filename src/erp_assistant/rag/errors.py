class RagError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class DocumentValidationError(RagError):
    pass


class PdfValidationError(DocumentValidationError):
    pass


class OcrRequiredError(RagError):
    pass


class RagDependencyError(RagError):
    pass
