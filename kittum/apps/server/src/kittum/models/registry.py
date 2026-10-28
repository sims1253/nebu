"""The model recipe registry, loaded from `recipes.yaml` shipped with the package."""

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
            if not recipe.file_hashes:
                raise RecipeError(f"recipe {recipe.id} pins no weight files")
            self._recipes[recipe.id] = recipe

    @classmethod
    def from_yaml(cls, text: str) -> ModelRegistry:
        data: Any = yaml.safe_load(text)
        if not isinstance(data, dict) or not isinstance(data.get("recipes"), list):
            raise RecipeError("a recipe file needs a top-level `recipes` list")
        recipes = []
        for i, raw in enumerate(data["recipes"]):
            try:
                recipes.append(ModelRecipe.model_validate(raw))
            except ValidationError as e:
                name = raw.get("id", f"#{i}") if isinstance(raw, dict) else f"#{i}"
                raise RecipeError(f"recipe {name}: {e}") from e
        return cls(recipes)

    @classmethod
    def load(cls, path: Path | None = None) -> ModelRegistry:
        """Load `path`, or the recipes shipped with Kittum."""
        if path is not None:
            return cls.from_yaml(path.read_text())
        return cls.from_yaml(resources.files("kittum.models").joinpath("recipes.yaml").read_text())

    def get(self, recipe_id: str) -> ModelRecipe:
        try:
            return self._recipes[recipe_id]
        except KeyError:
            raise NotFoundError(f"no model recipe {recipe_id!r}") from None

    def __iter__(self) -> Iterator[ModelRecipe]:
        return iter(self._recipes.values())

    def __len__(self) -> int:
        return len(self._recipes)
