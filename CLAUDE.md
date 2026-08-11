# ggumirror Backend

Backend repository for 꾸미러.

## Planned Stack

- Python
- FastAPI
- Firestore
- Google Cloud Run
- Docker
- GitHub Actions

## Responsibilities

- Apple identity verification
- server user identity
- shard ledger
- Store listing
- purchase / ownership
- seller shard settlement

## Current Implementation (Phase B-2B 완료)

FastAPI + Apple token 검증 + Firestore User / Session + Bearer auth.

```
app/main.py          create_app()
app/api/health.py    GET /health, GET /
app/auth/apple.py    AppleTokenVerifier, VerifiedAppleIdentity
app/auth/jwks.py     AppleJWKSProvider (조회 + cache)
app/auth/errors.py   AppleTokenReason, AppleTokenError
app/auth/models.py   User, Session, token/hash 정책
app/auth/store.py    AuthStore protocol + in-memory
app/auth/firestore_store.py  Firestore 구현
app/api/auth.py      POST /auth/apple, POST /auth/logout
app/api/users.py     GET /users/me
app/api/deps.py      Bearer → current user
app/core/config.py   환경변수 + logging
tests/               pytest (Apple · Firestore 호출 없음)
```

API는 health / auth / users뿐이다. shard · store · listing · purchase는 없다.

Python 3.13 고정 (local · Docker · CI 동일).

dependency source of truth: `requirements.txt`(runtime) / `requirements-dev.txt`(개발).
`pyproject.toml`은 pytest 설정만 담는다 — 이 service는 배포되는 package가 아니라 container다.

## Session Hash Policy

session은 **opaque random token**이다. 꾸미러 자체 JWT를 만들지 않는다 —
server가 취소할 수 있어야 한다.

- `secrets.token_urlsafe(32)`
- Firestore에는 **`sha256(token)`만** 저장한다. raw token은 client에게 반환하는
  그 순간에만 존재한다. 저장·로그 모두 금지
- document ID도 token hash다
- 수명은 `app/auth/models.py`의 `SESSION_LIFETIME` 한 곳에서만 정한다.
  숫자를 코드 여러 곳에 흩뿌리지 않는다
- 생성 시각 / 만료는 **server 시계**로 만든다. client가 보낸 시간을 근거로 쓰지 않는다

## Internal User vs Provider Identity

| | |
|---|---|
| 꾸미러 user ID | internal **UUID v4**. API에 나가는 유일한 사용자 식별자 |
| Apple subject | provider identity. Firestore mapping 안에서만 쓴다 |

- Apple subject를 **응답 · 로그 · analytics label에 쓰지 않는다**
- Apple subject를 document ID에 raw로 쓰지 않는다 —
  `sha256("apple:<subject>")`를 key로 쓴다(deterministic이라 중복 User 방지는 그대로)
- **email은 identity key가 아니다.** 식별은 항상 검증된 `sub`로 한다
- collection은 `ggumirror_` prefix. 같은 GCP project의 다른 service collection을
  읽지도 쓰지도 않는다

## Structure Rules

기능이 생기기 전에 layer를 만들지 않는다. 다음을 미리 추가하지 않는다:

- service layer
- repository abstraction을 더 쌓기 (`AuthStore` protocol 하나가 전부다 —
  구현은 Firestore + test fake 둘뿐)
- dependency injection container
- DDD layering
- global exception handler framework
- CORS (client가 iOS native다. web client가 생기면 그때)

Firestore client는 처음 auth 요청 때 만든다. `/health`가 credential에 의존하면
멀쩡한 container가 죽었다고 판정된다.

새 endpoint는 Client와 contract를 확정한 뒤에만 만든다.

## Boundaries

iOS UI, Mirror Camera, Editor, local artwork rendering은 Backend 책임이 아니다.

Apple identityToken은 server에서 검증한다(B-2A 완료). client가 "검증됐다"고 하는 말을 믿지 않는다.

## Apple Credential Logging (금지)

로그에 절대 넣지 않는다:

- raw identityToken · JWT 전체 · signature segment
- raw Apple subject
- email
- authorizationCode
- client nonce
- **session access token** (raw도, hash도 남길 이유가 없다)
- `Authorization` header
- secret / 향후 auth credential

남기는 것은 결과와 분류뿐이다:
`apple_token_verified` · `apple_token_rejected reason=invalid_audience` ·
`apple_jwks_unknown_kid` · `apple_jwks_refreshed keys=6`.

`AppleTokenError`의 message에도 claim 값을 담지 않는다 — 그대로 로그로 새어 나간다.
`tests/test_apple_token.py::test_logs_never_contain_credentials`가 이걸 고정한다.

