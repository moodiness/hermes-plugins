from pathlib import Path
import re


def test_skill_frontmatter_and_required_sections() -> None:
    path=Path(__file__).parents[1]/"skills"/"omp-service"/"SKILL.md"
    text=path.read_text()
    assert text.startswith("---\n")
    description=re.search(r"^description: (.+)$",text,re.M).group(1)
    assert len(description)<=60 and description.endswith(".")
    for section in ("When to Use","Prerequisites","How to Run","Quick Reference","Procedure","Pitfalls","Verification"):
        assert f"## {section}" in text
    assert "/Users/" not in text
    assert "platforms: [macos]" in text
    assert "terminal(command=" in text
