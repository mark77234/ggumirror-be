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

## 현재 구현 범위 (Phase B-5까지)

**B-1 — Foundation**

- `create_app()` FastAPI app
- `GET /health`, `GET /`
- 환경설정 · 표준 logging
- Cloud Run용 Docker image · pytest · GitHub Actions

**B-2A — Apple Identity Token Verification**

- Apple identity token 검증 service (`app/auth/`)
- JWKS 조회 + cache + key rotation 대응
- 실패 분류(`AppleTokenReason`)

**B-2B — Server User Identity + Session**

- Firestore User + Apple identity mapping
- opaque access token session (Bearer)
- `POST /auth/apple` · `POST /auth/logout` · `GET /users/me`
- iOS client 연결 (nonce 생성 → 서버 검증 → Keychain 세션)

**B-3 — Server-Authoritative Shard Ledger**

- 불변 ledger + wallet projection (`app/shards/`)
- Firestore transaction 안에서 원장 기록과 잔액 갱신이 한 번에 일어난다
- idempotency key로 중복 지급 / 중복 차감을 **구조적으로** 막는다
- `GET /users/me/shards` — **읽기 endpoint 하나뿐이다**
- iOS `ShardWallet`은 서버 값을 보여주기만 한다

**B-4 — Daily Attendance**

- 하루 한 번 출석 → 조각 **+1** (`app/shards/attendance.py`)
- 하루의 기준은 **server 시계의 Asia/Seoul 날짜**다. client 날짜 · timezone을 받지 않는다
- `GET /users/me/attendance` · `POST /users/me/attendance` — **둘 다 Bearer 필수**
- POST는 **request body를 받지 않는다.** userId · date · amount · reason을 정할 자리가 없다
- 지급은 B-3 원장이 한다 — `credit(daily_attendance, external_event_id=<KST 날짜>)`
- 출석 전용 collection을 만들지 않았다. "오늘 받았나"는 **원장에게 묻는다**

**B-5 — AdMob Rewarded + SSV**

- 광고 1회 → 조각 **+1**, 하루 **5회**(Asia/Seoul) (`app/ads/`)
- 보상 권위는 **검증된 Google SSV callback 하나뿐**이다. client callback은 근거가 아니다
- Google이 서명한 **raw query 바이트**를 검증한다 — 재조립한 문자열이 아니다
- 하루 상한 · 중복 방지 · 원장 기록 · 잔액 갱신이 **한 Firestore transaction**
- 보상 날짜는 **서명된 `timestamp`**에서 나온다 (늦게 온 callback도 그 날의 몫)
- user binding은 short-lived opaque context — session token을 Google에 보내지 않는다
- **production ad unit이 아직 없어 지급은 fail closed 상태다** (env 두 개를 채우면 동작)

아직 구현하지 않은 것 (의도적):

shard IAP · 꾸미러 Pass ·
Store API · Listing · Asset upload · actual Publish · 구매 · 소유권 · 판매자 정산 ·
Cloud Sync · authorizationCode 교환 · Apple private key / client_secret · refresh token.

## Architecture

```
app/
├── main.py                 create_app() + verifier / store 조립
├── api/health.py           GET /health, GET /
├── api/auth.py             POST /auth/apple, POST /auth/logout
├── api/users.py            GET /users/me · shards · attendance · rewarded-ads
├── api/ads.py              GET /admob/rewarded/ssv (Google 서명이 곧 인증)
├── api/deps.py             Bearer → current user
├── ads/verifier.py         AdMob 공개키 cache + raw query ECDSA 검증
├── ads/service.py          검증 → 제품 대조 → context → 원장
├── ads/store.py            reward context protocol + in-memory
├── ads/firestore_store.py  reward context Firestore 구현
├── ads/models.py           보상 상수 · 실패 분류 · 검증된 callback
├── shards/attendance.py    KST 날짜 규칙 + 출석 지급
├── shards/models.py        ShardWallet, ShardLedgerEntry, ShardReason, idempotency
├── shards/store.py         ShardStore protocol + in-memory 구현
├── shards/firestore_store.py  Firestore transaction 구현
├── shards/service.py       ShardLedgerService (credit / debit / wallet)
├── auth/apple.py           AppleTokenVerifier, VerifiedAppleIdentity
├── auth/jwks.py            AppleJWKSProvider (조회 + cache)
├── auth/errors.py          AppleTokenReason, AppleTokenError
├── auth/models.py          User, Session, token/hash 정책
├── auth/store.py           AuthStore protocol + in-memory 구현
├── auth/firestore_store.py Firestore 구현
└── core/config.py          환경변수 + logging
tests/                      pytest (Apple · Firestore 호출 없음)
scripts/                    수동 확인용. CI에 들어가지 않는다
Dockerfile                  Cloud Run 실행용
```

abstraction은 `AuthStore` · `ShardStore` protocol 둘뿐이다 —
각각 구현은 Firestore 하나 + test fake 하나. 계층을 더 쌓지 않는다.
`ShardLedgerService`는 amount 검증과 reason 강제를 담당하는 얇은 층이고,
"service layer 규약" 때문에 만든 것이 아니다.

의도적으로 **만들지 않은 것**: service layer, repository abstraction,
dependency container, DDD layering, global exception framework, CORS.
실제 기능이 생길 때 필요한 만큼만 추가한다.

Firestore client는 **처음 auth 요청 때** 만든다. `/health`가 Firestore credential에
의존하면 멀쩡한 container가 죽었다고 판정된다.

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

| method | path | auth | 설명 |
|---|---|---|---|
| GET | `/health` | — | process health. DB · 외부 API에 의존하지 않는다 |
| GET | `/` | — | service name + status |
| POST | `/auth/apple` | — | Apple identityToken 검증 → User → session |
| POST | `/auth/logout` | Bearer | 이 session만 revoke |
| GET | `/users/me` | Bearer | internal user id |
| GET | `/users/me/shards` | Bearer | 내 조각 잔액 (읽기 전용) |
| GET | `/users/me/attendance` | Bearer | 오늘(KST) 출석을 받았는지 |
| POST | `/users/me/attendance` | Bearer | 오늘의 출석 조각 +1. **body 없음** |
| GET | `/users/me/rewarded-ads` | Bearer | 오늘 광고 보상 횟수 / 남은 횟수 |
| POST | `/users/me/rewarded-ads/context` | Bearer | 광고에 실을 opaque context. **조각을 주지 않는다** |
| GET | `/admob/rewarded/ssv` | **Google 서명** | AdMob SSV callback. 유일한 광고 지급 통로 |

이 외의 endpoint는 **없다.** `/store`, `/listings`, `/purchases`를
미리 만들지 않는다 — Client와 contract를 확정한 뒤에 만든다.

