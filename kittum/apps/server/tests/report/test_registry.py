import pytest

from kittum.core import Id, new_id
from kittum.report import Artifact, register_target, render, targets


def test_a_profile_target_renders_through_report_and_cannot_be_registered_twice() -> None:
    target = f"test_{new_id().hex}.summary"
    scopes: list[str | None] = []

    def summary(project_id: Id, scope_id: str | None) -> Artifact:
        scopes.append(scope_id)
        return Artifact(target=target, project_id=project_id, cutoff_seq=7, files=())

    register_target(target, summary)
    project_id = new_id()

    assert render(target, project_id, "subgroup-a").project_id == project_id
    assert scopes == ["subgroup-a"]
    assert targets()[:4] == ("prisma", "ris", "bundle", "ai_disclosure")
    assert target in targets()
    with pytest.raises(ValueError, match="already registered"):
        register_target(target, summary)
    with pytest.raises(ValueError, match="<profile id>"):
        register_target("modul4", summary)
    with pytest.raises(ValueError, match="unknown report target"):
        render("nobody.registered_this", project_id)