## VerifiedAppleIdentity Boundary

검증 성공 결과로 raw JWT dict를 앱 안으로 흘리지 않는다.
`VerifiedAppleIdentity`(subject / email? / email_verified? / is_private_email?)가 경계다.

이 경계 밖에서 token을 다시 파싱하지 않는다. claim이 더 필요해지면
이 model에 field를 추가하고, 무엇에 쓰는지 함께 적는다.

## Apple Subject vs 꾸미러 User ID

Apple `sub`는 **Apple identity provider의 opaque subject**일 뿐 꾸미러 user ID가 아니다.

- 식별은 항상 검증된 `sub`로 한다. **email을 식별자로 쓰지 않는다**
  (첫 로그인에만 오고, private relay면 바뀔 수 있다)
- raw Apple `sub`를 public API user identifier로 노출하지 않는다
- B-2B에서 `Apple subject → internal ggumirror user UUID` mapping을 만들고,
  API에는 internal UUID만 나간다

Shard Ledger가 도입된 뒤에는 server ledger가 authoritative source다.

client가 보낸 shard balance를 신뢰해서 거래를 처리하지 않는다.

아직 정의되지 않은 API contract를 임의로 확정하지 않는다.

기능 구현 전 Client와 Server contract를 명확히 정의한다.

client가 보낸 user ID를 authorization 근거로 단독 신뢰하지 않는다.
누구의 요청인지는 검증된 token에서만 얻는다.

`Authorization` header와 secret을 로그에 남기지 않는다.
로그에는 값이 아니라 결과만 남긴다.

## Production

| | |
|---|---|
| GCP project | **`ggumirror-prod`** (꾸미러 전용) |
| Cloud Run | `ggumirror-api` @ asia-northeast3 |
| URL | https://ggumirror-api-cmyv4amroa-du.a.run.app |
| Firestore | `(default)` @ asia-northeast3 |
| Artifact Registry | `ggumirror` @ asia-northeast3 |
| runtime SA | `ggumirror-api-runtime` — `roles/datastore.user`만 |

image tag에 git SHA를 넣는다. `latest`에 의존하지 않는다.

## Infrastructure Isolation (영구 규칙)

DailyOPIc(`opicmobile-45cd5`)은 **실제 사용자가 있는 LIVE production**이고
꾸미러 작업에서 **완전히 OUT OF SCOPE**다.

명령 대상에 `opicmobile-45cd5` · `dailyopic-api` · `dailyopic-cloudrun` 또는
DailyOPIc resource가 등장하면 **mutation 전에 멈추고 보고한다.**
사용자의 명시적 승인 없이 진행하지 않는다. READ-ONLY audit만 허용.

공유 가능: Google 계정 · **GCP Billing Account** · Apple Developer 계정 · GitHub 계정.
공유 금지: project · Cloud Run · Firestore · Storage bucket · Artifact Registry ·
service account · IAM · Secret · Workload Identity · CI/CD identity · Pub/Sub ·
Cloud Tasks · Redis · Cloud SQL · Firebase project · RevenueCat project · StoreKit product ·
AdMob app.

새 infra를 붙일 때 먼저 묻는다: **"DailyOPIc에서 쓰는 resource인가?"**
YES면 재사용하지 않고 `ggumirror-prod` 안에 꾸미러 전용으로 만든다.

### 향후 정책

| 필요해지면 | 이렇게 한다 |
|---|---|
| Cloud Storage | `ggumirror-prod`에 꾸미러 식별 가능한 이름으로 bucket 신규 생성. runtime SA에 그 bucket 최소 권한만 추가 |
| CI/CD | `ggumirror-prod` 전용 Workload Identity + 배포 SA. long-lived JSON key를 만들지 않는다 |
| Secret Manager | 실제 secret이 생길 때 `ggumirror-prod`에만 |
| Firebase | 실제 요구(FCM · App Check 등)가 생길 때만, `ggumirror-prod` 기반 꾸미러 전용 설정으로 |
| RevenueCat | 꾸미러 전용 project / app / product. **구매 성공을 잔액 권위로 쓰지 않는다** — server ledger가 권위다 |

## Next Phase

**C-1 — Lock Screen Quick Mirror** (client 작업).

그 다음 **B-3 Shard Ledger** — server authoritative 조각 원장.
client가 보낸 잔액으로 거래를 처리하지 않는다.

Cloud Run 자동 배포 workflow는 GCP project · service account ·
Workload Identity가 확정된 뒤에 만든다.