**조각을 바꾸는 endpoint는 없고, 앞으로도 generic한 것은 만들지 않는다.**
`POST /shards/credit` · `/shards/debit` · `/shards/add` · `/shards/set` 같은 것이
하나라도 생기면 client가 원하는 만큼 조각을 만들 수 있게 된다.
지급 / 차감은 **각자의 이유를 검증하는 전용 endpoint**로만 생긴다
(출석 · AdMob SSV callback · IAP 검증 · 구매 · 등록).
`tests/test_shards.py::test_no_generic_mutation_endpoint`가 이걸 고정한다.

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

## Server User Identity (B-2B)

### Firestore

collection 이름에 `ggumirror_` prefix를 붙인다. 같은 GCP project에 다른 service가
있을 수 있고, **다른 service의 collection은 읽지도 쓰지도 않는다.**

| collection | document ID | 내용 |
|---|---|---|
| `ggumirror_users` | internal UUID | `createdAt`, `updatedAt` |
| `ggumirror_auth_identities` | `sha256("apple:<subject>")` | `provider`, `userId`, `createdAt` |
| `ggumirror_sessions` | `sha256(accessToken)` | `userId`, `createdAt`, `expiresAt`, `revokedAt` |
| `ggumirror_shard_wallets` | internal user UUID | `balance`, `lifetimeEarned`, `lifetimeSpent`, `updatedAt`, `schemaVersion` |
| `ggumirror_shard_ledger` | idempotency hash 또는 자동 id | `userId`, `delta`, `balanceAfter`, `reason`, `createdAt`, `schemaVersion` |

store / listing / purchase collection은 만들지 않았다.

Cloud Run에서는 **Application Default Credentials**를 쓴다.
service account JSON key를 repo에 넣지 않는다.

### User

`id`(UUID v4) · `createdAt` · `updatedAt`뿐이다.
shard balance · seller field · store profile · 통계를 미리 넣지 않았다.

### Apple identity mapping

Apple subject와 User는 분리돼 있다. identity 문서가 `Apple subject → userId`를 가리킨다.

**raw Apple subject를 저장하지 않는다.** document ID가
`sha256("apple:<subject>")`이고, 문서 본문에는 `provider`와 `userId`만 있다.
Firestore console · export · index 이름에 subject가 남지 않는다.

hash는 deterministic하므로 "같은 subject → 같은 문서"라는 성질은 그대로다 —
중복 User 방지의 근거가 바로 이것이다.

### 중복 User 방지

`user_for_identity`는 Firestore transaction 안에서 identity 문서를 읽고,
없을 때만 User + identity를 함께 만든다.
document key가 deterministic이라 동시에 처음 로그인해도 두 요청이 같은 문서를 겨루고,
한쪽은 재시도 후 상대가 만든 User를 그대로 쓴다.

email은 identity key가 **아니다.** User 생성에 email을 요구하지 않고, 서버에 복사하지도
않는다 — client가 이미 first authorization의 이름 / 이메일을 로컬에 보존한다.

### Session

- **opaque random token.** 꾸미러 자체 JWT를 만들지 않았다 — server가 취소할 수 있어야 하고
  JWT는 그걸 어렵게 만든다
- `secrets.token_urlsafe(32)` → 43자
- Firestore에는 **`sha256(token)`만** 저장한다. raw token은 어디에도 저장하지 않는다
  (client에게 반환하는 그 순간에만 존재한다)
- 수명 **30일**, `app/auth/models.py`의 `SESSION_LIFETIME` 한 곳에서만 정한다
- 취소: 만료 · 로그아웃 · 향후 강제 revoke 모두 Firestore 문서 하나로 가능하다
- 시간은 **server 시계**로 만든다. client가 보낸 시간을 근거로 쓰지 않는다

### POST /auth/apple

```json
{ "identityToken": "<Apple JWT>", "nonce": "<client raw nonce>" }
```

nonce는 **원본**을 보낸다. 서버가 SHA-256으로 바꿔 token의 `nonce` claim과 비교한다.
그래서 token만 훔쳐도 우리 서버에 쓸 수 없다. authorizationCode는 받지 않는다.

```json
{
  "accessToken": "...",
  "tokenType": "Bearer",
  "expiresAt": "2026-09-10T11:22:33.123456Z",
  "user": { "id": "<internal uuid>" }
}
```

**Apple subject는 응답에 담기지 않는다.** 나가는 것은 internal UUID뿐이다.
key는 client의 Swift Codable 이름과 그대로 맞춘 camelCase다.

흐름: 검증(B-2A) → identity 조회 → 없으면 User 생성 → session 생성 → token 반환.

### 오류 매핑

| 상황 | status |
|---|---|
| malformed / signature / expired / audience / issuer / nonce mismatch | **401** |
| Apple JWKS에 닿지 못함 | **503** |
| `APPLE_CLIENT_ID` 미설정 · Firestore client 생성 실패 | **503** |
| Firestore 쓰기 실패 | **500** |

client에게는 검증 상세를 주지 않는다 — 어떤 항목에서 걸렸는지, kid가 무엇인지,
Firestore 경로가 무엇인지 전부 응답에 넣지 않는다.

### Bearer

`Authorization: Bearer <accessToken>` → `sha256` → session 조회 → 만료 / 취소 확인
→ internal User. header를 로그에 남기지 않는다.

`get_current_user`는 `app/api/deps.py`에 있고, 지금 쓰는 곳은 `/users/me`와
`/auth/logout`뿐이다. 가짜 protected endpoint를 만들지 않았다.

### POST /auth/logout

이 기기의 session만 revoke한다. **Apple authorization 자체는 revoke하지 않는다** —
그건 사용자가 iOS 설정에서 할 일이다. client의 거울 / 스티커 / 등록 준비와는 무관하다.
이미 없거나 만료된 token이어도 204다 — client는 어차피 로컬을 지운다.

### Local 개발

`/health`와 `/`는 credential 없이 그대로 뜬다. auth endpoint는 Firestore가 필요하다.

**주의**: 아무 설정 없이 로컬에서 auth endpoint를 부르면
Application Default Credentials의 **기본 project**로 붙는다.
로컬에서는 emulator를 쓰는 편이 안전하다:

```bash
gcloud emulators firestore start --host-port=127.0.0.1:8090
```

```bash
FIRESTORE_EMULATOR_HOST=127.0.0.1:8090 APPLE_CLIENT_ID=com.mark77234.ggumirror .venv/bin/uvicorn app.main:app --port 8080
```

`FIRESTORE_EMULATOR_HOST`가 있으면 SDK가 알아서 emulator로 간다 — 코드에 분기가 없다.

## Shard Ledger (B-3)

거울조각은 **server가 유일한 진실**이다. client는 잔액을 보여줄 뿐이고,
client가 보낸 숫자는 어떤 거래의 근거도 되지 않는다.

### 원장이 먼저, 잔액은 결과

```
ShardLedgerEntry (불변 · append-only)   ← 진실
ShardWallet (balance / lifetime 합계)   ← 빠르게 읽기 위한 projection
```

