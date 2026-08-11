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

## 현재 구현 범위 (Phase B-2A까지)

**B-1 — Foundation**

- `create_app()` FastAPI app
- `GET /health`, `GET /`
- 환경설정 · 표준 logging
- Cloud Run용 Docker image · pytest · GitHub Actions

**B-2A — Apple Identity Token Verification**

- Apple identity token 검증 service (`app/auth/`)
- JWKS 조회 + cache + key rotation 대응
- 실패 분류(`AppleTokenReason`)

아직 구현하지 않은 것 (의도적):

Firestore User 생성 · 꾸미러 access token / session 발급 · `POST /auth/apple` endpoint ·
iOS 연결 · Shard Ledger · Store API · Listing · 구매 · 소유권 · 판매자 정산 ·
authorizationCode 교환 · Apple private key / client_secret.

## Architecture

```
app/
├── main.py          create_app()
├── api/health.py    GET /health, GET /
├── auth/apple.py    AppleTokenVerifier, VerifiedAppleIdentity
├── auth/jwks.py     AppleJWKSProvider (조회 + cache)
├── auth/errors.py   AppleTokenReason, AppleTokenError
└── core/config.py   환경변수 + logging
tests/               pytest (Apple 호출 없음)
scripts/             수동 확인용. CI에 들어가지 않는다
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

## Apple identity token verification (B-2A)

### 흐름

```
identityToken (JWT)
  → header.alg 확인 (RS256만 허용. none / HS256 즉시 거부)
  → header.kid → AppleJWKSProvider.key_for(kid)
  → PyJWT signature 검증 + iss / aud / exp / iat / sub 검증
  → (expected_nonce를 준 경우) nonce claim 비교
  → VerifiedAppleIdentity
```

이 Phase에는 **endpoint가 없다.** `POST /auth/apple`도, `/auth/apple/verify-test` 같은
debug endpoint도 만들지 않았다 — 그런 endpoint는 결국 production에 남아
검증을 우회하는 문이 된다. verification service만 있다.

### 검증 항목

| 항목 | 정책 |
|---|---|
| JWT 구조 | 깨진 token은 `malformed_token` |
| `alg` | **RS256만.** token이 주장하는 alg를 그대로 쓰지 않는다. `none` 절대 불허 |
| `kid` | header의 kid와 **일치하는** key만 사용. 첫 key를 쓰지 않는다 |
| signature | Apple public key로 검증 (PyJWT + cryptography) |
| `iss` | `https://appleid.apple.com` 고정 |
| `aud` | `APPLE_CLIENT_ID`와 일치 |
| `exp` | 만료 거부. clock skew 여유 30초 |
| `iat` | 필수. 숫자가 아니면 거부 |
| `sub` | 필수. 비어 있으면 거부 |
| `nonce` | `expected_nonce`를 준 경우에만 검증(상수 시간 비교) |

### Expected audience

**native iOS Sign in with Apple의 `aud`는 app의 Bundle ID다.** Services ID가 아니다
(Services ID는 web / Android처럼 Apple에 redirect로 붙는 flow용이다).

현재 client를 조사한 결과:

| 항목 | 값 |
|---|---|
| Bundle ID | `com.mark77234.ggumirror` |
| Team ID | `GQ89YG5G9R` |
| entitlement | `com.apple.developer.applesignin = [Default]` |
| 로그인 UI | `SignInWithAppleButton(.signIn)` (native) |

→ `APPLE_CLIENT_ID=com.mark77234.ggumirror`

값을 코드에 두지 않고 settings에서 받는다. `APP_ENV=production`인데 비어 있으면
**기동에서 실패한다** — audience 없이 검증하면 다른 앱의 token도 통과하기 때문이다.
`AppleTokenVerifier`도 빈 client_id를 거부한다.

### JWKS / cache

- endpoint: `https://appleid.apple.com/auth/keys`
- Apple은 **여러 key를 동시에 게시한다** (확인 시점 6개, 전부 RS256).
  그래서 kid로 고른다 — 첫 key를 쓰면 rotation 중에 틀린다
- process-memory cache, TTL 10분. 외부 cache(Redis 등)를 쓰지 않는다
- **rotation 대응**: cache에 kid가 없으면 JWKS를 한 번 갱신하고 다시 찾는다.
  그래도 없으면 `unknown_kid`로 거부
- 임의 kid를 넣은 요청이 Apple 호출로 증폭되지 않도록 갱신 간격 하한 60초
- HTTP timeout **3초 명시**. Apple이 응답하지 않을 때 Cloud Run worker가 매달리지 않는다
- Apple 장애 중에는 이미 받아둔 key로 계속 검증한다. cache도 없으면 `jwks_unavailable`
- retry framework를 넣지 않았다. rotation 확인 1회가 사실상의 retry다

HTTP는 stdlib `urllib`다. 요청이 이거 하나뿐이라 httpx를 runtime dependency로 올리지 않았다.

