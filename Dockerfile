# Cloud Run에서 그대로 돌아가는 image.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

# dependency를 먼저 넣는다 — app 코드가 바뀌어도 이 layer는 재사용된다.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# non-root. Cloud Run은 어차피 격리하지만, 컨테이너가 새어도 root는 아니게 한다.
RUN useradd --create-home --uid 10001 appuser
USER appuser

# Cloud Run이 PORT를 준다. 로컬 docker run에서는 8080.
ENV PORT=8080
EXPOSE 8080

# exec form으로 감싸 uvicorn이 PID 1 신호를 받게 한다 (graceful shutdown).
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