ledger 문서는 **한 번 쓰면 수정하지 않는다.** 잘못 지급했으면 반대 부호의
`refund` / `admin_adjustment` entry를 새로 쌓는다. 과거를 고쳐 쓰지 않는다 —
분쟁이 생겼을 때 "그때 무슨 일이 있었나"를 답할 수 있어야 한다.

두 문서는 **하나의 Firestore transaction** 안에서 같이 쓰인다
(`@firestore.transactional`, 모든 read를 write보다 먼저). 동시에 두 요청이 와도
잔액이 어긋나지 않는다 — transaction 충돌 시 SDK가 재시도하면서 최신 잔액을 다시 읽는다.

`balance`는 절대 음수가 되지 않는다. 부족하면 `InsufficientShards`로 거절하고
**아무것도 쓰지 않는다.** 차감을 먼저 하고 실패를 나중에 처리하지 않는다.

### Reason

모든 이동에는 이유가 붙는다 (`ShardReason`):

`daily_attendance` · `rewarded_ad` · `iap_purchase` · `mirror_purchase` ·
`mirror_sale` · `mirror_publish_fee` · `refund` · `admin_adjustment`

이유 없는 이동은 만들 수 없다. 나중에 "이 조각이 왜 늘었나"를 답하지 못하면
환불 · 정산 · 어뷰징 조사를 전부 할 수 없다.

### Idempotency

같은 사건이 두 번 도착해도 조각은 한 번만 움직인다. 네트워크 재시도 · AdMob SSV 재전송 ·
App Store notification 재전송은 **정상 동작**이지 예외가 아니다.

```
document id = sha256( len:user_id | len:reason | len:external_event_id )
```

이 hash가 **ledger 문서의 ID 자체**다. transaction 안에서 `create()`로 쓰기 때문에
이미 있으면 쓰기가 실패한다 — "먼저 조회해서 없으면 쓴다"가 아니라 **구조적으로** 막힌다.
조회-후-쓰기 사이의 틈이 없다.

**user scope는 `ShardLedgerService`가 강제한다.** 호출부가 event id에 user id를 넣어주기를
기대하지 않는다 — 잊은 곳 하나가 사용자끼리 같은 문서를 겨루게 만든다.
출석처럼 event id가 날짜뿐일 때(`daily_attendance` + `2026-08-12`) user scope가 없으면
**하루에 한 사람만** 조각을 받는다.

세 값은 **길이 접두사**로 이어 붙인다. 어떤 값에 `:`나 `|`가 섞여 있어도
다른 조합과 같은 문자열이 되지 않는다 — `("u:x", "1")`과 `("u", "x:1")`이
구분되지 않으면 서로 다른 사건이 하나로 합쳐진다.

raw user id와 raw external event id는 **문서 ID에 노출되지 않는다.** hash 결과만 쓴다.

external event id 예:

| 사건 | external event id |
|---|---|
| 출석 | 날짜(**KST**) `YYYY-MM-DD`. user는 service가 붙인다 |
| AdMob rewarded | SSV callback의 `transaction_id` |
| IAP | App Store transaction id |
| 구매 / 판매 | 주문 id |

같은 user + 같은 event = **정확히 한 번.** 다른 user + 같은 event = **서로 독립.**
AdMob `transaction_id`가 global unique여도 원장 invariant 자체는 user-scoped로 유지된다.

### "이번 호출이 실제로 적었는가" — `ShardMutationResult`

`credit` / `debit`은 `ShardMutationResult(wallet, applied)`를 돌려준다.

- `applied=True` — 이 호출이 원장에 줄을 적었다
- `applied=False` — 같은 사건이 이미 있어 아무것도 적지 않았다. **실패가 아니다**

`applied`는 저장소의 **원자적 쓰기 결과**다. `event_applied`로 미리 조회해서 짐작하면
조회와 쓰기 사이에 다른 요청이 끼어들어 둘 다 "내가 적었다"고 답한다.

출석(B-4)이 첫 사용자다. B-5 SSV 재전송 · B-6 IAP 재검증 · B-8 주문 재시도도
"이번 callback이 실제로 지급했는가"를 같은 값으로 판단한다 —
기능마다 다른 방법을 만들지 않는다.

`event_applied` / `ShardLedgerService.has_event`는 **읽기 전용 상태 조회**
(`GET /users/me/attendance`)에만 쓴다. 지급 경로에서는 쓰지 않는다.

### 검증

`ShardLedgerService`가 amount를 먼저 거른다: 정수가 아니거나, `bool`이거나,
0 이하이거나, `MAX_DELTA`(100,000)를 넘으면 거절한다. 부호는 credit / debit
**호출부가 아니라 service가** 정한다 — 호출부가 음수를 넘겨 credit으로 차감하는 길이 없다.

### 로그

`shard_wallet_read` · `shard_ledger_credit` · `shard_ledger_debit`뿐이다.
user id · external event id · idempotency key를 로그에 남기지 않는다.

## Daily Attendance (B-4)

하루 한 번 출석하면 **조각 +1**. 원장 · reason · idempotency는 B-3 것을 그대로 쓴다 —
B-4가 더한 것은 "하루"의 정의와 전용 endpoint 둘뿐이다.

| | |
|---|---|
| 보상 | **+1** (`app/shards/attendance.py`의 `DAILY_REWARD`) |
| 한도 | **Asia/Seoul calendar day 당 1회** |
| authority | **server**. client 날짜 · timezone · 기기 시각을 믿지 않는다 |
| ledger reason | `daily_attendance` |
| external event id | server KST 날짜 `YYYY-MM-DD` |

### 하루의 기준 — Asia/Seoul

```
server 시각(UTC) → Asia/Seoul → YYYY-MM-DD
```

UTC 2026-08-13 15:01 = **KST 2026-08-14 00:01** → 출석일은 `2026-08-14`다.
UTC로 계산하면 한국 사용자가 자정 직후에 어제 몫을 다시 받거나 오늘 몫을 못 받는다.

`attendance_date(now=None)`는 test가 시간을 고정할 수 있도록 `now`를 받는다.
production은 언제나 인자 없이 부른다 = server 시계. **client가 시각을 넘기는 경로는 없다.**

**정책은 Asia/Seoul calendar day**이고, 현재 구현은 **server-authoritative UTC+09:00
고정 offset**(`timezone(timedelta(hours=9))`)이다. 한국의 현행 civil time이 UTC+09:00
고정이라 두 값이 일치하고, container에 tzdata가 들어 있는지에 의존하지 않는다.

한국의 timezone rule이 바뀌면(예: DST 도입) `ZoneInfo("Asia/Seoul")`로 바꾼다 —
날짜 계산이 `attendance_date()` 한 함수에만 있으므로 그 한 줄이 전부다.
그때는 image에 tzdata가 있는지 함께 확인해야 한다.

`now`가 timezone-naive면 **거부한다.** 조용히 UTC로 가정하면 하루가 통째로 어긋난다.

### API

