import pandas as pd

from sia.agent.demarcator import StructureDemarcator


def build_demarcator() -> StructureDemarcator:
    return StructureDemarcator(llm_client=None)


def test_extract_candidate_blocks_finds_separated_regions():
    df = pd.DataFrame(
        [
            ["Campaign", "Impressions", None, None, None],
            ["A", 100, None, None, None],
            ["B", 120, None, None, None],
            [None, None, None, None, None],
            [None, None, None, "Notes", None],
            [None, None, None, "Source: platform export", None],
        ]
    )

    blocks = build_demarcator().extract_candidate_blocks(df)

    assert len(blocks) == 2

    first = blocks[0]["coordinates"]
    second = blocks[1]["coordinates"]

    assert first == {"start_row": 0, "end_row": 2, "start_col": 0, "end_col": 1, "header_row": 0}
    assert second["start_row"] == 4
    assert second["end_row"] == 5
    assert second["start_col"] == 3
    assert second["end_col"] == 3


def test_extract_candidate_blocks_can_skip_intro_row_for_header_candidate():
    df = pd.DataFrame(
        [
            ["Meta Delivery Report", None, None],
            ["Date", "Publisher", "Impressions"],
            ["2025-07-01", "Meta", 1000],
            ["2025-07-02", "Meta", 1200],
        ]
    )

    blocks = build_demarcator().extract_candidate_blocks(df)

    assert len(blocks) == 1
    block = blocks[0]

    assert block["category"] == "Main Data"
    assert block["coordinates"]["header_row"] == 1
    assert block["python_detection"]["header_row_candidate"] == 1


def test_extract_candidate_blocks_marks_tiny_isolated_noise():
    df = pd.DataFrame(
        [
            [None, None, None, None],
            [None, "x", None, None],
            [None, None, None, None],
            [None, None, None, "y"],
        ]
    )

    blocks = build_demarcator().extract_candidate_blocks(df)

    assert len(blocks) == 2
    assert all(block["category"] == "Noise" for block in blocks)
    assert all(block["ai_suggestion"] == "Discard" for block in blocks)
    assert all("marked_tiny_isolated_noise" in block["python_detection"]["cleanup_actions"] for block in blocks)


def test_dedupes_column_fragment_inside_larger_table_bbox():
    """Marginal column strip disconnected from body but inside table bbox is merged away."""
    df = pd.DataFrame([[None] * 8 for _ in range(10)])
    for c in range(8):
        df.iloc[0, c] = "top"
        df.iloc[1, c] = "top2"
    # Body cols A–F only (0–5); leave G (6) empty on rows 3–5 so H (7) notes are disconnected.
    for r in range(2, 10):
        for c in range(6):
            df.iloc[r, c] = "d"
    df.iloc[3, 7] = "n4"
    df.iloc[4, 7] = "n5"
    df.iloc[5, 7] = "n6"

    blocks = build_demarcator().extract_candidate_blocks(df)

    assert len(blocks) == 1
    c0 = blocks[0]["coordinates"]
    assert c0["end_col"] == 7
    assert any(
        isinstance(a, str) and a.startswith("suppressed_contained_fragment")
        for a in blocks[0].get("python_detection", {}).get("cleanup_actions", [])
    )


def test_metadata_block_has_no_header_fields():
    df = pd.DataFrame(
        [
            ["Campaign", "Impressions", None, None, None],
            ["A", 100, None, None, None],
            ["B", 120, None, None, None],
            [None, None, None, None, None],
            [None, None, None, "Notes", None],
            [None, None, None, "Source: platform export", None],
        ]
    )

    blocks = build_demarcator().extract_candidate_blocks(df)
    assert len(blocks) == 2
    non_main = next(b for b in blocks if b["category"] != "Main Data")
    assert non_main.get("coordinates", {}).get("header_row") is None
    assert non_main.get("python_detection", {}).get("header_row_candidate") is None
    assert non_main.get("python_detection", {}).get("header_candidates") == []


def test_confidence_bands_follow_thresholds():
    dem = build_demarcator()
    blocks = [
        {"id": "b1", "confidence": 0.95},
        {"id": "b2", "confidence": 0.55},
        {"id": "b3", "confidence": 0.35},
    ]
    counts = dem._tag_confidence_bands(blocks)
    assert counts == {"high": 1, "ambiguous": 1, "low": 1}
    assert blocks[0]["confidence_band"] == "high" and blocks[0]["human_review_required"] is False
    assert blocks[1]["confidence_band"] == "ambiguous" and blocks[1]["human_review_required"] is False
    assert blocks[2]["confidence_band"] == "low" and blocks[2]["human_review_required"] is True
