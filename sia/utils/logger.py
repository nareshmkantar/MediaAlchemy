"""
Logging utilities for the Structure Inference Agent.
Provides structured logging with trace IDs.
"""
import logging
import sys
from typing import Optional


def setup_logging(level: int = logging.INFO, log_file: str = None) -> None:
    """
    Configure logging for the SIA.
    
    Args:
        level: Logging level (default INFO)
        log_file: Optional file path for logging
    """
    # Create formatter
    formatter = logging.Formatter(
        '%(asctime)s | %(levelname)-8s | %(name)s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    
    # Configure root logger
    root_logger = logging.getLogger('sia')
    root_logger.setLevel(level)
    root_logger.addHandler(console_handler)
    
    # File handler if specified
    if log_file:
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)


def get_trace_logger(trace_id: str) -> logging.LoggerAdapter:
    """
    Get a logger adapter that prefixes messages with trace ID.
    
    Args:
        trace_id: The trace ID to prefix
        
    Returns:
        Logger adapter with trace context
    """
    logger = logging.getLogger('sia')
    return logging.LoggerAdapter(logger, {'trace_id': trace_id})