```
GET /users/me/attendance     → {"attendanceDate":"2026-08-16","claimed":false}
POST /users/me/attendance    → {"attendanceDate":"2026-08-16","claimed":true,"reward":1,"balance":1}
```

같은 날 두 번째 POST:

```
{"attendanceDate":"2026-08-16","claimed":false,"reward":0,"balance":1}
```

**중복 출석은 오류가 아니라 idempotent success다.** 400으로 만들면 client가
"네트워크 실패 후 재시도"와 "정말 두 번 눌렀다"를 구분해야 한다. 재시도는 정상 동작이다.
`balance`는 어느 경우에나 서버 원장이 계산한 현재 잔액이라, 응답을 잃어버린 client가
다시 불러도 잔액이 부풀지 않고 오히려 제자리를 찾는다.

#### `claimed`의 정확한 뜻

| | POST | GET |
|---|---|---|
| `claimed: true` | **이 요청이 원장에 줄을 적고 reward를 지급했다** | 오늘 이미 받았다 |
| `claimed: false` | 같은 사건이 이미 적용돼 **이 요청은 지급하지 않았다** | 아직 받지 않았다 |

POST의 `claimed`는 **`ShardMutationResult.applied` 그대로**다.
같은 사용자·같은 날짜로 10개가 동시에 들어오면 HTTP는 10개 모두 200이고,
`claimed=true`는 **정확히 하나**, 나머지 9개는 `claimed=false, reward=0`이다.
`balance`는 10개 전부 최종 서버 잔액을 말한다.

지급 여부를 `has_event`로 **미리 조회해서 정하지 않는다.** 조회와 쓰기 사이에
다른 요청이 끼어들면 둘 다 "내가 지급했다"고 답한다.

POST는 **request body를 받지 않는다.** `userId` · `date` · `amount` · `reason`을 보내도
받는 자리가 없어 아무 영향이 없다. 누구인지는 Bearer session, 며칠인지는 server 시계,
얼마인지는 서버 상수에서 온다. 둘 다 인증 없이 부르면 401이다.

### 왜 출석 collection이 없나

"오늘 받았나"는 **원장에게 묻는다** (`ShardLedgerService.has_event`).
ledger 문서 ID가 `sha256(user | daily_attendance | KST 날짜)`이므로 조회 하나로 끝난다.

같은 경제 사실의 authority를 두 곳에 두지 않는다. 출석 상태를 따로 저장하면
언젠가 원장과 다른 답을 하고, 그때 어느 쪽이 진실인지 아무도 모른다.

### 동시성 — 경제도 응답도 정확하다

같은 사용자가 같은 날짜로 10개를 동시에 보내면:

| | |
|---|---|
| HTTP 200 | 10 |
| `claimed=true` / `reward=1` | **정확히 1** |
| `claimed=false` / `reward=0` | **정확히 9** |
| ledger entry | 1 |
| balance · lifetimeEarned | +1 |

"이번 요청이 적었는가"는 **원장 쓰기 transaction의 결과**에서 나온다
(`ShardStore.apply` → `ShardMutationResult.applied`).
`transaction.create(<idempotency 문서>)`가 성공한 쪽만 `applied=True`이고,
중복 분기로 들어간 쪽은 `False`다.

Firestore가 commit 시점에 `AlreadyExists`를 돌려주는 경우(우리가 읽은 뒤 상대가 먼저
commit) 역시 **실패가 아니다.** 원장에 이미 그 줄이 있다는 뜻이므로 transaction을
한 번 다시 돌려 중복 분기로 들어간다 — 500이 아니라 `claimed=false`가 정답이다.

## AdMob Rewarded + SSV (B-5)

광고 1회 정상 시청 → **조각 +1**, 하루 최대 **5회**(Asia/Seoul).

| | |
|---|---|
| 보상 | **+1** (`app/ads/models.py`의 `REWARD_PER_AD`) |
| 하루 한도 | **5** (`DAILY_REWARD_LIMIT`) |
| 하루 기준 | Asia/Seoul, **서명된 SSV `timestamp`**에서 유도 |
| authority | **검증된 Google SSV callback 하나뿐** |
| client callback | **경제 authority가 아니다** |
| idempotency | Google `transaction_id` |
| ledger reason | `rewarded_ad` |

### 왜 client callback을 믿지 않나

`onUserEarnedReward`는 검증되지 않은 입력이다. 그것으로 지급하면 앱을 고쳐
"광고 다 봤다"를 무한히 보내는 것으로 조각을 찍어낼 수 있다.
client가 광고를 다 본 뒤 하는 일은 **상태 새로고침 하나**다.

### 흐름

```
로그인 client → POST /users/me/rewarded-ads/context → opaque context (조각 안 움직임)
                                    ↓ customData
                              Google 광고 시청
                                    ↓ 서명된 callback
Google → GET /admob/rewarded/ssv?...&signature=..&key_id=..
            1. ECDSA 서명 검증        ← 이걸 통과해야 어떤 값도 믿는다
            2. ad_unit / reward_item / reward_amount 대조
            3. context → 내부 user UUID
            4. 원장 transaction (하루 5회 + 중복 방지 + 잔액)
```

### 서명 검증 — raw query bytes를 그대로

Google은 **자기가 보낸 query string의 바이트열**에 서명한다.
그래서 검증 입력은 **ASGI `scope["query_string"]`(bytes) 그대로**다.

`Request.url.query`도 쓰지 않는다 — URL 객체를 거치면 parsing과 재직렬화를 한 번
지나므로 검증 대상이 "받은 바이트"가 아니게 될 여지가 생긴다.
`verify()`는 문자열을 받으면 `TypeError`로 거절한다(그 경로가 굳지 않게).

```
...&reward_item=..&timestamp=..&signature=<sig>&key_id=<id>
^------------ 서명 대상 ------------^
```

규약상 마지막 두 parameter는 항상 `signature`, `key_id`다.
decode · 정렬 · 재인코딩을 하지 않고, **검증에 성공한 뒤에야** 값을 해석한다.
특히 `timestamp`는 보상 날짜를 정하는 값이라 검증 전에 읽지 않는다.

공개키는 `https://www.gstatic.com/admob/reward/verifier-keys.json`에서 받아
process memory에 1시간 cache한다(Google rotation 고려, 하루보다 길게 믿지 않는다).
모르는 `key_id`면 **한 번 갱신**하고 다시 찾는다 — `AppleJWKSProvider`와 같은 모양이다.
**key 조회 실패는 서명 실패와 구분한다**(전자만 5xx로 재시도를 받는다).

ECDSA(SHA-256) 검증은 이미 있는 `cryptography`로 한다. 새 dependency를 넣지 않았다.

### user binding — session token을 Google에 보내지 않는다

callback URL은 로그 · 중계 · 재시도 기록에 남는다. 그래서 거기에
**session token · Apple identity token · 내부 user UUID를 넣지 않는다.**

