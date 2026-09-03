from sia.agent.state import is_stalled


def test_high_confidence_plateau_is_not_a_stall():
    assert is_stalled({"confidence_trajectory": [0.99, 0.99, 0.99]}) is False
    assert is_stalled({"confidence_trajectory": [0.9, 0.91, 0.92]}) is False


def test_low_confidence_plateau_is_a_stall():
    assert is_stalled({"confidence_trajectory": [0.4, 0.41, 0.42]}) is True


def test_two_repeated_issues_do_not_stall_when_confidence_is_high():
    assert is_stalled({
        "confidence_trajectory": [0.99],
        "issues_history": [
            {"issue_type": "missing_column", "resolved": False},
            {"issue_type": "missing_column", "resolved": False},
        ],
    }) is False


def test_three_repeated_issues_stall_when_confidence_is_low():
    assert is_stalled({
        "confidence_trajectory": [0.4],
        "issues_history": [
            {"issue_type": "missing_column", "resolved": False},
            {"issue_type": "missing_column", "resolved": False},
            {"issue_type": "missing_column", "resolved": False},
        ],
    }) is True
