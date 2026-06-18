"""
Schema definition models for the Structure Inference Agent.
These represent the inferred structure of data.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from enum import Enum
from datetime import datetime
import pandas as pd


class AttributeRole(Enum):
    """Semantic role of a column."""
    DIMENSION = "dimension"  # Qualitative, for grouping/filtering
    METRIC = "metric"        # Quantitative, for aggregation
    TEMPORAL = "temporal"    # Date/time fields
    OTHER = "other"          # Unclassified


class DataType(Enum):
    """Data type of column values."""
    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    DATE = "date"
    DATETIME = "datetime"
    BOOLEAN = "boolean"
    CURRENCY = "currency"
    UNKNOWN = "unknown"


@dataclass
class SourceCoordinates:
    """Tracks where a column originated in the source file."""
    sheet: str
    col_index: int
    col_header: str
    original_range: Optional[str] = None


@dataclass
class ColumnSchema:
    """
    Schema definition for a single column.
    """
    name: str
    data_type: DataType
    role: AttributeRole
    confidence: float = 1.0
    nullable: bool = True
    format: Optional[str] = None  # For dates: "%Y-%m-%d"
    description: Optional[str] = None
    example_values: List[Any] = field(default_factory=list)
    source_coordinates: Optional[SourceCoordinates] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON output."""
        result = {
            "name": self.name,
            "type": self.data_type.value,
            "role": self.role.value,
            "confidence": self.confidence,
            "nullable": self.nullable
        }
        if self.format:
            result["format"] = self.format
        if self.description:
            result["description"] = self.description
        if self.example_values:
            result["example_values"] = self.example_values[:5]
        if self.source_coordinates:
            result["source_coordinates"] = {
                "sheet": self.source_coordinates.sheet,
                "col_header": self.source_coordinates.col_header
            }
        return result


@dataclass
class InferredSchema:
    """
    Complete schema definition for a dataset.
    This is the primary metadata output of the SIA.
    """
    schema_name: str
    version: str = "1.0"
    fields: List[ColumnSchema] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    source_file: Optional[str] = None
    total_rows: int = 0
    sheets_processed: List[str] = field(default_factory=list)
    overall_confidence: float = 0.0
    
    def add_field(self, column: ColumnSchema) -> None:
        """Add a column to the schema."""
        self.fields.append(column)
        self._update_confidence()
    
    def _update_confidence(self) -> None:
        """Recalculate overall confidence as average of field confidences."""
        if self.fields:
            self.overall_confidence = sum(f.confidence for f in self.fields) / len(self.fields)
    
    def get_dimensions(self) -> List[ColumnSchema]:
        """Get all dimension columns."""
        return [f for f in self.fields if f.role == AttributeRole.DIMENSION]
    
    def get_metrics(self) -> List[ColumnSchema]:
        """Get all metric columns."""
        return [f for f in self.fields if f.role == AttributeRole.METRIC]
    
    def get_temporal_columns(self) -> List[ColumnSchema]:
        """Get all temporal columns."""
        return [f for f in self.fields if f.role == AttributeRole.TEMPORAL]
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to JSON Schema-like dictionary."""
        return {
            "schema_name": self.schema_name,
            "version": self.version,
            "created_at": self.created_at,
            "source_file": self.source_file,
            "total_rows": self.total_rows,
            "sheets_processed": self.sheets_processed,
            "overall_confidence": round(self.overall_confidence, 3),
            "fields": [f.to_dict() for f in self.fields]
        }
    
    def to_json(self, indent: int = 2) -> str:
        """Convert to JSON string."""
        import json
        from datetime import date, datetime
        
        def json_serial(obj):
            """JSON serializer for objects not serializable by default json code"""
            if isinstance(obj, (datetime, date)):
                return obj.isoformat()
            return str(obj)

        return json.dumps(self.to_dict(), indent=indent, default=json_serial)
    
    @classmethod
    def from_dataframe(cls, df: pd.DataFrame, name: str = "inferred_schema") -> 'InferredSchema':
        """
        Create a schema from a pandas DataFrame by analyzing its columns.
        """
        schema = cls(schema_name=name, total_rows=len(df))
        
        for i, col_name in enumerate(df.columns):
            # Use iloc to ensure we get a Series even with duplicate column names
            series = df.iloc[:, i]
            
            # Robustness: ensure we have a Series (iloc[:, i] should be Series, but just in case)
            if isinstance(series, pd.DataFrame):
                series = series.iloc[:, 0]
            
            # Basic type detection
            if hasattr(series, 'dtype') and series.dtype == 'object':
                data_type = DataType.STRING
            elif 'int' in str(series.dtype).lower():
                data_type = DataType.INTEGER
            elif 'float' in str(series.dtype).lower():
                data_type = DataType.FLOAT
            elif 'datetime' in str(series.dtype).lower():
                data_type = DataType.DATETIME
            elif 'bool' in str(series.dtype).lower():
                data_type = DataType.BOOLEAN
            else:
                data_type = DataType.UNKNOWN
                
            # Basic role detection (very naive, usually refined by LLM)
            if data_type in [DataType.INTEGER, DataType.FLOAT]:
                role = AttributeRole.METRIC
            elif data_type in [DataType.DATETIME, DataType.DATE]:
                role = AttributeRole.TEMPORAL
            else:
                role = AttributeRole.DIMENSION
                
            column = ColumnSchema(
                name=str(col_name),
                data_type=data_type,
                role=role,
                confidence=0.5, # Heuristic confidence is low
                example_values=series.dropna().unique()[:5].tolist()
            )
            schema.add_field(column)
            
        return schema

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'InferredSchema':
        """Create schema from dictionary."""
        schema = cls(
            schema_name=data.get("schema_name", "unnamed"),
            version=data.get("version", "1.0"),
            source_file=data.get("source_file"),
            total_rows=data.get("total_rows", 0),
            sheets_processed=data.get("sheets_processed", [])
        )
        
        for field_data in data.get("fields", []):
            col = ColumnSchema(
                name=field_data["name"],
                data_type=DataType(field_data.get("type", "unknown")),
                role=AttributeRole(field_data.get("role", "other")),
                confidence=field_data.get("confidence", 1.0),
                nullable=field_data.get("nullable", True),
                format=field_data.get("format"),
                description=field_data.get("description")
            )
            schema.add_field(col)
        
        return schema