대신 로그인한 사용자가 광고 직전에 **short-lived opaque context**를 발급받고 그것만 보낸다.
session과 같은 규칙이다 — `secrets.token_urlsafe`, Firestore에는 `sha256`만,
document ID도 hash, 수명 6시간.

signed stateless token 대신 저장형을 고른 이유: 서명 token은 **새 server secret**이
필요한데 이 서비스에는 secret이 하나도 없다(Secret Manager도 만들지 않았다).
보상 하나 때문에 secret 관리 · rotation · 유출 대응을 새로 들이는 것보다,
이미 있는 opaque-token 패턴 재사용이 작고 안전하다. 게다가 저장형은 **취소할 수 있다.**

context는 callback이 와도 **소비하지 않는다** — Google 재전송 때도 같은 사용자로 풀려야 한다.
중복 지급은 `transaction_id` idempotency가 막는다.

### 제품 대조 — 서명만 맞다고 주지 않는다

`ad_unit` · `reward_item` · `reward_amount`가 설정과 다르면 지급하지 않는다.
지급액은 **`REWARD_PER_AD` 상수**다 — callback의 숫자는 지급액의 출처가 아니라 검증 대상이다.

`ADMOB_SSV_EXPECTED_AD_UNIT` / `ADMOB_REWARD_ITEM`이 비어 있으면 서명이 맞아도
**지급하지 않는다(fail closed).** 실제 ad unit이 생기기 전에 추측한 ID를 넣지 않는다.

#### ad unit 이름이 두 개인 이유

| 어디 | 이름 | 무엇 |
|---|---|---|
| client (xcconfig) | `ADMOB_REWARDED_AD_UNIT_ID` | 광고를 **load**할 때 쓰는 ID |
| backend (env) | `ADMOB_SSV_EXPECTED_AD_UNIT` | callback의 `ad_unit`과 **비교**할 값 |

**둘이 같은 문자열이라고 가정하지 않는다.** 확인된 것은 "서명된 callback에 `ad_unit`이
들어온다"는 사실뿐이고, 그 값의 정확한 표현은 실제 callback을 받아봐야 안다.
같다고 단정하고 하드코딩하면 다를 경우 **모든 보상이 조용히 거절**된다(200 + 미지급).

확정 방법: ad unit 생성 → SSV callback URL 등록 → SSV Test Tool 1회 →
`admob_ssv_no_reward reason=unexpected_ad_unit` 로그와 함께 실제 값을 확인 →
그 값을 `ADMOB_SSV_EXPECTED_AD_UNIT`에 넣는다.

### 하루 5회 — 확인과 증가가 지급과 같은 transaction

```
GET count → if count < 5 → credit()      ← 절대 이렇게 하지 않는다
```

동시에 도착한 callback들이 전부 "아직 4개다"를 보고 상한을 넘겨 지급한다.
quota가 4일 때 서로 다른 정상 callback 10개가 동시에 오면 **정확히 하나만** 지급되고
최종 5가 되어야 한다(`test_concurrent_callbacks_cannot_exceed_the_daily_cap`).

그래서 `ShardStore.apply`가 `PeriodQuota`를 함께 받는다 —
**상한 확인 · counter 증가 · 원장 기록 · 잔액 갱신이 한 Firestore transaction**이다.
중복(idempotency)으로 판정되면 counter를 올리지 않는다 — 재전송이 남은 횟수를 깎으면 안 된다.

counter는 `ggumirror_shard_quotas`에 있고 문서 ID는 hash다(raw user id · 날짜 노출 없음).
**이것은 잔액의 authority가 아니다** — 경제의 진실은 여전히 원장이고,
이 문서는 상한을 원자적으로 세기 위한 operational projection이다.

### 보상 날짜는 서명된 timestamp

```
광고 완료 23:59:58 KST → callback 도착 00:00:02 KST → 전날 quota
```

서버가 받은 시각으로 정하면 자정 근처에 quota가 두 번 열리거나 하루가 통째로 밀린다.
**서명 검증을 통과한** `timestamp`만 쓴다.

### 응답 코드 = Google에 대한 지시

| 상황 | 응답 |
|---|---|
| 지급함 | **200** |
| 중복 transaction · 하루 상한 도달 | **200** |
| context 없음 · 우리 것 아님 · 만료 | **200** |
| ad unit / reward 불일치 · 미설정(fail closed) | **200** |
| 서명 실패 · signature/key_id 누락 · 형식 오류 | **400** |
| Google key 조회 실패 · Firestore 오류 | **5xx** |

원칙: **서명이 유효한데 우리가 "안 준다"고 판단을 끝낸 상태는 전부 200**이다.
재시도해도 결과가 같은데 4xx를 주면 Google이 전달 실패로 보고 한동안 다시 보낸다.
왜 안 줬는지는 응답이 아니라 **로그**로 구분한다.

**AdMob SSV Test Tool은 사용자 context 없이 호출한다** — 서명이 유효하므로 200이고
조각은 하나도 움직이지 않는다. 그래서 ad unit 설정 전에도 Test Tool이 통과한다.

서명이 틀린 callback에 5xx를 주면 영원히 재시도를 받는다.
반대로 일시적 실패에 200을 주면 정당한 보상이 조용히 사라진다.

#### context 실패는 셋으로 나눠 기록한다

| 로그 | 뜻 |
|---|---|
| `admob_ssv_missing_context` | `custom_data`가 아예 없다. **SSV Test Tool이 정상적으로 이 상태** |
| `admob_ssv_unknown_context` | 값은 있는데 우리가 발급한 적이 없다 |
| `admob_ssv_expired_context` | 우리가 준 것이지만 시간이 지났다 |

실제 광고 트래픽에서 `missing_context`가 보이기 시작하면 **client가 context를 안 싣고
있다는 신호**다. 하나로 뭉뚱그리면 그걸 알아챌 수 없다.

### 로그

`admob_ssv_reward_applied` · `admob_ssv_duplicate` · `admob_ssv_daily_limit_reached` ·
`admob_ssv_rejected reason=...` · `admob_ssv_unknown_key_id`.
transaction id는 **12자 hash**로만 남긴다.

**access log는 우리가 부르는 것이 아니라서 filter로 막는다.** uvicorn access logger는
요청 줄을 query까지 통째로 남기고, 거기에 Google signature와 reward context가 들어 있다.
`RedactSensitiveQuery`가 `/admob/rewarded/ssv?<redacted>`로 바꾼다(경로는 남긴다).

### 아직 없는 것

**production AdMob app / rewarded ad unit이 아직 없다.** endpoint는 살아 있지만
`ADMOB_SSV_EXPECTED_AD_UNIT`이 비어 있는 동안에는 아무에게도 조각을 주지 않는다.
console 설정 후 env 두 개를 채우면 그때부터 동작한다.

### AdMob Rewarded 준비 상태 (B-3 시점 기록)

