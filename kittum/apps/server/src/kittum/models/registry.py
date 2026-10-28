"""The model recipe registry: `recipes.yaml` shipped with the package, plus a deployment's own
recipes file (`KITTUM_RECIPES_FILE`) in the same format."""

from importlib import resources
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import ValidationError

from kittum.core import KittumError, NotFoundError
from kittum.models.types import ALLOWED_WEIGHTS_LICENCES, ModelRecipe

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator
    from pathlib import Path


class RecipeError(KittumError):
    """A recipe is malformed, duplicated, or under a weights licence Kittum does not allow."""


class ModelRegistry:
    def __init__(self, recipes: Iterable[ModelRecipe]) -> None:
        self._recipes: dict[str, ModelRecipe] = {}
        for recipe in recipes:
            licence = recipe.weights_licence.lower()
            if licence not in ALLOWED_WEIGHTS_LICENCES:
                raise RecipeError(
                    f"recipe {recipe.id}: weights licence {recipe.weights_licence!r} is not "
                    f"allowed (allowed: {', '.join(sorted(ALLOWED_WEIGHTS_LICENCES))})"
                )
            if recipe.id in self._recipes:
                raise RecipeError(f"recipe {recipe.id} is defined twice")
            if not recipe.file_hashes and recipe.serving != "hosted":
                raise RecipeError(f"recipe {recipe.id} pins no weight files")
            self._recipes[recipe.id] = recipe

    @classmethod
    def from_yaml(cls, text: str) -> ModelRegistry:
        return cls(_parse(text))

    @classmethod
    def load(cls, path: Path | None = None, *, local: Path | None = None) -> ModelRegistry:
        """Load `path`, or the recipes shipped with Kittum, and add the recipes in `local`.

        A local recipe may not reuse a shipped recipe's ID: a run that names a recipe must mean
        the same model wherever it is read.
        """
        if path is not None:
            shipped = _parse(path.read_text())
        else:
            shipped = _parse(resources.files("kittum.models").joinpath("recipes.yaml").read_text())
        if local is None:
            return cls(shipped)
        try:
            text = local.read_text()
        except OSError as e:
            raise RecipeError(f"cannot read the recipes file {local}: {e}") from e
        own = _parse(text, source=str(local))
        taken = {r.id for r in shipped}
        for recipe in own:
            if recipe.id in taken:
                raise RecipeError(
                    f"{local}: recipe {recipe.id} has the ID of a built-in recipe; give it another"
                )
        return cls([*shipped, *own])

    def get(self, recipe_id: str) -> ModelRecipe:
        try:
            return self._recipes[recipe_id]
        except KeyError:
            raise NotFoundError(f"no model recipe {recipe_id!r}") from None

    def __iter__(self) -> Iterator[ModelRecipe]:
        return iter(self._recipes.values())

    def __len__(self) -> int:
        return len(self._recipes)


def _parse(text: str, *, source: str = "") -> list[ModelRecipe]:
    where = f"{source}: " if source else ""
    data: Any = yaml.safe_load(text)
    if not isinstance(data, dict) or not isinstance(data.get("recipes"), list):
        raise RecipeError(f"{where}a recipe file needs a top-level `recipes` list")
    recipes = []
    for i, raw in enumerate(data["recipes"]):
        try:
            recipes.append(ModelRecipe.model_validate(raw))
        except ValidationError as e:
            name = raw.get("id", f"#{i}") if isinstance(raw, dict) else f"#{i}"
            raise RecipeError(f"{where}recipe {name}: {e}") from e
    return recipes
