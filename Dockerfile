# uv's own image: it ships the interpreter and uv together, so the container and
# the laptop resolve from the same uv.lock and run the same Python (3.13).
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PORT=8080

WORKDIR /srv

# Dependencies first, so editing application code doesn't invalidate the install
# layer. --frozen makes the build fail loudly if uv.lock is out of date rather
# than quietly resolving something different from what was tested.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
COPY data ./data
COPY static ./static
COPY fixtures ./fixtures
# The console's /api/replay pushes the REAL recorded transcript through the live
# cascade, and that transcript is the eval corpus. Without this the demo's main
# affordance 404s in production while working perfectly on a laptop — the class
# of bug that only appears once the image is the thing being run.
COPY evals/data ./evals/data

ENV PATH="/srv/.venv/bin:$PATH"

EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
