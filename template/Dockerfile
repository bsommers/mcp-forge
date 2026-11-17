# Offline-capable: run scripts/vendor_wheels.sh first, then `docker build --network=none .`
# Build and runtime MUST share the same Python minor (distroless python3-debian12 = 3.11).
FROM python:3.11-slim AS build
WORKDIR /app
COPY wheels ./wheels
RUN pip install --no-cache-dir --no-index --find-links wheels --target /deps \
    "mcp>=1.2,<2" "pydantic>=2" "httpx>=0.27"
COPY src ./src

FROM gcr.io/distroless/python3-debian12:nonroot
WORKDIR /app
COPY --from=build /deps /deps
COPY --from=build /app/src ./src
ENV PYTHONPATH=/deps PYTHONUNBUFFERED=1
USER nonroot
ENTRYPOINT ["python3", "src/index.py"]
