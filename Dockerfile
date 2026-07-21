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
    FILE2DOC_LOCAL_ASR_MODEL_DIR=/data/file2doc/models/funasr \
    FILE2DOC_LOCAL_ASR_ENGINE=funasr-local \
    FILE2DOC_AUTH_ENABLED=true

WORKDIR /app

RUN useradd --create-home --shell /usr/sbin/nologin file2doc \
    && mkdir -p /data/file2doc \
    && chown -R file2doc:file2doc /data/file2doc

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir \
    -i https://mirrors.aliyun.com/pypi/simple \
    --trusted-host mirrors.aliyun.com \
    . \
    && pip install --no-cache-dir \
    --index-url https://download.pytorch.org/whl/cpu \
    torch==2.6.0+cpu \
    torchaudio==2.6.0+cpu

USER file2doc
EXPOSE 8000

CMD ["uvicorn", "file2doc.main:app", "--host", "0.0.0.0", "--port", "8000", "--loop", "asyncio", "--http", "h11"]
