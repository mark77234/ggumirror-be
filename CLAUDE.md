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

## Current Implementation (Phase B-5 완료)

FastAPI + Apple token 검증 + Firestore User / Session + Bearer auth +
server-authoritative 조각 원장 + 하루 한 번 출석 + AdMob rewarded SSV.

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
app/api/users.py     GET /users/me · shards · attendance · rewarded-ads
app/api/ads.py       GET /admob/rewarded/ssv (Google 서명이 곧 인증)
app/ads/verifier.py  AdMob 공개키 cache + raw query ECDSA 검증
app/ads/service.py   검증 → 제품 대조 → context → 원장
app/ads/store.py     reward context (opaque token, hash 저장)
app/api/deps.py      Bearer → current user
app/shards/models.py ShardWallet, ShardLedgerEntry, ShardReason, idempotency_hash
app/shards/store.py  ShardStore protocol + in-memory
app/shards/firestore_store.py  Firestore transaction 구현
app/shards/service.py  ShardLedgerService
app/shards/attendance.py  KST 날짜 규칙 + 출석 지급
app/core/config.py   환경변수 + logging
tests/               pytest (Apple · Firestore 호출 없음)
```

API는 health / auth / users / admob뿐이다. store · listing · purchase는 없다.

조각을 움직이는 통로는 **둘뿐**이고 둘 다 client가 값을 정할 수 없다:

| 통로 | 무엇이 인증하나 |
|---|---|
| `POST /users/me/attendance` | Bearer session (body 없음) |
| `GET /admob/rewarded/ssv` | **Google ECDSA 서명** (Bearer 없음) |

`POST /users/me/rewarded-ads/context`는 광고에 실을 opaque context를 발급할 뿐
**조각을 움직이지 않는다.**

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

## Shard Ledger Policy (영구 규칙)

거울조각의 진실은 **server ledger 하나**다. client가 보낸 잔액은 어떤 거래의 근거도 아니다.

- `ShardLedgerEntry`는 **불변 append-only**다. 이미 쓴 entry를 고치거나 지우지 않는다.
  잘못됐으면 반대 부호의 `refund` / `admin_adjustment` entry를 새로 쌓는다
- ledger 기록과 wallet 갱신은 **하나의 Firestore transaction**에서만 일어난다.
  따로 쓰면 중간에 죽었을 때 잔액과 원장이 갈라진다
- 모든 이동에 `ShardReason`이 붙는다. 이유 없는 이동은 만들 수 없다
- **idempotency**: ledger 문서 ID가
  `sha256(len:user_id | len:reason | len:external_event_id)`다.
  transaction 안에서 `create()`로 쓰므로 중복이 구조적으로 막힌다 —
  "조회해서 없으면 쓴다"로 바꾸지 않는다 (그 사이에 틈이 생긴다)
- **user scope는 service가 강제한다.** `user_id`를 열쇠에서 빼지 않고,
  호출부가 event id에 user id를 넣어주기를 기대하지도 않는다.
  빼면 출석(`daily_attendance` + 날짜)에서 하루에 한 사람만 조각을 받는다
- 길이 접두사 canonical encoding을 쓴다. 값에 `:` · `|`가 섞여도 조합이 뒤섞이지 않는다
- raw user id · raw external event id를 문서 ID에 노출하지 않는다. hash 결과만 쓴다
- `balance`는 음수가 되지 않는다. 부족하면 `InsufficientShards`로 거절하고 아무것도 쓰지 않는다
- 부호는 `credit` / `debit`이 정한다. 호출부가 음수를 넘겨 방향을 뒤집을 수 없다
- 로그에 user id · external event id · idempotency key를 남기지 않는다

### generic mutation endpoint 금지

`POST /shards/credit` · `/shards/debit` · `/shards/add` · `/shards/set` 을
**절대 만들지 않는다.** 하나라도 생기면 client가 원하는 만큼 조각을 만들 수 있다.

지급 / 차감은 **각자의 이유를 검증하는 전용 endpoint**로만 생긴다 —
출석(하루 1회) · AdMob SSV callback(서명 검증) · IAP(영수증 검증) · 구매 · 등록.
`tests/test_shards.py::test_no_generic_mutation_endpoint`가 고정한다.

### Daily Attendance (B-4) — 하루의 기준은 server KST

출석은 **하루 한 번 +1**이고, "하루"의 정의가 이 기능의 전부다.

- 날짜는 **server 시계 → Asia/Seoul → `YYYY-MM-DD`**다.
  client 날짜 · client timezone · 기기 시각 · UserDefaults · 앱 재설치 여부를
  근거로 쓰지 않는다. client가 시각을 넘기는 경로 자체가 없다
- `attendance_date(now=None)`의 `now`는 **test 전용**이다. production은 인자 없이 부른다.
  timezone-naive datetime은 거부한다 — 조용히 UTC로 가정하면 하루가 통째로 어긋난다
- **정책은 Asia/Seoul calendar day**, 현재 구현은 server-authoritative **UTC+09:00 고정
  offset**이다. 한국 현행 civil time이 UTC+09:00 고정이라 결과가 같고, container tzdata에
  의존하지 않는다. 한국 timezone rule이 바뀌면 `ZoneInfo("Asia/Seoul")`로 옮긴다 —
  날짜 계산이 `attendance_date()` 한 곳에만 있어 그 한 줄이 전부다
- 지급은 **B-3 원장만** 한다 —
  `credit(amount=1, reason=daily_attendance, external_event_id=<KST 날짜>)`.
  user scope는 service가 붙이므로 날짜만 넘긴다
- **출석 전용 collection을 만들지 않는다.** 상태 조회(GET)는 `has_event`로 원장에 묻는다.
  같은 경제 사실의 authority가 두 곳이 되면 언젠가 서로 다른 답을 한다
- POST는 **body를 받지 않는다.** `userId` · `date` · `amount` · `reason`을 받는 자리를
  만들지 않는다. 하나라도 열면 client가 보상을 정하는 구조가 된다
- 중복 출석은 **오류가 아니라 idempotent success**다 (`claimed=false, reward=0`).
  400으로 만들면 client가 재시도와 중복 tap을 구분해야 하는데, 재시도는 정상 동작이다
- 응답의 `balance`는 어느 경로에서나 **원장이 계산한 현재 잔액**이다.
  응답을 잃은 client가 다시 불러도 잔액이 부풀지 않고 제자리를 찾는다
- streak · 7일 보너스 · 달력 · 지난 출석 복구 · 알림은 **없다.** 딱 하루 한 번 +1이다

#### `claimed` semantics (API 계약)

| | 뜻 |
|---|---|
| POST `claimed=true` | **이 요청이 원장에 줄을 적고 reward를 지급했다** |
| POST `claimed=false` | 같은 event가 이미 적용돼 **이 요청은 지급하지 않았다** |
| GET `claimed` | 오늘 이미 받았는가 |

동시 10요청 → HTTP 200 × 10, `claimed=true`는 **정확히 1개**, 나머지 9개는 `reward=0`.

### 지급 여부는 transaction 결과에서만 나온다 (영구 규칙)

**`has_event` → `credit` 같은 check-then-act로 "지급했는가"를 정하지 않는다.**
조회와 쓰기 사이에 다른 요청이 끼어들면 둘 다 "내가 지급했다"고 답한다 —
잔액은 맞는데 응답이 거짓말을 하고, client는 받지도 않은 조각을 받았다고 표시한다.

- `ShardStore.apply`는 `(wallet, entry, applied)`를 돌려준다.
  `applied`는 **원자적 쓰기의 결과 그 자체**다
- `ShardLedgerService.credit` / `debit`은 `ShardMutationResult(wallet, applied)`를 돌려준다.
  기능마다 다른 판단 방법을 만들지 않는다 — B-5 SSV 재전송 · B-6 IAP 재검증 ·
  B-8 주문 재시도가 전부 이 값을 쓴다
- Firestore가 commit에서 `AlreadyExists`를 주는 것도 **실패가 아니다.**
  원장에 이미 있다는 뜻이므로 transaction을 한 번 다시 돌려 중복 분기로 들어간다.
  500으로 올리지 않는다
- `has_event`는 **읽기 전용 상태 조회**(GET)에만 쓴다. 지급 경로에서는 쓰지 않는다.
  `tests/test_attendance.py::test_claim_does_not_check_before_acting`과
  `test_slow_ledger_still_has_one_winner`가 이걸 고정한다

### AdMob Rewarded = SSV only (B-5, 영구 규칙)

보상 권위는 **Google AdMob Server-Side Verification callback 하나뿐이다.**
client의 `onUserEarnedReward`로 조각을 지급하지 않는다 — 검증되지 않은 입력이다.

- **raw query bytes를 그대로 검증한다.** 검증 입력은 ASGI `scope["query_string"]`이다.
  `Request.url.query`도 쓰지 않는다 — URL 객체를 거치면 parsing·재직렬화를 한 번 지난다.
  `signature=` 앞에서 **바이트를 자르는 것**이 전부다. decode · 정렬 · 재인코딩 금지.
  `verify()`는 문자열을 받으면 `TypeError`로 거절한다(재구성 경로가 굳지 않게)
- **검증 전에는 어떤 값도 믿지 않는다.** 특히 `timestamp`는 보상 날짜를 정하므로
  검증 전에 읽으면 공격자가 보상 날짜를 고를 수 있다
- 공개키는 `verifier-keys.json`에서 받아 **1시간** cache한다(하루보다 길게 믿지 않는다).
  모르는 `key_id`면 한 번 갱신하고 다시 찾는다. **key 조회 실패는 서명 실패와 구분한다** —
  전자만 5xx로 Google 재시도를 받는다. 서명 실패를 5xx로 주면 영원히 재시도가 온다
- 서명이 맞아도 **`ad_unit` · `reward_item` · `reward_amount`가 설정과 다르면 지급하지 않는다.**
  지급액은 `REWARD_PER_AD` 상수다 — callback의 숫자는 지급액의 출처가 아니라 검증 대상이다
- `ADMOB_SSV_EXPECTED_AD_UNIT`이 비어 있으면 **fail closed**다. 추측한 ID를 넣지 않는다
- **client의 ad unit ID와 SSV의 `ad_unit`을 같은 값으로 가정하지 않는다.**
  client는 `ADMOB_REWARDED_AD_UNIT_ID`(광고 load용), backend는
  `ADMOB_SSV_EXPECTED_AD_UNIT`(callback 비교용)이고 이름을 일부러 다르게 뒀다.
  같다고 단정해 하드코딩하면, 다를 경우 모든 보상이 조용히 거절된다
- 하루 5회 상한은 **`ShardStore.apply`의 `PeriodQuota`**로 건다 —
  확인 · 증가 · 원장 · 잔액이 한 transaction이다. 세어보고 지급하면 동시 callback이 상한을 넘긴다
- 중복(idempotency)으로 판정되면 **counter를 올리지 않는다.** 재전송이 남은 횟수를 깎으면 안 된다
- 보상 날짜는 **서명된 `timestamp`**에서 유도한다. 서버 수신 시각이 아니다 —
  23:59:58에 본 광고가 00:00:02에 도착해도 전날 몫이다
- 응답 원칙: **서명이 유효한데 "안 준다"고 판단이 끝난 상태는 전부 200**이다
  (중복 · 하루 상한 · context 없음/불명/만료 · ad unit 불일치 · 미설정).
  재시도해도 결과가 같은데 4xx를 주면 Google이 전달 실패로 보고 계속 다시 보낸다.
  **400**은 서명 실패 · signature/key_id 누락 · 형식 오류뿐이고,
  **5xx**는 key 조회 실패 · Firestore 오류처럼 재시도로 복구되는 것뿐이다
- context 실패는 `missing_context` / `unknown_context` / `expired_context`로 **나눠 기록한다.**
  SSV Test Tool은 정상적으로 `missing_context`이고, 실제 광고에서 이게 보이면
  client가 context를 안 싣고 있다는 신호다. 하나로 뭉치면 알아챌 수 없다

#### user binding — Google에 보내도 되는 것

callback URL은 로그 · 중계 · 재시도 기록에 남는다. 거기에
**session token · Apple identity token · 내부 user UUID를 넣지 않는다.**

short-lived opaque context(`ggumirror_reward_contexts`)만 보낸다. session과 같은 규칙이다 —
`secrets.token_urlsafe`, 저장은 `sha256`만, document ID도 hash, 수명 6시간.
callback이 와도 **소비하지 않는다**(Google 재전송 때도 같은 사용자로 풀려야 한다).

signed stateless token을 쓰지 않은 이유: **새 server secret이 필요하기 때문**이다.
이 서비스에는 secret이 하나도 없다. 보상 하나 때문에 secret 관리 · rotation · 유출 대응을
새로 들이는 것보다 이미 있는 패턴 재사용이 작고 안전하며, 저장형은 **취소할 수 있다.**

#### access log

우리 코드가 조심하는 것만으로 부족하다 — uvicorn access logger는 요청 줄을
**query까지 통째로** 남기고 거기에 Google signature와 reward context가 들어 있다.
`RedactSensitiveQuery`(`app/core/config.py`)가 `/admob/rewarded/ssv?<redacted>`로 바꾼다.
query에 credential이 실려 오는 경로를 새로 만들면 `SENSITIVE_QUERY_PATHS`에 추가한다.

## Structure Rules

기능이 생기기 전에 layer를 만들지 않는다. 다음을 미리 추가하지 않는다:

- repository abstraction을 더 쌓기 (`AuthStore` · `ShardStore` protocol 둘이 전부다 —
  각각 구현은 Firestore + test fake 둘뿐)
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

## Business Model Roadmap

B-3 원장 위에 얹는다. 전부 server가 지급 / 차감한다.

| Phase | 내용 | reason |
|---|---|---|
| B-4 ✅ | 출석 — 하루 1개 (Asia/Seoul day) | `daily_attendance` |
| B-5 ✅ | AdMob rewarded — 1개, 하루 5회, **SSV 필수** | `rewarded_ad` |
| B-6 | 조각 IAP — 10 / 30 / 70 / 160 | `iap_purchase` |
| B-7 | 꾸미러 Pass — ₩4,900 월 / ₩39,000 년 | 정책 확정 후 |
| B-8 | 마켓 — 등록 20 조각, 조각 구매 거울은 영구 소유 | `mirror_publish_fee` · `mirror_purchase` · `mirror_sale` |

## Next Phase

**B-6 — 조각 IAP.** 원장 · idempotency · 전용 endpoint · 원자적 상한이 모두 갖춰졌다.
B-6이 더할 것은 App Store 영수증 검증이고, transaction id를 external event id로 쓴다.

그 전에 **B-5 production 설정**이 남아 있다 — AdMob app / rewarded ad unit 생성,
SSV callback URL 등록, `ADMOB_SSV_EXPECTED_AD_UNIT` · `ADMOB_REWARD_ITEM` 배포.
그때까지 SSV endpoint는 살아 있되 fail closed다.

Cloud Run 자동 배포 workflow는 GCP project · service account ·
Workload Identity가 확정된 뒤에 만든다.