### VerifiedAppleIdentity

검증 성공 결과로 raw JWT dict를 앱 전체에 넘기지 않는다. 경계는 이 값이다.

- `subject` — Apple의 opaque subject
- `email` / `is_email_verified` / `is_private_email` — 있을 때만

**`email`을 사용자 식별자로 쓰지 않는다.** Apple은 첫 로그인에만 주고, private relay면
바뀔 수 있다. 식별은 언제나 `subject`로 한다.

그리고 **`subject`는 꾸미러 user ID가 아니다.** Apple identity provider의 subject일 뿐이다.
raw Apple subject를 public API user identifier로 노출하지 않는다.
B-2B에서 `Apple subject → internal ggumirror user UUID` mapping을 만든다.

### 실패 분류

내부: `malformed_token` · `unsupported_algorithm` · `unknown_kid` · `jwks_unavailable` ·
`invalid_signature` · `invalid_issuer` · `invalid_audience` · `expired_token` ·
`invalid_issued_at` · `nonce_mismatch` · `missing_claim`.

외부 응답에는 이 상세를 내보내지 않는다. B-2B의 endpoint는
`jwks_unavailable`만 503으로, 나머지는 전부 **401 + 일반 메시지**로 바꾼다.
"audience는 맞았고 signature만 틀렸다"를 공격자에게 알려줄 이유가 없다.

### Client nonce 조사 결과

**Client nonce flow not yet implemented.**

client 전체(`*.swift`)에서 `nonce` 검색 결과 **0건**이다.
`AccountSection.swift`는 `request.requestedScopes = [.fullName, .email]`만 설정하고
`request.nonce`를 넣지 않는다.

그래서 server는 **존재하지 않는 nonce를 검증했다고 하지 않는다.**
`verify(token, expected_nonce=...)`를 준 경우에만 nonce claim을 검증하고,
주지 않으면 검증하지 않는다. 인터페이스와 test는 준비돼 있다.

B-2B TODO (Client 변경 포함 — 이번 Phase에서는 Client를 건드리지 않았다):

1. Client가 random nonce 생성 → `request.nonce`에 설정
2. Client가 그 nonce를 server 요청에 함께 보낸다 (또는 server가 발급한 nonce를 쓴다)
3. Apple이 token의 `nonce` claim에 그 값을 담아 돌려준다
4. Server가 `expected_nonce`로 검증

Apple 권장대로 raw nonce를 SHA256으로 해싱해 보낼지는 B-2B에서 client와 함께 확정한다.
현재 verifier는 **token claim과 비교할 문자열을 그대로 받는다** — 해싱 위치를 서버에
못 박아두지 않았다.

### authorizationCode

client는 `authorizationCode`도 받는다(현재 아무 데도 보내지 않는다).
이 Phase에서 **구현하지 않았다**: Apple token validation endpoint 교환 · refresh token ·
`client_secret` 생성 · Apple private key.

→ **B-2B 이후 결정.** refresh token은 "사용자가 Apple 계정을 지웠는지" 확인이나
Apple 요구사항인 계정 삭제 시 token revoke가 필요할 때 의미가 있다.
필요하지 않은 Apple private key를 지금 Secret Manager에 만들지 않는다.

### 이 layer의 test

`pytest`는 **인터넷 없이 전부 통과한다.** Apple을 호출하는 unit test는 없다.

test용 RSA key pair를 실행 중에 생성해 Apple과 같은 모양의 JWKS / JWT를 만든다.
**실제 Apple token이나 credential을 fixture로 commit하지 않는다.**

conftest의 autouse fixture가 `urllib.request.urlopen`을 막는다 —
실수로 실제 endpoint를 부르는 test가 들어오면 통과하지 않고 실패한다.

실제 endpoint를 손으로 확인할 때만:

```bash
PYTHONPATH=. .venv/bin/python scripts/apple_jwks_smoke.py
```

CI에는 들어가지 않는다.

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
- 다음을 **로그에 남기지 않는다**: Apple identityToken · JWT 전체 · raw subject ·
  email · authorizationCode · `Authorization` header · secret.
  남기는 것은 결과와 분류뿐이다 — `apple_token_verified`,
  `apple_token_rejected reason=invalid_audience`, `apple_jwks_unknown_kid`.
  test로 고정했다 (`test_logs_never_contain_credentials`)
- secret 기본값을 코드에 넣지 않는다. `.env`는 commit하지 않는다
- production 응답에 stack trace를 담지 않는다 (`debug=False`)

## 다음 Phase

**B-2B — Server User Identity.**

1. `Apple subject → internal ggumirror user UUID` mapping (Firestore)
2. `POST /auth/apple` — identityToken 검증 → user 생성/조회 → 꾸미러 session 발급
3. 실패 매핑: `jwks_unavailable` → 503, 나머지 → 401 + 일반 메시지
4. nonce flow 연결 (Client 변경 필요)
5. iOS 연결

그 다음이 Shard Ledger다.
