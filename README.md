# ggumirror-be

꾸미러 iOS app의 backend service.

이 repo는 iOS client(`ggumirror`)와 **완전히 독립된 Git repository**다.
두 repo의 변경을 하나의 commit으로 묶지 않는다.

## 서비스 목적

꾸미러의 Core 기능(거울 · 촬영 · 꾸미기 · 내 거울 · 상점 구경 · local 저장)은
**로그인 없이** 동작한다. Backend는 그 위에 얹히는 것만 맡는다.

- Apple identity 검증 · server user identity
- 거울조각(shard) ledger — **server authoritative**
- 실제 Store listing · 구매 / 소유권
- 판매자 조각 정산

## 현재 구현 범위 (Phase B-1 — Backend Foundation)

FastAPI 뼈대만 있다. **기능은 아직 하나도 없다.**

구현한 것:

- `create_app()` FastAPI app
- `GET /health`, `GET /`
- 환경설정(`APP_ENV` / `LOG_LEVEL` / `PORT`)
- 표준 logging
- Cloud Run에서 실행 가능한 Docker image
- pytest + GitHub Actions

아직 구현하지 않은 것 (의도적):

Apple identityToken 검증 · Backend User · Shard Ledger ·
Mirror listing · Sticker listing · 구매 · 소유권 · 판매자 정산 · Firestore.

## Architecture

```
app/
├── main.py          create_app()
├── api/health.py    GET /health, GET /
└── core/config.py   환경변수 + logging
tests/               pytest
Dockerfile           Cloud Run 실행용
```

의도적으로 **만들지 않은 것**: service layer, repository abstraction,
dependency container, DDD layering, global exception framework, CORS.
실제 기능이 생길 때 필요한 만큼만 추가한다.

Firestore도 아직 붙이지 않았다. 붙일 때는 `app/core/firestore.py` 하나에
client를 만들고 필요한 곳에서 FastAPI dependency로 주입한다 —
repository interface를 먼저 만들지 않는다.

## Python version

**3.13** 하나로 고정한다. local · Docker · CI 모두 같은 version을 쓴다.

## Local setup

```bash
cd ggumirror-be
python3.13 -m venv .venv
```

```bash
.venv/bin/pip install -r requirements-dev.txt
```

`requirements.txt`는 runtime(image에 들어가는 것), `requirements-dev.txt`는
개발/테스트용이다. 이 두 파일이 dependency의 source of truth고,
`pyproject.toml`은 pytest 설정만 담는다 (이 service는 배포되는 package가 아니다).

환경변수는 필요할 때만:

```bash
cp .env.example .env
```

`.env`는 commit하지 않는다. `.env.example`만 commit한다.

## Tests

```bash
.venv/bin/pytest
```

## Local server

```bash
.venv/bin/uvicorn app.main:app --reload --port 8080
```

```bash
curl -s localhost:8080/health
```

→ `{"status":"ok"}`

local에서는 `/docs`가 열린다. `APP_ENV=production`이면 닫힌다.

## Docker

```bash
docker build -t ggumirror-be .
```

```bash
docker run --rm -p 8080:8080 ggumirror-be
```

```bash
curl -s localhost:8080/health
```

Cloud Run이 주는 `PORT`를 그대로 읽으므로 port를 바꿔서도 돌아간다:

```bash
docker run --rm -e PORT=9000 -p 9000:9000 ggumirror-be
```

## API

| method | path | 설명 |
|---|---|---|
| GET | `/health` | process health. DB · 외부 API에 의존하지 않는다 |
| GET | `/` | service name + status |

이 외의 endpoint는 **없다.** `/auth/apple`, `/users`, `/shards`, `/store`,
`/listings`, `/purchases`를 미리 만들지 않는다 —
Client와 contract를 확정한 뒤에 만든다.

## Cloud Run readiness

이번 Phase에서 **실제 배포는 하지 않았다.** 다음은 준비돼 있다.

- container가 `0.0.0.0:$PORT`에서 listen
- non-root user(uid 10001)로 실행
- `/health`가 외부 의존성 없이 200
- graceful shutdown (uvicorn이 PID 1 신호를 받는다)

배포 workflow(`deploy.yml`)는 **아직 만들지 않았다** —
GCP project · service account · Workload Identity가 확정되지 않았다.

## Security

기능을 구현하기 전에 정해 둔 원칙이다. 지금부터 지킨다.

- client가 보낸 **shard balance를 신뢰하지 않는다.** 잔액은 server ledger가 유일한 진실이다
- Apple credential은 **server에서 검증**한다. client가 "검증됐다"고 말하는 것을 믿지 않는다
- client가 보낸 user ID를 **authorization 근거로 단독 신뢰하지 않는다.**
  누구 요청인지는 검증된 token에서만 얻는다
- 다음을 **로그에 남기지 않는다**: Apple identityToken · authorizationCode ·
  raw Apple user identifier · email · 향후 auth credential · secret · `Authorization` header.
  로그가 필요하면 값이 아니라 결과만 남긴다 (`token verify failed reason=expired`)
- secret 기본값을 코드에 넣지 않는다. `.env`는 commit하지 않는다
- production 응답에 stack trace를 담지 않는다 (`debug=False`)

## 다음 Phase

**B-2 — Apple Server Authentication.**

Apple identityToken 검증(Apple public key · signature · `aud` · `iss` · `exp` ·
nonce) → server user identity 생성/조회. 그 다음이 Shard Ledger다.