**보상 권위는 Google SSV callback 하나뿐이다.**
client의 `onUserEarnedReward`는 UI를 갱신할 뿐, 그것만으로 조각을 지급하지 않는다.
client가 "광고 다 봤다"고 말하는 것은 검증되지 않은 입력이다.

B-5에서 만들 것:

1. AdMob SSV callback endpoint (`GET`, Google가 부른다)
2. Google public key로 **서명 검증** — 검증 실패하면 지급하지 않는다
3. `transaction_id`를 external event id로 써서 재전송에도 한 번만 지급
4. 하루 5회 상한 — 상한 확인도 같은 transaction 안에서
5. 지급 reason은 `rewarded_ad` 고정

지금 원장이 이미 갖춘 것: 이유 · idempotency · transaction · 상한 검증 자리.
B-5는 "검증된 callback → `credit(rewarded_ad, transaction_id)`" 한 줄을 붙이는 일이다.

### Cloud Run request log는 앱 로그와 별개다 (운영 설정)

`app/core/config.py`의 `RedactSensitiveQuery`는 **우리 process가 찍는 로그만** 가린다.
Cloud Run은 그것과 별개로 **platform request log**(`run.googleapis.com/requests`)를 남기고,
거기에는 `httpRequest.requestUrl`이 **query까지 통째로** 들어간다.
SSV callback query에는 Google signature와 우리가 발급한 reward context가 있으므로
application logging filter만으로는 보호되지 않는다 — canary 요청으로 실제 확인했다.

그래서 `ggumirror-prod`의 `_Default` sink에 **endpoint 하나만** 제외하는 exclusion을 뒀다.

| | |
|---|---|
| name | `exclude-ggumirror-admob-ssv-request-log` |
| 대상 | `ggumirror-api`의 `run.googleapis.com/requests` 중 `/admob/rewarded/ssv` |
| 유지 | `/health` · `/auth/*` · `/users/me/*` 등 **다른 request log는 그대로 남는다** |

```
resource.type="cloud_run_revision"
AND resource.labels.service_name="ggumirror-api"
AND log_id("run.googleapis.com/requests")
AND httpRequest.requestUrl=~"^https://[^/]+/admob/rewarded/ssv(\?|$)"
```

**앱 stdout log는 그대로 남는다** — `GET /admob/rewarded/ssv?<redacted>`와
`admob_ssv_*` 이벤트는 계속 보이므로 운영에 필요한 정보를 잃지 않는다.
없어지는 것은 query가 들어간 platform request log 항목 하나뿐이다.

exclusion은 **즉시 적용되지 않는다** — 반영까지 몇 분 걸린다.
바꾼 직후 canary로 확인하면 아직 저장되는 것처럼 보인다.

새 endpoint가 credential을 query로 받게 되면 `SENSITIVE_QUERY_PATHS`와
이 exclusion **양쪽**에 추가해야 한다. 한쪽만 하면 반쪽만 가려진다.

## Production

**꾸미러 전용 GCP project를 쓴다.** 다른 서비스와 project를 공유하지 않는다.

| | |
|---|---|
| GCP project | **`ggumirror-prod`** (project number `764151610434`) |
| Cloud Run service | `ggumirror-api` |
| region | `asia-northeast3` (서울) |
| URL | https://ggumirror-api-cmyv4amroa-du.a.run.app |
| Firestore | **`(default)` @ asia-northeast3** |
| Artifact Registry | `ggumirror` @ asia-northeast3 |
| runtime SA | `ggumirror-api-runtime@ggumirror-prod.iam.gserviceaccount.com` — `roles/datastore.user`만 |
| scaling | min 0 / max 3, CPU 1, 512Mi, concurrency 80, timeout 30s |

Firestore collection은 `ggumirror_users` · `ggumirror_auth_identities` ·
`ggumirror_sessions` · `ggumirror_shard_wallets` · `ggumirror_shard_ledger` 다섯뿐이다. project 단위 격리 + collection namespace 격리를 둘 다 갖는다.

### opicmobile-45cd5 는 우리 것이 아니다

`opicmobile-45cd5`는 **DailyOPIc production project고 꾸미러 작업 범위 밖(OUT OF SCOPE)이다.**

그 project에서 꾸미러 때문에 resource 생성 · 삭제 · IAM · Firestore · Cloud Run ·
Artifact Registry · Service Account · API · billing을 **건드리지 않는다.**
특히 `dailyopic-api` · Firestore `(default)`(nam5) · Artifact Registry `dailyopic` ·
`dailyopic-cloudrun` SA는 절대 수정하지 않는다.

초기 bootstrap 때 그 project에 꾸미러 resource를 만든 적이 있지만
(`ggumirror-api` · named DB `ggumirror-prod` · AR `ggumirror` · runtime SA)
**I-3에서 전부 삭제했다.** `opicmobile-45cd5`에 남아 있는 꾸미러 resource는 **없다.**
지금 production은 위 표뿐이다.

### 배포 (수동)

```bash
gcloud auth configure-docker asia-northeast3-docker.pkg.dev
```

Cloud Run은 linux/amd64다. Apple Silicon에서는 platform을 명시한다:

```bash
SHA=$(git rev-parse --short HEAD) && docker build --platform linux/amd64 -t asia-northeast3-docker.pkg.dev/ggumirror-prod/ggumirror/ggumirror-api:$SHA .
```

```bash
docker push asia-northeast3-docker.pkg.dev/ggumirror-prod/ggumirror/ggumirror-api:$(git rev-parse --short HEAD)
```

```bash
gcloud run deploy ggumirror-api --project=ggumirror-prod --region=asia-northeast3 --image=asia-northeast3-docker.pkg.dev/ggumirror-prod/ggumirror/ggumirror-api:$(git rev-parse --short HEAD) --service-account=ggumirror-api-runtime@ggumirror-prod.iam.gserviceaccount.com --allow-unauthenticated --min-instances=0 --max-instances=3 --cpu=1 --memory=512Mi --concurrency=80 --timeout=30s --set-env-vars="APP_ENV=production,LOG_LEVEL=INFO,APPLE_CLIENT_ID=com.mark77234.ggumirror,GCP_PROJECT_ID=ggumirror-prod,FIRESTORE_DATABASE=(default)"
```

`latest` tag를 쓰지 않는다 — 어떤 commit이 떠 있는지 revision에서 바로 보여야 한다.

Cloud Run endpoint는 인터넷에서 도달 가능하다(`--allow-unauthenticated`).
iOS 앱이 직접 부르기 때문이다. **인증은 application Bearer session이 한다** —
Cloud Run IAM 인증을 사용자에게 요구하지 않는다.

Secret Manager는 만들지 않았다. 지금 필요한 secret이 하나도 없다(ADC + Apple private key 불필요).

### 자동 배포 (아직 안 함)

첫 배포는 수동으로 했다. GitHub Actions 배포를 붙일 때 필요한 것:

1. Workload Identity Pool + provider (repo 한정 조건) — **새 project에**
2. 배포용 service account — `roles/run.developer` · `roles/artifactregistry.writer` ·
   `roles/iam.serviceAccountUser`
