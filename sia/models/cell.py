"""
Cell and VisualGrid data models for the Structure Inference Agent.
These represent the normalized view of Excel data after preprocessing.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from enum import Enum
import pandas as pd


class CellType(Enum):
    """Type of cell content."""
    EMPTY = "empty"
    TEXT = "text"
    NUMBER = "number"
    DATE = "date"
    BOOLEAN = "boolean"
    FORMULA = "formula"
    ERROR = "error"


@dataclass
class CellStyle:
    """Visual style attributes of a cell."""
    is_bold: bool = False
    is_italic: bool = False
    bg_color: Optional[str] = None  # Hex color e.g., 'FF0000'
    font_color: Optional[str] = None
    font_size: Optional[int] = None
    indent_level: int = 0
    is_merged: bool = False
    merge_range: Optional[str] = None  # e.g., 'A1:C1'


@dataclass
class Cell:
    """
    Represents a single cell in the visual grid.
    Contains value, position, style, and type information.
    """
    value: Any
    row: int  # 0-indexed
    col: int  # 0-indexed
    style: CellStyle = field(default_factory=CellStyle)
    original_type: CellType = CellType.EMPTY
    original_coordinate: str = ""  # e.g., 'A1'
    
    def is_empty(self) -> bool:
        """Check if cell is empty or None."""
        return self.value is None or str(self.value).strip() == ""
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "value": self.value,
            "row": self.row,
            "col": self.col,
            "type": self.original_type.value,
            "coordinate": self.original_coordinate,
            "is_bold": self.style.is_bold,
            "is_merged": self.style.is_merged
        }


@dataclass
class VisualGrid:
    """
    A 2D grid representation of an Excel sheet.
    This is the standardized format that all modules operate on.
    """
    cells: List[List[Cell]]
    sheet_name: str
    total_rows: int
    total_cols: int
    merged_ranges: List[str] = field(default_factory=list)
    hidden_rows: List[int] = field(default_factory=list)
    hidden_cols: List[int] = field(default_factory=list)
    
    @property
    def data(self) -> List[List[Any]]:
        """Return the grid as a nested list of raw values."""
        return [[cell.value for cell in row] for row in self.cells]
    
    def to_dataframe(self) -> 'pd.DataFrame':
        """Convert the grid to a pandas DataFrame."""
        return pd.DataFrame(self.data)

    def get_cell(self, row: int, col: int) -> Optional[Cell]:
        """Get cell at specific position."""
        if 0 <= row < self.total_rows and 0 <= col < self.total_cols:
            return self.cells[row][col]
        return None
    
    def get_row(self, row_idx: int) -> List[Cell]:
        """Get all cells in a row."""
        if 0 <= row_idx < self.total_rows:
            return self.cells[row_idx]
        return []
    
    def get_column(self, col_idx: int) -> List[Cell]:
        """Get all cells in a column."""
        if 0 <= col_idx < self.total_cols:
            return [row[col_idx] for row in self.cells]
        return []
    
    def get_subgrid(self, start_row: int, end_row: int, 
                    start_col: int, end_col: int) -> 'VisualGrid':
        """Extract a rectangular region as a new VisualGrid."""
        sub_cells = []
        for r in range(start_row, min(end_row + 1, self.total_rows)):
            row_cells = []
            for c in range(start_col, min(end_col + 1, self.total_cols)):
                cell = self.cells[r][c]
                # Create new cell with relative coordinates
                new_cell = Cell(
                    value=cell.value,
                    row=r - start_row,
                    col=c - start_col,
                    style=cell.style,
                    original_type=cell.original_type,
                    original_coordinate=cell.original_coordinate
                )
                row_cells.append(new_cell)
            sub_cells.append(row_cells)
        
        return VisualGrid(
            cells=sub_cells,
            sheet_name=self.sheet_name,
            total_rows=len(sub_cells),
            total_cols=len(sub_cells[0]) if sub_cells else 0
        )
    
    def to_text_grid(self, max_rows: int = 20, max_cols: int = 10) -> str:
        """
        Convert to text representation for LLM processing.
        Truncates to max_rows x max_cols for context efficiency.
        """
        lines = []
        for r in range(min(self.total_rows, max_rows)):
            row_values = []
            for c in range(min(self.total_cols, max_cols)):
                cell = self.cells[r][c]
                val = str(cell.value) if cell.value is not None else ""
                # Mark bold cells
                if cell.style.is_bold:
                    val = f"**{val}**"
                row_values.append(val[:30])  # Truncate long values
            lines.append(" | ".join(row_values))
        
        if self.total_rows > max_rows:
            lines.append(f"... ({self.total_rows - max_rows} more rows)")
        
        return "\n".join(lines)
    
    def to_smart_summary(
        self,
        max_cols: int = 200,
        header_rows: int = 5,
        samples_per_anchor: int = 2,
        max_anchor_rows: int = 220,
        max_sparse_column_lines: int = 48,
        max_cell_chars: int = 25,
    ) -> str:
        """
        Generate a smart structural summary for LLM processing.

        1. Always includes the first N header rows
        2. Includes anchor rows (column 0 non-empty), subsampled when there are many
           (e.g. every data row filled in column 0 would otherwise embed the whole sheet)
        3. Includes a few sample data rows after each *included* anchor

        The footer never lists tens of thousands of row indices (that alone can exceed context limits).
        """

        def _sample_sorted_anchors(sorted_anchors: List[int], max_n: int) -> List[int]:
            if len(sorted_anchors) <= max_n:
                return sorted_anchors
            head = max(1, max_n // 3)
            tail = max(1, max_n // 4)
            mid_budget = max_n - head - tail
            first = sorted_anchors[:head]
            last = sorted_anchors[-tail:]
            middle_slice = sorted_anchors[head : len(sorted_anchors) - tail]
            if mid_budget <= 0 or not middle_slice:
                return sorted(set(first + last))[:max_n]
            step = max(1, len(middle_slice) // mid_budget)
            sampled_mid = middle_slice[::step][:mid_budget]
            return sorted(set(first + sampled_mid + last))[:max_n]

        lines = []
        lines.append(f"=== GRID STRUCTURE SUMMARY ===")
        lines.append(f"Total: {self.total_rows} rows × {self.total_cols} columns")
        lines.append("")
        
        # Identify all anchor rows (Column 0 non-empty)
        anchor_rows = set()
        for r in range(self.total_rows):
            cell = self.cells[r][0] if self.total_cols > 0 else None
            if cell and not cell.is_empty():
                anchor_rows.add(r)

        sorted_all_anchors = sorted(anchor_rows)
        display_anchor_list = _sample_sorted_anchors(sorted_all_anchors, max_anchor_rows)
        display_anchors = set(display_anchor_list)
        
        # Build set of rows to include
        rows_to_include = set()
        
        # 1. Always include header rows (first N rows)
        for r in range(min(header_rows, self.total_rows)):
            rows_to_include.add(r)
        
        # 2. Include (subsampled) anchor rows for the text body
        rows_to_include.update(display_anchors)
        
        # 3. Include sample data rows after each included anchor
        sorted_anchors = sorted(display_anchors)
        for anchor_row in sorted_anchors:
            for offset in range(1, samples_per_anchor + 1):
                sample_row = anchor_row + offset
                if sample_row < self.total_rows and sample_row not in anchor_rows:
                    rows_to_include.add(sample_row)
        
        # Sort and output
        sorted_rows = sorted(rows_to_include)
        
        lines.append(f"--- HEADER REGION (rows 0-{header_rows-1}) ---")
        header_section_done = False
        
        prev_row = -1
        for r in sorted_rows:
            # Add section separator when transitioning from header to anchor region
            if not header_section_done and r >= header_rows:
                lines.append("")
                sub = (
                    f"{len(display_anchors)} of {len(anchor_rows)} anchors shown"
                    if len(display_anchors) < len(anchor_rows)
                    else f"{len(anchor_rows)} anchors found in Column 0"
                )
                lines.append(f"--- ANCHOR ROWS + SAMPLES ({sub}) ---")
                header_section_done = True
            
            # Show gap indicator if rows were skipped
            if prev_row >= 0 and r > prev_row + 1:
                skipped = r - prev_row - 1
                if skipped > 0:
                    lines.append(f"  ... ({skipped} rows omitted)")
            
            # Format the row
            row_values = []
            for c in range(min(self.total_cols, max_cols)):
                cell = self.cells[r][c]
                val = str(cell.value) if cell.value is not None else ""
                # Mark bold cells
                if cell.style.is_bold:
                    val = f"**{val}**"
                # Truncate long values
                row_values.append(val[:max_cell_chars])
            
            # Mark anchor rows with indicator
            is_anchor = r in anchor_rows
            prefix = "[A]" if is_anchor else "   "
            lines.append(f"{prefix}[{r:3d}] {' | '.join(row_values)}")
            
            prev_row = r
        
        # Summary footer
        lines.append("")
        lines.append(f"--- Column Statistics ---")
        sparse_lines = 0
        for c in range(self.total_cols):
            if sparse_lines >= max_sparse_column_lines:
                rest = self.total_cols - c
                if rest > 0:
                    lines.append(f"- ... ({rest} more columns not listed; use grid column counts above)")
                break
            col_cells = self.get_column(c)
            populated = sum(1 for cell in col_cells if not cell.is_empty())
            # Only report if it's suspicious (mostly empty or totally empty)
            if populated == 0:
                lines.append(f"- Col {c}: EMPTY (0/{self.total_rows} populated)")
                sparse_lines += 1
            elif populated < self.total_rows * 0.1:
                lines.append(f"- Col {c}: Sparse ({populated}/{self.total_rows} populated)")
                sparse_lines += 1
        
        lines.append("")
        lines.append("--- SUMMARY ---")
        lines.append(
            f"Anchor rows (column 0 non-empty): total={len(anchor_rows)} "
            f"included_in_body={len(display_anchors)} (cap={max_anchor_rows})."
        )
        if len(display_anchors) < len(anchor_rows):
            lines.append(
                "Dense first-column data: anchors were subsampled (head, tail, uniform stride) for LLM context limits."
            )
        
        return "\n".join(lines)
    
    def calculate_density(self, start_row: int, end_row: int,
                          start_col: int, end_col: int) -> float:
        """Calculate the density of non-empty cells in a region."""
        total = 0
        non_empty = 0
        for r in range(start_row, min(end_row + 1, self.total_rows)):
            for c in range(start_col, min(end_col + 1, self.total_cols)):
                total += 1
                if not self.cells[r][c].is_empty():
                    non_empty += 1
        return non_empty / total if total > 0 else 0.0


@dataclass
class DataBlock:
    """
    Represents a detected data table region within a sheet.
    """
    sheet_name: str
    start_row: int
    end_row: int
    start_col: int
    end_col: int
    header_row: int
    confidence: float
    block_type: str = "main_table"  # main_table, metadata, summary
    features_used: List[str] = field(default_factory=list)
    
    @property
    def range_string(self) -> str:
        """Get Excel-style range string (e.g., 'A1:F20')."""
        start_col_letter = self._col_to_letter(self.start_col)
        end_col_letter = self._col_to_letter(self.end_col)
        return f"{start_col_letter}{self.start_row + 1}:{end_col_letter}{self.end_row + 1}"
    
    @staticmethod
    def _col_to_letter(col: int) -> str:
        """Convert 0-indexed column number to Excel letter."""
        result = ""
        while col >= 0:
            result = chr(col % 26 + ord('A')) + result
            col = col // 26 - 1
        return result
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON output."""
        return {
            "sheet": self.sheet_name,
            "range": self.range_string,
            "header_row": self.header_row,
            "confidence": self.confidence,
            "type": self.block_type,
            "features": self.features_used
        }
