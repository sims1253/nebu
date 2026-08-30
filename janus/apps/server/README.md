# Janus server

The FastAPI server compiles comparison inputs, reads PDF evidence, extracts specification-defined fields, compares them with reference values, and persists versioned review artifacts.

## Run

```
uv run uvicorn janus.main:app --reload --port 3001
```

The default reader uses the PDF text layer; it does no OCR.

Runtime configuration lives in environment variables. Copy `.env.example` to `.env`; `.env` is gitignored.

## Tests and checks

```
uv sync --all-extras
uv run pytest
uv run ruff check src tests
uv run ty check src
```
