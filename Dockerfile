FROM --platform=linux/amd64 python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_PROGRESS_BAR=off \
    OPENBLAS_NUM_THREADS=1 \
    OMP_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    NUMEXPR_NUM_THREADS=1 \
    FILE2DOC_STORAGE_ROOT=/data/file2doc \
    FILE2DOC_LOCAL_ASR_MODEL_DIR=/data/file2doc/models/sherpa-paraformer-zh \
    FILE2DOC_AUTH_ENABLED=true \
    FILE2DOC_PORT=8000

WORKDIR /app

RUN useradd --create-home --shell /usr/sbin/nologin file2doc \
    && mkdir -p /data/file2doc \
    && chown -R file2doc:file2doc /data/file2doc

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir .

USER file2doc
EXPOSE 8000

CMD ["sh", "-c", "exec uvicorn file2doc.main:app --host 0.0.0.0 --port \"$FILE2DOC_PORT\" --loop asyncio --http h11"]
