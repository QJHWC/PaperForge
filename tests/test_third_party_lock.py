from pathlib import Path

from paperforge.third_party import verify_third_party_lock


def test_vendored_publication_skills_match_source_lock() -> None:
    root = Path(__file__).resolve().parents[1]
    result = verify_third_party_lock(root)
    assert result.valid
    assert result.checked_files > 0
    assert "latex-paper-skills" in result.sources
    assert "PaperFit" in result.sources
