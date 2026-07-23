FROM python:3.11.15-slim-bookworm@sha256:721dc13fd1be0a771e54b72097634291d628d0007dee9da777e2ce676a9c998f

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MPLCONFIGDIR=/tmp/matplotlib \
    PORT=8080

WORKDIR /app

COPY services/image_processor/requirements.lock ./
RUN python -m pip install --no-cache-dir --upgrade pip==26.1.2 \
    && python -m pip install --no-cache-dir -r requirements.lock \
    && python -m pip install --no-cache-dir --no-deps page-dewarp==0.3.4

COPY pyproject.toml ./librarytool/pyproject.toml
COPY src/librarytool ./librarytool/src/librarytool
RUN python -m pip install --no-cache-dir --no-deps --no-build-isolation ./librarytool

COPY services/image_processor/pyproject.toml services/image_processor/README.md ./
COPY services/image_processor/whl_image_processor ./whl_image_processor
RUN python -m pip install --no-cache-dir --no-deps --no-build-isolation . \
    && python -c "from librarytool.processing import raster; from whl_image_processor import pipeline" \
    && adduser --system --uid 10001 --group processor

USER 10001:10001
EXPOSE 8080

CMD ["python", "-m", "whl_image_processor.api"]