3. `.github/workflows/deploy.yml` — `google-github-actions/auth`(WIF) → build/push → deploy

**service account JSON key를 쓰지 않는다.**

### Firestore location

`(default)`를 처음부터 `asia-northeast3`에 만들었다. 서울 Cloud Run과 같은 region이라
로그인 요청이 태평양을 건너지 않는다. (bootstrap 때 겪은 `nam5` 문제는 project를
분리하면서 사라졌다 — 그 DB는 DailyOPIc project 것이고 우리가 쓰지 않는다.)

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
- session raw token을 **저장하지 않는다.** Firestore에는 `sha256(token)`만 둔다
- raw Apple subject를 저장 · 응답 · 로그에 쓰지 않는다. 저장은 hash key로만 한다

## 비즈니스 모델 로드맵

B-3 원장 위에 얹는다. **전부 server가 지급 / 차감한다.**

| Phase | 내용 | reason | idempotency key |
|---|---|---|---|
| B-4 ✅ | 출석 — 하루 1개 | `daily_attendance` | user + reason + **KST 날짜** |
| B-5 ✅ | AdMob rewarded — 1개, **하루 5회** | `rewarded_ad` | SSV `transaction_id` |
| A-1A ✅ | AI 스티커 — **−6개**, 실패하면 환불 | `ai_sticker` · `refund` | 서버가 만든 `generation_id` |
| B-6 🔧 | 조각 IAP — 10 / 50 / 100 (서버 검증 완료, client는 B-6C) | `iap_purchase` | App Store `transactionId` |
| B-7 | 꾸미러 Pass — ₩4,900 월 / ₩39,000 년 | (구독 혜택 정책 확정 후) | 구독 transaction id |
| B-8 | 마켓 — 등록 20 조각, 조각으로 산 거울은 영구 소유 | `mirror_publish_fee` · `mirror_purchase` · `mirror_sale` | 주문 id |

### 조각 IAP 검증 (B-6)

Apple 공식 **`app-store-server-library==3.1.2`**의 `SignedDataVerifier`로 StoreKit
transaction JWS를 검증한다. **x5c 인증서 체인 검증기를 직접 만들지 않는다.**

- **Apple root certificate는 `app/iap/certs/`에 DER로 커밋한다** — 공개 값이라 secret이 아니다.
  runtime에 외부 URL을 부르지 않는다. 출처 <https://www.apple.com/certificateauthority/>,
  SHA-256 지문은 `CLAUDE.md`에 기록. Apple이 공개한 root **셋 다** 넣는다(G3 하나만 가정하지 않는다)
- **`enable_online_checks=True`** — 인증서 폐기(OCSP) 확인을 켠다. 조회 실패는 **503**이고
  검증을 건너뛰지 않는다. client가 `finish()` 전이라 그 결제는 다시 온다
- **Production verifier에는 numeric `IAP_APP_APPLE_ID`가 필수다.** 없으면 Production만 꺼진다
- **`.p8` / issuerId / keyId는 필요 없다** — App Store Server **API**를 부르지 않는다.
  환불 조회처럼 실제 API 호출이 생길 때만 도입한다
- **unverified payload의 `environment`로 verifier를 고르지 않는다.** 허용된 verifier들을
  고정 순서로 시도하고 정확히 하나만 성공해야 한다
- **`Xcode` verifier를 만들지 않는다.** library는 그 environment에서 **서명 검증을 건너뛴다**

B-5는 **Google SSV callback만** 보상 근거로 쓴다. B-6 / B-7은 Apple 영수증 검증
결과만 쓴다 — client의 "구매 성공했다"를 그대로 믿지 않는다.

## AI 스티커 (A-1A · A-1B)

프롬프트 한 줄 → 투명 PNG. **조각을 쓰는 첫 기능**이고,
생성은 **서버가 소유하는 durable 작업**이다(A-1B).

| endpoint | 하는 일 |
|---|---|
| `GET /ai/stickers/config` | `available` · `price` · `resultRetentionDays`. + 묶인 조각 정리(sweep) |
| `POST /ai/stickers` | `{requestId, prompt}` → `{generationId, status, createdAt, balance}` |
| `GET /ai/stickers/{id}` | 상태 조회. 남의 것은 **404** |
| `GET /ai/stickers/{id}/image` | 결과 PNG. Bearer + 소유자 검증 |

**응답에 이미지가 없다.** A-1A는 base64로 실어 보냈고 그래서 응답을 잃으면 끝이었다.
지금은 결과가 먼저 durable하게 저장되고, 같은 `requestId`로 다시 물으면 되찾을 수 있다.

### 멱등성

`generationId = sha256(len:userId|len:requestId)`이고 그것이 곧 Firestore 문서 ID다.
같은 `(user, requestId)`는 **provider 1회 · 차감 1회**다. 재시도할 때는 `prompt`를 비운다 —
응답을 잃은 client는 원문을 다시 보낼 수 없고 서버도 저장하지 않는다.

### 복구

    upload → status=succeeded → 응답     (이 순서를 바꾸지 않는다)

⚠️ **Cloud Run timeout은 container를 죽이지 않는다** — client 연결을 끊고 504를 줄 뿐이다.
예전 worker가 살아서 늦게 성공할 수 있으므로 시간에 경제를 걸지 않는다.

안전은 **terminal 권위 + lease CAS**가 만든다: `refunded → succeeded`도
`succeeded → refunded`도 불가능하다. CAS에서 진 worker는 자기 object를 치운다(orphan).

`lease` 만료만으로는 환불하지 않는다. `RECOVERY_GRACE`(15분, 정상 worker 수명보다 훨씬 김)를
넘기면 **recovery eligible**이 되고, 실제 정리는 그 작업을 건드리는 다음 요청
(재시도 · 상태 조회 · 앱 시작 sweep)에서 결과 유무를 보고 일어난다.
**시간이 지났다고 저절로 풀리지 않는다** — 아무도 오지 않으면 `pending`으로 남는다.
`GET /image`는 `status == succeeded`일 때만 내보낸다 — object 존재는 증거일 뿐이다.

### 환경변수

| | |
|---|---|
| `AI_IMAGE_API_KEY` | provider API key. **비어 있으면 기능이 꺼진다**(fail closed) |
| `AI_IMAGE_MODEL` | **`gpt-image-2`** (production 기본값) |
| `AI_IMAGE_QUALITY` | 기본 `low` |

### 왜 `gpt-image-2` + 기기 배경제거인가 (A-1B.2)

capability probe로 확인한 것:

- `gpt-image-1-mini`는 `background=transparent`를 **지원한다.**
  하지만 **deprecated라 production 기본 model로 채택하지 않았다**
- **`gpt-image-2`는 지원하지 않는다** —
  `400 / param=background / "Transparent background is not supported for this model."`
  이것이 현재 production model이다

