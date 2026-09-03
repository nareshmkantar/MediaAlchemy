"""
Error classes for the Structure Inference Agent.
Provides structured error types with user-facing messages.
"""
from typing import Optional, Dict, Any


class SIAError(Exception):
    """Base exception for SIA errors."""
    
    def __init__(self, message: str, details: Dict[str, Any] = None,
                 user_message: str = None, recoverable: bool = True):
        """
        Initialize SIA error.
        
        Args:
            message: Technical error message
            details: Additional error details
            user_message: User-friendly error message
            recoverable: Whether the error can be recovered from
        """
        super().__init__(message)
        self.details = details or {}
        self.user_message = user_message or message
        self.recoverable = recoverable
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for logging/serialization."""
        return {
            "error_type": self.__class__.__name__,
            "message": str(self),
            "user_message": self.user_message,
            "details": self.details,
            "recoverable": self.recoverable
        }


class CorruptFileError(SIAError):
    """Raised when the input file is corrupt or unreadable."""
    
    def __init__(self, file_path: str, original_error: str = None):
        super().__init__(
            message=f"Corrupt or unreadable file: {file_path}",
            details={"file_path": file_path, "original_error": original_error},
            user_message=f"The file '{file_path}' could not be read. It may be corrupt or in an unsupported format.",
            recoverable=False
        )


class AmbiguousHeaderError(SIAError):
    """Raised when header detection is ambiguous."""
    
    def __init__(self, sheet_name: str, candidate_rows: list):
        super().__init__(
            message=f"Ambiguous header in sheet: {sheet_name}",
            details={"sheet_name": sheet_name, "candidate_rows": candidate_rows},
            user_message=f"Multiple possible header rows detected in '{sheet_name}'. Please review and select the correct header row.",
            recoverable=True
        )


class DateParseError(SIAError):
    """Raised when date parsing fails for a column."""
    
    def __init__(self, column_name: str, failed_values: list, detected_format: str = None):
        super().__init__(
            message=f"Date parsing failed for column: {column_name}",
            details={
                "column_name": column_name,
                "failed_values": failed_values[:5],
                "detected_format": detected_format
            },
            user_message=f"Some dates in column '{column_name}' could not be parsed. Failed values will be set to null.",
            recoverable=True
        )


class LLMError(SIAError):
    """Raised when LLM inference fails."""
    
    def __init__(self, operation: str, original_error: str = None, details: Dict[str, Any] = None, user_message: str = None):
        super().__init__(
            message=f"LLM inference failed during: {operation}",
            details=details or {"operation": operation, "original_error": original_error},
            user_message=user_message or f"AI analysis failed. The system will use rule-based fallback for {operation}.",
            recoverable=True
        )