그래서 allowlist를 억지로 넓히지 않고 **output contract를 바꿨다**: provider는
`valid PNG`만 주면 되고 **서버는 alpha를 요구하지 않는다.** 요청에 `background`를 보내지 않는다.

투명 배경은 **기기가 만든다.** 꾸미러에는 이미 사진 배경제거(`PhotoStickerMaker`,
Vision on-device)가 있으므로 배경제거 API를 따로 붙이지 않는다.
model 이름은 여전히 `SUPPORTED_MODELS`로 고정하고, 모르는 값이면
`observed_model=`만 남기고 fail closed다.

**앱을 다시 내지 않고 이 값만 채우면 기능이 열린다** — client는 `config`를 보고 CTA를 켠다.

### 켜기 전에 해야 하는 것 (아직 하지 않았다)

아래는 전부 **production mutation**이라 코드와 함께 자동으로 일어나지 않는다.
지금 `AI_IMAGE_API_KEY` · `AI_RESULT_BUCKET`이 비어 있어 기능은 fail closed다.

1. **OpenAI 꾸미러 전용 Project.** DailyOPIc key를 재사용하지 않는다.
   Project(예: `ggumirror-production`)를 새로 만들고 그 project 안에서 key를 발급한다 —
   project를 나눠야 spend budget · rate limit · key 폐기를 따로 할 수 있다.
   image generation에 필요한 최소 권한만 준다.

2. **Secret Manager** (API가 아직 꺼져 있다):
   ```
   gcloud services enable secretmanager.googleapis.com --project=ggumirror-prod
   gcloud secrets create ggumirror-openai-api-key --project=ggumirror-prod --replication-policy=automatic
   # 값은 stdin으로만 넣는다. 명령줄 인자로 주면 shell history에 남는다.
   gcloud secrets versions add ggumirror-openai-api-key --project=ggumirror-prod --data-file=-
   gcloud secrets add-iam-policy-binding ggumirror-openai-api-key --project=ggumirror-prod \
     --member=serviceAccount:ggumirror-api-runtime@ggumirror-prod.iam.gserviceaccount.com \
     --role=roles/secretmanager.secretAccessor
   ```

   **secret 하나에만** `secretAccessor`를 붙인다 — project-level
   `roles/secretmanager.secretAccessor`를 주지 않는다. 그러면 앞으로 생길 모든 secret을
   이 SA가 읽게 된다.

3. **결과 bucket** (꾸미러 전용 · private · uniform access · lifecycle 7일):
   ```
   gcloud storage buckets create gs://ggumirror-ai-results --project=ggumirror-prod \
     --location=asia-northeast3 --uniform-bucket-level-access --public-access-prevention
   gcloud storage buckets update gs://ggumirror-ai-results --project=ggumirror-prod \
     --lifecycle-file=scripts/ai-results-lifecycle.json
   gcloud storage buckets add-iam-policy-binding gs://ggumirror-ai-results --project=ggumirror-prod \
     --member=serviceAccount:ggumirror-api-runtime@ggumirror-prod.iam.gserviceaccount.com \
     --role=roles/storage.objectAdmin
   ```

   **권한은 bucket 하나에만 붙인다. project-wide storage role을 주지 않는다.**
   runtime SA는 지금 `roles/datastore.user`뿐이고, 여기에 더하는 것은
   **이 bucket의 object 권한**뿐이다. 필요한 동작은 세 가지다:

   | 동작 | 왜 필요한가 |
   |---|---|
   | create | 생성 결과 upload |
   | read | `GET /image` 스트리밍 · 복구 시 존재 확인 |
   | **delete** | CAS에서 진 worker의 orphan object 정리 |

   delete까지 필요해서 `objectAdmin`을 쓴다. delete를 빼려면 `objectCreator` +
   `objectViewer`로 나눌 수 있지만, 그러면 orphan이 lifecycle(7일)까지 남는다 —
   사용자에게 나가지는 않지만 저장 비용과 감사 잡음이 된다.
   bucket 밖(다른 bucket · project 전체)에는 어떤 권한도 주지 않는다.

4. **`--timeout`.** 이미지 생성은 수십 초가 걸린다. 현재 `--timeout=30s`로는
   정상 생성도 잘리고, 그러면 사용자는 조각을 쓰고 아무것도 받지 못한다.
   **`--timeout=180s`** 로 올린다.

   > ⚠️ **timeout은 "container를 죽이는 deadline"이 아니라 client 응답 deadline이다.**
   > 초과해도 worker는 계속 돈다. 그래서 순서가 중요하다:
   >
   >     provider(90s) < Cloud Run(180s) < client(200s)
   >
   > provider 쪽이 **먼저** 끊겨야 application이 실패를 직접 보고 환불까지 마친다.
   > 반대면 client만 끊기고 정리는 아무도 하지 않는 구간이 길어진다.

   AI 때문에 전체 서비스 timeout이 올라간다. 다른 endpoint는 이미 수백 ms 안에 끝나므로
   **느려지지 않는다** — 늘어나는 것은 "죽은 요청을 붙잡고 있는 최대 시간"뿐이고,
   `--max-instances=3` · `--concurrency=80`은 그대로다.

5. 배포:
   ```
   gcloud run deploy ggumirror-api --project=ggumirror-prod --region=asia-northeast3 \
     --image=... --timeout=180s \
     --set-env-vars="...,AI_IMAGE_MODEL=gpt-image-2,AI_RESULT_BUCKET=ggumirror-ai-results" \
     --set-secrets="AI_IMAGE_API_KEY=ggumirror-openai-api-key:latest"
   ```
   `--set-env-vars`는 기존 값을 **덮어쓴다** — `ADMOB_*`를 포함해 전부 다시 적는다.

### 조각

- **차감이 provider 호출보다 먼저다.** 요금 나가는 호출을 잔액 없이 하지 않는다
- 잔액이 곧 상한이다 — 하루 N회 counter를 따로 두지 않았다
- provider가 실패하면 `refund`로 되돌린다(append-only, 재시도해도 한 번)
- 가격은 서버 상수 하나이고 `config`로 내려간다. **client가 가격을 알고 있지 않다**

### 저장하지 않는 것

프롬프트 원문 · 생성된 이미지. 응답으로 한 번 흘려보내고 끝이다 —
Cloud Storage bucket이 필요 없는 이유이고, 스티커의 주인은 기기다.
로그에는 `prompt_length`만 남는다.

## 다음 Phase

**B-6 — 조각 IAP.** 원장 · idempotency · 전용 endpoint · 원자적 상한까지 갖췄다.
B-6이 더할 것은 App Store 영수증 검증과 transaction id 기반 지급이다.
client의 "구매 성공했다"를 그대로 믿지 않는다.

그 전에 **B-5 production 설정이 남아 있다** — AdMob app / rewarded ad unit 생성,
SSV callback URL 등록, `ADMOB_SSV_EXPECTED_AD_UNIT` · `ADMOB_REWARD_ITEM` 배포.
