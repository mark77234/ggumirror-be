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

## Current Implementation (Mirror Capacity Purchase 완료)

FastAPI + Apple token 검증 + Firestore User / Session + Bearer auth +
server-authoritative 조각 원장 + 하루 한 번 출석 + AdMob rewarded SSV + AI 스티커 생성 +
조각 IAP(Apple JWS 검증 + StoreKit client) + App Store 알림 V2 검증 · **환불 조각 회수 · 환불 되돌리기**.

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
app/api/capacity.py  GET·POST /users/me/mirror-capacity (거울 보관 공간)
app/capacity/        models(정책) · store(원자적 구매) · service
app/api/ads.py       GET /admob/rewarded/ssv (Google 서명이 곧 인증)
app/api/ai.py        config · POST /ai/stickers · GET {id} · GET {id}/image
app/ai/models.py     가격 · 상한 · 상태 전이표(can_transition) · lease/grace
app/ai/prompt.py     프롬프트 정리 (저장하지 않는다)
app/ai/provider.py   외부 image provider (gpt-image-2 · valid PNG · fail closed)
app/ai/service.py    차감 → 생성 → upload → 확정. 실패/중단이면 환불
app/ai/store.py      GenerationStore protocol + in-memory (terminal 권위 + lease CAS)
app/ai/firestore_store.py  ggumirror_ai_generations
app/ai/storage.py    결과 PNG private bucket (put/get/exists/delete)
app/ads/verifier.py  AdMob 공개키 cache + raw query ECDSA 검증
app/ads/service.py   검증 → 제품 대조 → context → 원장
app/ads/store.py     reward context (opaque token, hash 저장)
app/api/deps.py      Bearer → current user
app/shards/models.py ShardWallet, ShardLedgerEntry, ShardReason, idempotency_hash
app/shards/store.py  ShardStore protocol + in-memory
app/shards/firestore_store.py  Firestore transaction 구현
app/shards/service.py  ShardLedgerService (credit · debit · refund_iap · reverse_iap_refund
                       · apply_in_transaction — 호출자가 transaction을 소유)
app/shards/attendance.py  KST 날짜 규칙 + 출석 지급
app/core/config.py   환경변수 + logging
app/api/iap.py       POST /users/me/iap/shards (body는 signedTransaction 하나)
app/api/app_store.py POST /app-store/notifications/v2 (Apple 서명이 곧 인증)
app/api/catalog.py   GET(공개) /catalog/templates/stats · POST acquire · reconcile
app/api/marketplace.py  GET(공개) /marketplace/listings · {id} · {id}/preview
                     GET  /users/me/marketplace/listings (판매자 자기 목록)
                     POST /marketplace/listings · {id}/publish · {id}/unpublish · {id}/purchase
                     POST /marketplace/snapshots (multipart)
                     GET  /marketplace/listings/{id}/template · template/assets/{assetId}
                     PUT/DELETE /marketplace/listings/{id}/like
                     GET /users/me/marketplace/purchases · likes · listings
                     GET /users/me/marketplace/listings/{id}/preview (판매자 전용)
app/marketplace/models.py  Listing · Snapshot · 상태 · 등록비 정책
app/marketplace/store.py   MarketplaceStore protocol + in-memory
app/marketplace/firestore_store.py  등록비 + 게시가 **한 transaction**
app/marketplace/service.py  client가 정할 수 있는 것과 서버가 정하는 것의 경계
app/marketplace/assets.py  **GCS를 아는 유일한 파일.** 검증 + create-only 업로드
app/iap/notifications.py  알림 검증 · 분류 (조각은 만지지 않고 REFUND만 넘긴다)
app/iap/refunds.py   환불 회수 · 되돌리기 정책 (record가 금액 authority)
app/iap/models.py    catalog(10/50/100) · 전역 claim id · appAccountToken 대조
app/iap/verifier.py  검증 seam (protocol + fail-closed Unconfigured)
app/iap/apple_verifier.py  Apple 공식 SignedDataVerifier wrapper (environment별)
app/iap/certs/       Apple root certificate (DER, 공개값)
app/iap/service.py   검증 → bundle/type/env/catalog/token → 원장
scripts/admin_shards.py  운영자 조각 지급/회수 CLI (B-3 원장 재사용)
tests/               pytest (Apple · Firestore 호출 없음)
```

API는 health / auth / users / admob / ai / iap / app-store뿐이다. store · listing · marketplace는 없다.

조각을 움직이는 통로는 **다섯뿐**이고 전부 client가 값을 정할 수 없다:

| 통로 | 무엇이 인증하나 | 방향 |
|---|---|---|
| `POST /users/me/attendance` | Bearer session (body 없음) | +1 |
| `GET /admob/rewarded/ssv` | **Google ECDSA 서명** (Bearer 없음) | +1 |
| `POST /ai/stickers` | Bearer session (body는 프롬프트뿐) | **−5** |
| `POST /users/me/iap/shards` | Bearer session + **Apple 서명 JWS** | +10 / +50 / +100 |
| `POST /app-store/notifications/v2` | **Apple 서명 알림** (Bearer 없음) | **환불 회수만** (−, `REFUND` 한정) |

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
- **Apple 환불(`refund_iap`)만 예외 projection이다** — 음수 delta이면서 `lifetimeSpent`가
  아니라 `lifetimeRefunded`로 간다. 사용자가 쓴 것이 아니기 때문이다.
  다른 어떤 기능도 이 경로를 쓰지 않는다
- 로그에 user id · external event id · idempotency key를 남기지 않는다

### generic mutation endpoint 금지

`POST /shards/credit` · `/shards/debit` · `/shards/add` · `/shards/set` 을
**절대 만들지 않는다.** 하나라도 생기면 client가 원하는 만큼 조각을 만들 수 있다.

지급 / 차감은 **각자의 이유를 검증하는 전용 endpoint**로만 생긴다 —
출석(하루 1회) · AdMob SSV callback(서명 검증) · AI 스티커(프롬프트만 받는다) ·
IAP(영수증 검증) · 구매 · 등록.
`tests/test_shards.py::test_no_generic_mutation_endpoint`와
`tests/test_ai_stickers.py::test_no_generic_ai_mutation_endpoint`가 고정한다.

차감 endpoint도 같은 규칙이다: `POST /ai/stickers`의 body는 **프롬프트 하나뿐**이고
`amount` · `price` · `reason` · `userId`를 받는 자리가 없다.

### 조각 IAP (B-6) — 서명된 transaction만 믿는다

티어는 **10 / 50 / 100**이고 전부 **consumable**이다.

| productId | 조각 |
|---|---|
| `com.mark77234.ggumirror.shards.10` | 10 |
| `com.mark77234.ggumirror.shards.50` | 50 |
| `com.mark77234.ggumirror.shards.100` | 100 |

`POST /users/me/iap/shards`의 body는 **`signedTransaction` 하나뿐**이고
`extra="forbid"`라 `amount`를 몰래 얹을 수 없다.

#### 보안 invariant (영구 규칙)

- **`appAccountToken`이 Apple transaction을 사용자에 묶는다.** StoreKit 구매 때
  `.appAccountToken(<꾸미러 user UUID>)`를 싣고, 서버는 **서명된** 값이 지금 로그인한
  사용자와 같을 때만 지급한다. **없으면 거절한다** — "없으면 현재 사용자로 본다"로 두면
  남의 결제 JWS로 자기 지갑을 채울 수 있다.
  꾸미러 user id는 UUID v4이고 `sha256("apple:<subject>")` 매핑으로 **재생성되지 않는다**
  (identity 문서가 깨져 있으면 새 user를 만들지 않고 실패한다). 그래서 별도 token을 두지 않았다
- **`transactionId`는 전역에서 한 번만 쓰인다.** 원장 멱등 열쇠에는 `user_id`가 들어가
  **user 안에서만** 유일하므로, 같은 Apple transaction이 다른 사용자 이름으로 오면 막히지 않는다.
  그래서 `ggumirror_iap_transactions/{hash}` 전역 claim이 따로 있다.
  같은 사용자의 재전송은 `credited=false`, **다른 사용자면 409**다
- **claim · ledger · wallet은 한 Firestore transaction**이다(`ShardStore.apply`의 `claim` 인자).
  `PeriodQuota`와 같은 자리다 — 하나만 성공하는 상태가 없다
- **수량은 서버 catalog가 정한다.** client가 보낸 productId도 쓰지 않고,
  **JWS 안의 서명된 productId**를 열쇠로 쓴다
- **`finish()`는 서버가 지급을 확정한 뒤에만** 부른다(client 규칙). 먼저 finish하면
  응답을 잃었을 때 StoreKit이 재전달하지 않아 사용자가 돈만 내고 조각을 잃는다
- **Xcode StoreKit Testing JWS를 production backend가 받지 않는다.**
  `parse_allowed_environments`가 `Xcode`를 값에서 **버린다** — 설정으로도 켤 수 없다.
  로컬 `.storekit`은 client UX/복구 확인용이고, 실제 지급 검증은 Sandbox/TestFlight로 한다
- **환불 대응은 실제 판매 활성화 전 필수다.** consumable도 환불되며, 그때 조각을 회수하지
  않으면 무한 재구매·환불로 조각을 만들 수 있다. App Store Server Notifications V2의
  `REFUND`를 받아 반대 부호 entry를 쌓는 것이 **출시 blocker**다(선택 사항이 아니다)
- `IAP_ALLOWED_ENVIRONMENTS`가 비어 있으면 **아무것도 허용하지 않는다**(fail closed).
  Debug 빌드도 production API를 쓰므로 Sandbox를 켜면 sandbox 결제가 production 경제에 들어온다
- 로그에 raw `transactionId`를 남기지 않는다 — `sha256(txn)[:12]`만 (B-5 SSV와 같은 규칙)

#### JWS 검증은 Apple 공식 library가 한다 (B-6B)

`app-store-server-library==3.1.2`의 `SignedDataVerifier.verify_and_decode_signed_transaction`.
**x5c 인증서 체인 검증기를 직접 만들지 않는다** — 체인 · 만료 · 폐기 판단을 우리가 지면
틀렸을 때 조각이 공짜가 된다. `tests/test_iap_verification.py`가 소스에서 자작 구현을 금지한다.

library가 요구하는 것(설치본 소스에서 확인): 체인 길이 **정확히 3** · leaf에 OID
`1.2.840.113635.100.6.11.1` · intermediate에 OID `1.2.840.113635.100.6.2.1` ·
`X509_STRICT`(SKI/AKI + CA keyUsage) · alg **ES256**만.

**Apple root certificate** — 공개 값이라 secret이 아니다. `app/iap/certs/`에 DER로 커밋한다.
runtime에 외부 URL을 부르지 않는다(결제 검증이 남의 사이트 가용성에 묶이면 안 되고,
값이 조용히 바뀌어도 안 된다). 출처: <https://www.apple.com/certificateauthority/>

| 파일 | 이름 | SHA-256 (DER) |
|---|---|---|
| `AppleIncRootCertificate.cer` | Apple Root CA | `b0b1730ecbc7ff4505142c49f1295e6eda6bcaed7e2c68c5be91b5a11001f024` |
| `AppleRootCA-G2.cer` | Apple Root CA - G2 | `c2b9b042dd57830e7d117dac55ac8ae19407d38e41d88f3215bc3a890444a050` |
| `AppleRootCA-G3.cer` | Apple Root CA - G3 | `63343abfb89a6a03ebb57e9b3f5fa7be7c4f5c756f3017b3a8c488c3653e9179` |

Apple PKI가 공개한 root **세 개를 모두** 넣는다. 현재 App Store JWS 체인은 G3에 뿌리를 두지만,
G3 하나만 가정하면 Apple이 체인을 옮길 때 결제가 통째로 멈춘다. 셋 다 Apple 자신의 root라
신뢰 범위가 넓어지는 손실이 없고, bundleId · environment · 체인 길이 검사는 그대로 걸린다.

**`enable_online_checks=True`가 기본이다.** 인증서 **폐기(OCSP)** 확인을 켠다 —
돈이 오가는 경로에서 폐기된 서명 인증서를 받는 것이 더 위험하다. 조회가 실패하면
`RETRYABLE_VERIFICATION_FAILURE`를 받아 **503으로 올리고 지급하지 않는다**(우회 경로 없음).
client가 아직 `finish()`하지 않았으므로 그 결제는 유실되지 않고 다시 온다 —
그래서 fail closed의 비용이 싸다.

- **Production verifier에는 numeric `appAppleId`가 필수다.** library가 생성 시점에 거절하므로
  `IAP_APP_APPLE_ID`가 없으면 **Production만 꺼진다**(Sandbox는 그대로). 느슨하게 만들지 않는다
- **`.p8` / issuerId / keyId는 JWS 검증에 필요 없다.** App Store Server **API**를 부를 때만
  필요하고 지금은 부르지 않는다. 그래서 Secret Manager에 아무것도 더하지 않았다.
  환불 조회(B-6F)처럼 실제 API 호출이 필요해질 때 도입한다
- **unverified payload로 verifier를 고르지 않는다.** 서명 검증 전에 `environment`를 읽어
  Production/Sandbox를 선택하면 공격자가 그 값으로 검증 경로를 고를 수 있다.
  대신 **서버가 허용한 verifier들을 고정 순서로 각각 시도**하고 정확히 하나만 성공해야 한다
- **`Xcode` · `LocalTesting` verifier는 만들지 않는다.** library는 그 두 environment에서
  **서명 검증을 아예 건너뛴다**(`signed_data_verifier.py`에서 확인). 만들면 위조 payload가
  그대로 통과한다. `parse_allowed_environments`가 값 단계에서 버리고, verifier map에도 없다
- 검증 실패 사유를 client에 알려주지 않는다 — 로그에 `environment` + `status`만 남긴다

#### 환불 알림 (B-6F) — App Store Server Notifications V2

`POST /app-store/notifications/v2`. **Apple server-to-server라 Bearer가 없다** —
인증은 오직 Apple이 서명한 payload다(B-5의 Google SSV와 같은 모양).
body는 `signedPayload` 하나뿐이고 `extra="forbid"`다.

검증은 **B-6B verifier를 그대로 재사용**한다(`verify_and_decode_notification`).
서명 전에 `notificationType` · `environment` · `bundleId`를 읽어 verifier를 고르지 않고,
`Xcode` · `LocalTesting` verifier는 만들지 않는다.
**바깥 JWS가 맞다고 안쪽 `signedTransactionInfo`를 믿지 않는다** — 따로 검증한다.

##### B-6F-A 범위 — 경제 mutation 0

| 알림 | 처리 | status |
|---|---|---|
| `TEST` | 검증만 | 200 |
| `CONSUMPTION_REQUEST` | **환불 승인이 아니다.** 조각 불변, Apple에 응답도 안 함 | 200 |
| `REFUND_DECLINED` | 되돌릴 것 없음 | 200 |
| **`ONE_TIME_CHARGE`** | **consumable 구매의 정상 알림.** 검증만 하고 조각 불변 | 200 |
| `REFUND` | **조각 회수**(B-6F-B) — 아래 참고 | **200** / 400 / 503 |
| `REFUND_REVERSED` | **회수했던 만큼만 복구**(B-6F-C) | **200** / 400 / 503 |
| 그 밖의/새 타입 | 조각 영향 여부를 알 수 없음 | **503** |

**모르는 타입을 200으로 삼키지 않는다.** allowlist(`ACKNOWLEDGED_NOTIFICATIONS`)에
있는 것만 소비하고 나머지는 전부 deferred다 — 200으로 답하면 그 환불 알림은 영영 사라진다.

##### `ONE_TIME_CHARGE`는 지급 authority가 아니다

조각 IAP가 consumable이라 **구매마다 이 알림이 실제로 온다.** 그래서 deferred로 두면
정상 알림에 503을 주게 되고 Apple이 영원히 재시도한다 — allowlist에 넣는다.

다만 **이 알림으로 조각을 지급하지 않는다.** 지급 경로는 하나뿐이다:

```
client verified StoreKit transaction → POST /users/me/iap/shards
→ Apple JWS 검증 → 전역 claim → ledger/wallet
```

알림으로 또 지급하면 **한 결제에 두 번** 들어간다. `app/iap/notifications.py`에
`credit` · `SHARD_PRODUCTS` · `shard_amount` · `IAPService`가 **하나도 없고**
테스트가 그것을 고정한다.

**알림과 구매 endpoint의 순서를 오류로 취급하지 않는다.** 알림이 client fulfillment보다
먼저 도착할 수 있으므로, 우리는 **claim을 조회하지 않는다** — 조회하지 않으니 race가
성립하지 않고, claim 유무와 무관하게 200이다.

status 규칙은 B-5와 같다: **재시도로 결과가 달라지는 것만 5xx.**
서명·형식·bundle·environment 오류는 400(영구), 검증기 미설정·인증서 조회 실패는 503.

#### B-6F-B — 환불 조각 회수

`REFUND` 알림이 **조각을 실제로 회수하는 유일한 알림**이다. 다른 알림은 전부 검증까지다.

##### 금액의 authority는 원본 구매 claim이다

되돌릴 양은 `ggumirror_iap_transactions/{hash}`의 **`amount`**에서 나온다.
알림이 말한 값도, **지금의 `SHARD_PRODUCTS` catalog 값도 쓰지 않는다** —
catalog는 나중에 바뀔 수 있고, 그러면 예전 구매를 잘못된 금액으로 되돌리게 된다.
그 결제로 실제 나간 조각이 되돌릴 수 있는 최대치다.

원본 claim과 **대조하고 어긋나면 아무것도 하지 않는다**(`RefundMismatch` → 400):
`productId` · `environment` · 주인 · `amount > 0`.
주인 대조는 **정규화된 UUID 문자열이 정확히 같아야** 한다 —
지갑 문서 ID가 문자열이라, 표기만 다른 값을 통과시키면 엉뚱한 지갑에서 뺀다.

##### requested ≠ recovered

| | 뜻 |
|---|---|
| `requested` | Apple이 되돌리라고 한 양 (claim amount × 정책) |
| `recovered` | **지갑에서 실제로 뺀 양** = `min(balance, requested)` |
| `unrecovered` | 못 뺀 몫. **빚이 아니다** |

`balance`는 **절대 음수가 되지 않는다**(B-3 영구 규칙). 못 뺀 몫은 record에만 남고,
**나중에 번 조각에서 자동 상계하지 않는다.** 부채 시스템을 만들지 않았다.

    구매 50 · 잔액 10 · 전액 환불 → requested 50 · recovered 10 · unrecovered 40 · 잔액 0

`recovered == 0`도 **처리 완료된 환불**이다 — record는 남기고
**delta 0짜리 원장 줄은 만들지 않는다**(조각이 움직이지 않았다). Apple에게 200이다.

##### 회계 의미를 섞지 않는다

| projection | 뜻 | 환불 때 |
|---|---|---|
| `lifetimeEarned` | 누적 획득(gross) | **불변** — 받았다는 사실은 사라지지 않는다 |
| `lifetimeSpent` | 사용자가 **실제로 쓴** 양 | **불변** — 환불은 쓴 것이 아니다 |
| `lifetimeRefunded` | Apple 환불로 **실제 회수한** 양 | `+= recovered` |

**`lifetimeRefunded`에 `requested`가 아니라 `recovered`만 쌓인다.**
예전 지갑 문서에는 이 field가 없고 **읽을 때 0으로 본다**(migration script 없음).
일반 credit/debit은 이 값을 **그대로 물려준다** — 환불 뒤의 거래가 누적을 지우지 않는다.

##### generic debit을 재사용하지 않는다 (중요)

`ShardStore.apply`의 음수 delta는 `lifetimeSpent`로 집계된다.
환불에 그대로 쓰면 **"얼마나 썼는가"가 거짓말이 된다.**
그래서 projection이 다른 **전용 원자적 연산** `ShardStore.refund` /
`ShardLedgerService.refund_iap`가 따로 있고, generic debit의 의미는 손대지 않았다.

**새 public mutation endpoint가 아니다** — internal store/service 경로다.
환불을 부를 수 있는 통로는 Apple이 서명한 알림 하나뿐이다.

##### 한 transaction 안에서 전부

    구매 claim 읽기 → refund record 읽기 → 지갑 읽기
      → requested 계산(+대조) → record create → (recovered>0) 원장 create → 지갑 갱신

읽기가 전부 쓰기보다 먼저다(Firestore 규칙). 중간 상태가 없다.
`create`라서 우리가 읽은 뒤 다른 요청이 먼저 자리를 잡으면 commit이 `AlreadyExists`로
깨지고, 한 번 다시 돌려 중복 분기로 들어간다(`apply`와 같은 패턴).

##### 멱등은 원본 구매 transaction 기준

**`notificationUUID`를 쓰지 않는다** — Apple이 같은 환불을 다른 UUID로 다시 보낼 수 있다.
`ggumirror_iap_refunds/{sha256(len:"iap_refund"|len:transactionId)}` 문서 하나가
그 결제의 환불 전체를 대표한다. 이미 있으면 **지갑도 원장도 건드리지 않고**
그때 결과를 그대로 돌려준다(`applied=False`).

원장 열쇠는 다른 이유와 같은 `idempotency_hash(user_id, iap_refund, transactionId)`다.
둘 다 같은 transaction에서 쓰이므로 갈라질 수 없다.

##### refund record — business state만

`ggumirror_iap_refunds/{hash}`. **generic notification history가 아니다.**

    userId · productId · environment · purchaseTransactionClaimId
    originalAmount · requestedAmount · recoveredAmount · unrecoveredAmount
    revocationType · revocationPercentage · ledgerEntryId · createdAt · schemaVersion

`revocationPercentage`에는 Apple이 보낸 **raw milliunit integer**를 그대로 저장한다 —
50%는 `50000`이다. 50으로 정규화하면 원본 Apple 의미를 잃는다.
`requestedAmount`가 그것을 적용한 별도 계산 결과다.

저장하지 않는 것: raw `transactionId` · raw `originalTransactionId` ·
raw `appAccountToken` · `signedPayload` · JWS · notification payload.

`status` 같은 파생 field를 두지 않았다 — `unrecoveredAmount`가 이미 답이고,
파생 값은 언젠가 원본과 어긋난다. B-6F-C의 `reversedAmount`도 **미리 만들지 않았다**
(Firestore는 schemaless라 없는 field는 `lifetimeRefunded`처럼 0으로 읽으면 된다).

##### 되돌리지 않는 경우 (mutation 0 + 200)

| 상황 | 왜 |
|---|---|
| 원본 구매 claim 없음 | 우리가 조각을 준 적 없는 결제다. 되돌릴 것이 없다 |
| `FAMILY_REVOKE` | 가족 공유 회수. **일반 환불로 매핑하지 않는다** |
| `REFUND_PRORATED` + `revocationPercentage` 없음 | 얼마인지 모른다. **추측하지 않는다** |
| 모르는 `revocationType` | `observed_revocation_type=`만 남기고 fail closed |
| `appAccountToken` 없음/형식 오류 | 주인을 알 수 없다. 어느 지갑도 건드리지 않는다 |
| 안쪽 transaction 없음 | 되돌릴 대상을 알 수 없다 |

전부 **재시도해도 답이 같다** — 그래서 200이다. Apple에게 계속 503을 주지 않는다.

##### prorated — `revocationPercentage`는 **milliunits**다 (0..100 아니다)

⚠️ **Apple `revocationPercentage`의 단위는 milliunits이고 `100% = 100000`이다.**
`app-store-server-library`의 field 주석이 *"The percentage, in milliunits"*라고 말한다.
0..100 퍼센트로 읽으면 실제 환불의 **1/1000만** 회수하게 된다 —
실제로 그 버그를 냈고(B-6F-B.1), 그래서 단위를 이름과 상수에 박아 두었다:
`VerifiedTransaction.revocation_percentage_milliunits` ·
`MILLIUNITS_PER_UNIT = 100_000`.

    requested = min(originalAmount, max(1, originalAmount * p // 100_000))

`p > 0`인데 결과가 0이면 **최소 1**이다 — Apple이 일부를 돌려줬는데 우리가 아무것도
회수하지 않는 상태를 만들지 않는다. `originalAmount`를 넘지 않는다.

| original | p (milliunits) | 실제 % | requested |
|---|---|---|---|
| 50 | 50000 | 50% | 25 |
| 50 | 33000 | 33% | 16 |
| 10 | 33000 | 33% | 3 |
| 10 | 1000 | 1% | **1** (최소 1) |
| 50 | 67932 | 67.932% | 33 |
| 10 | 15 | 0.015% | **1** (최소 1) |
| 10 | 100000 | 100% | 10 |

`p <= 0` 또는 `p > 100000`은 해석 불가능한 payload라 **400**(영구 실패)이고 조각은 그대로다.

**`REFUND_FULL`은 percentage를 금액 authority로 쓰지 않는다** — 값이 있든 없든
원본 claim amount 전체다.

property로 고정한다: `0 < p <= 100000`이면 언제나 `1 <= requested <= originalAmount`이고,
`p`가 오르면 회수량은 절대 줄지 않는다(monotonic).

##### reason

Apple IAP 환불은 **`iap_refund`**다. 기존 **`refund`는 AI 생성 실패 복구용**이라 섞지 않는다 —
섞으면 원장에서 둘을 구분할 수 없다.

`iap_refund_reversed`는 **아직 만들지 않았다.** 쓰지 않는 enum을 미리 넣지 않는다(B-6F-C).

##### status 매핑

| 상황 | status |
|---|---|
| 처리 완료 · 중복 · 되돌릴 것 없음 | **200** |
| 원본 구매와 불일치 · 해석 불가능한 percentage | **400** (영구) |
| Firestore 장애 | **503** (재시도로 달라진다) |
| `REFUND_REVERSED` · 모르는 타입 · **환불 처리기 미설정** | **503** |

"되돌릴 것이 없다"와 "되돌리지 못했다"는 다르다.

##### 여전히 하지 않는 것

- **Apple에 consumption data를 보내지 않는다.** 동의 흐름이 없다.
  `.p8` · issuerId · keyId · `AppStoreServerAPIClient`를 도입하지 않았다(테스트가 고정)
- generic notification history collection을 만들지 않는다
- `REFUND_REVERSED`(B-6F-C)는 **200으로 삼키지 않는다** — Apple이 다시 보내게 둔다

`tests/test_iap_refunds.py`가 위 전부를 고정한다.

#### B-6F-C — 환불 되돌리기 (`REFUND_REVERSED`)

Apple이 분쟁 등으로 **환불을 되돌렸을 때** 회수했던 조각을 복구한다.

##### 복구량 authority는 refund record의 `recoveredAmount`다

**실제로 회수한 만큼만** 되돌린다. `originalAmount` · `requestedAmount` · catalog 값 ·
알림이 말한 값은 쓰지 않는다.

    구매 50 · 환불 요청 50 · 당시 잔액 10 → 회수 10 · 미회수 40
    REFUND_REVERSED → **복구 10**   (50도 40도 아니다)

미회수 40은 애초에 우리 지갑에서 나간 적이 없으므로 되돌릴 대상이 아니다.
`recoveredAmount == 0`이었다면 복구도 **0**이고 원장 줄을 만들지 않는다.

##### 회계 — `lifetime*`은 모두 gross이고 줄지 않는다

기존 정의를 **바꾸지 않았다.** `lifetime_earned`에 이미
*"환불이 나도 줄지 않는다 — 받았다는 사실은 사라지지 않는다"*가 박혀 있고,
`lifetimeRefunded`도 *"recovered만 **쌓인다**"*로 쓰여 있다 — 둘 다 누적이다.

| projection | 되돌릴 때 |
|---|---|
| `lifetimeEarned` | **불변** — 원래 구매가 이미 올렸다. 또 올리면 한 결제를 두 번 센다 |
| `lifetimeSpent` | **불변** |
| `lifetimeRefunded` | **불변**(gross) — 회수는 실제로 일어난 사건이다 |
| `lifetimeRefundReversed` | `+= restored` (새 field, 뒤로 호환: 없으면 0) |
| `balance` | `+= restored` |

`lifetimeRefunded`를 줄이지 않은 이유: 그러면 `lifetime*` 중 유일하게 감소하는 field가
되고, "gross 누적"이라는 이미 고정된 의미를 조용히 net으로 바꾸는 일이다.
반복 구매–환불을 감시할 때도 gross가 필요한 값이다.

**대신 counterpart field를 추가해 지갑만으로 검산할 수 있게 했다:**

    balance == lifetimeEarned - lifetimeSpent - lifetimeRefunded + lifetimeRefundReversed

이 field가 없으면 지갑이 거짓말처럼 보인다(잔액 146인데 164−18−10=136).
authority는 여전히 **원장 replay**(`sum(delta) == balance`)이고, 테스트가 둘 다 고정한다.

##### 순서를 가정하지 않는다 (`REFUND`가 늦게 올 수 있다)

`REFUND_REVERSED`가 왔는데 환불 record가 없으면 두 갈래로 나눈다:

| 상황 | 처리 |
|---|---|
| 구매 claim도 없다 | 우리가 지급한 적 없는 결제다. **200** + mutation 0 |
| 구매 claim은 있는데 record가 없다 | `REFUND`가 아직 안 왔을 수 있다. **503 — 재시도를 받는다** |

**후자를 200으로 삼키면 사용자가 되돌려받아야 할 조각을 영구히 잃는다.**
근거: Apple V2 payload에는 순서를 알려주는 field가 없고
(`notificationUUID` · `signedDate` · `version`뿐, 설치본 model에서 확인),
`app-store-server-library`도 ordering 보장을 문서화하지 않는다.
게다가 우리는 notification history를 조회할 credential(`.p8`)을 의도적으로 갖고 있지 않아
사후 재조정 수단이 없다. 그래서 **"지금 없다"를 "영영 없다"로 단정하지 않는다.**

재시도가 실제로 결과를 바꾸는 것이 테스트로 고정돼 있다
(`test_deferred_reversal_succeeds_after_the_refund_arrives`).

##### 멱등 — 원본 구매 transaction 기준

`notificationUUID`를 쓰지 않는다. 남은 복구량이 곧 멱등 열쇠다:

    remaining = recoveredAmount - reversedAmount

첫 번째는 10을 복구하고 `reversedAmount = 10`이 된다. 두 번째부터는 `remaining == 0`이라
**지갑도 원장도 건드리지 않고** 200이다. 별도 claim 문서를 두지 않았다 — record 자체가
그 역할을 한다. 원장 문서 ID도 결정적이라 동시 요청에서 한쪽은 `AlreadyExists`로 깨지고
다시 돌아 0을 읽는다(`apply`와 같은 패턴).

##### record 확장 — 최소 3개

기존 `ggumirror_iap_refunds/{hash}` 문서를 그대로 쓰고 **덮어쓰지 않는다**(`update`).

    reversedAmount · reversalLedgerEntryId · reversedAt

B-6F-B가 만든 record에는 이 field가 없으므로 **없으면 0**으로 읽는다.
`recoveredAmount` · `unrecoveredAmount` 같은 환불 사실은 건드리지 않는다.
raw transactionId · appAccountToken · JWS · payload는 여전히 저장하지 않는다.

##### 대조 (fail closed)

record의 `productId` · `environment` · `userId`가 검증된 transaction과 어긋나면
`RefundMismatch` → **400**, 조각 mutation 0. 주인 대조는 정규화된 UUID **문자열이 정확히**
같아야 한다. `appAccountToken`이 없거나 형식이 틀리면 복구하지 않는다.

##### reason

**`iap_refund_reversed`**(delta > 0). `iap_purchase` · `iap_refund` ·
`refund`(AI 복구)와 섞지 않는다 — 원장에서 넷을 구분할 수 있어야 한다.

##### `DEFERRED_NOTIFICATIONS`는 이제 비어 있다

`REFUND`와 `REFUND_REVERSED`를 실제로 처리하므로 목록에 남은 것이 없다.
개념은 그대로 둔다 — 조각에 영향이 있을 수 있는 새 타입이 생기면 여기 넣는다.
**모르는 타입은 이 목록과 무관하게 여전히 deferred(503)**이고, 처리기가 설정되지 않은
환불 계열도 deferred다(fail closed).

`tests/test_iap_refunds.py`가 위 전부를 고정한다.

##### 로그 · URL

로그에 raw `signedPayload` · raw `transactionId` · raw `appAccountToken` · 인증서 체인을
남기지 않는다. 남기는 것은 `notificationType` · `subtype` · `environment` ·
`transaction=sha256(txn)[:12]` · 결과뿐이고, **transaction이 없는 TEST에는 hash를 만들지 않는다**(`-`).

##### TEST notification은 live acceptance 필수가 아니다

Apple의 *Request a Test Notification* API는 **App Store Server API JWT 인증**을 요구한다
(`.p8` · keyId · issuerId). 우리는 그 credential을 의도적으로 갖고 있지 않고,
알림 하나를 보내려고 새로 만들지 않는다.

그래서 B-6F-A의 실제 acceptance는 **Sandbox consumable 구매 1회로 오는
`ONE_TIME_CHARGE`**로 한다. TEST 200 동작은 단위 테스트로만 고정한다.

App Store Connect URL은 **Sandbox 먼저**:
`https://ggumirror-api-cmyv4amroa-du.a.run.app/app-store/notifications/v2`
Production URL은 출시 직전 **별도 gate**로 연결한다 — Sandbox만 설정하면
production 알림은 전송되지 않아, 검증되지 않은 코드가 실제 결제에 닿지 않는다.

`tests/test_app_store_notifications.py`가 위 전부를 고정한다.

#### client 복구 계약 (B-6C)

1. 앱 시작 즉시 `Transaction.updates` listener를 띄운다
2. 인증된 서버 세션이 준비된 뒤 `Transaction.unfinished`를 sweep한다
3. `VerificationResult.verified`만 backend에 제출한다
4. backend가 지급을 확정한 뒤에만 `transaction.finish()`

`Transaction.currentEntitlements`는 **consumable 복구에 쓰지 않는다** — 소모품은 거기에 남지 않는다.

### 호출자가 소유하는 transaction (B-7B) — Marketplace의 토대

`credit` / `debit` / `refund_iap` / `reverse_iap_refund`는 **각자 transaction을 열고 닫는다.**
그래서 marketplace 구매처럼 **지갑 두 개 + 원장 두 줄 + 소유권**을 한 번에 커밋해야 하는
경우에 쓸 수 없다 — 두 번 부르면 "구매자만 차감된" 중간 상태가 실제로 생긴다.

`ShardLedgerService.apply_in_transaction(transaction, user_id, delta, reason, external_event_id)`은
**이미 열려 있는 transaction에 조각 이동 하나를 얹기만 한다.** 열지도 commit하지도 않는다.

- **범용 이체가 아니다.** 보내는 사람·받는 사람을 한 번에 받는 자리가 없고
  `POST /shards/transfer` 같은 endpoint도 만들지 않는다 —
  marketplace 밖의 임의 조각 이동은 제품에 없다. 테스트가 소스에서 금지한다
- 방향은 `delta` 부호 + 호출부가 고른 `reason`이 함께 정한다
  (`mirror_purchase` −, `mirror_sale` +, `mirror_publish_fee` −)
- **같은 attempt에서 같은 지갑을 두 번 바꾸면 `WalletAlreadyChanged`다.**
  Firestore transaction의 읽기는 시작 시점 snapshot이라 두 번째가 첫 번째를 못 보고
  덮어쓴다 — 조각이 조용히 사라지는 경로다. 자기 자신에게 파는 경우가 정확히 이 모양이라
  상위 service의 검사에만 기대지 않고 저장소에서 막는다
- ⚠️ **그 기록은 `ShardTransactionContext`에 담고, transaction 객체에 붙이지 않는다.**
  Firestore는 commit이 `ABORTED`되면 **같은 Python `Transaction` 객체로** callable을
  다시 부른다(설치본 2.22.0 `_Transactional.__call__`이 loop 안에서 같은 객체를 넘긴다).
  `_clean_up()`이 지우는 것은 `_write_pbs`와 `_id`뿐이라, transaction에 붙인 표시는
  **다음 시도까지 살아남아** 아무것도 commit되지 않았는데 재시도가 거절된다.
  실제 SDK wrapper로 재현했고(B-7B.1), 그래서 호출부가 attempt마다 새 context를 만든다:

      @firestore.transactional
      def run(transaction):
          scoped = shards.context(transaction)   # ← 시도마다 새 기록
          shards.apply_in_transaction(scoped, buyer, -price, MIRROR_PURCHASE, listing_id)

  재시도는 **지갑을 다시 읽는다** — 이전 시도가 본 잔액으로 계산하지 않는다
- 잔액 부족은 `InsufficientShards` — **호출자의 transaction 전체가 취소된다.** 음수 불가
- 멱등 열쇠는 다른 이유와 **같은 `idempotency_hash`**다. 구매자/판매자/수수료가
  각각 다른 문서 ID를 갖는다(user_id와 reason이 섞이므로)
- quota · 전역 claim은 다루지 않는다. 필요하면 `apply`를 쓴다

**기존 `apply`를 이 primitive 위에 다시 얹지 않았다.** `apply`의 읽기 순서
(claim → 멱등 → quota → 지갑)에는 "중복이면 quota를 깎지 않는다" 같은 규칙이 박혀 있고,
살아 있는 지급 경로 전부가 그 위에 있다. 대신 **지갑 projection 계산만 `_moved()` 한 곳으로
뽑아** 둘이 같은 규칙을 쓰게 했다 — 중복은 없애고 검증된 choreography는 건드리지 않았다.

test용 `InMemoryTransaction`은 쓰기를 모았다가 commit에서 반영한다.
Firestore의 all-or-nothing을 흉내 내야 "구매자만 차감된 상태가 없다"를 실제로 시험할 수 있다.

`tests/test_shard_transactions.py`가 위 전부를 고정한다.

### 이름은 전역에서 겹치지 않는다 (Device QA)

사용자 이름과 상품 이름 둘 다 **document id를 claim으로 쓰는** 같은 패턴이다 —
"조회해서 없으면 쓴다"가 아니라 transaction 안의 `create()`가 승자를 정한다.

| | collection | 열쇠 |
|---|---|---|
| 사용자 이름 | `ggumirror_username_claims` | `display_name_key(name)` |
| 상품 이름 | `ggumirror_listing_title_claims` | `listing_title_key(title)` |

정규화는 **NFC → strip → casefold**다. `Pink` · ` pink ` · `PINK`가 같은 이름이고,
자모가 풀린 한글도 같은 이름이다. 두 domain이 각자 함수를 갖지만 규칙은 같다.

- **이름 바꾸기는 새 열쇠를 먼저 잡고 옛 열쇠를 놓는다.** 순서가 반대면 그 사이에
  남이 옛 이름을 가져갈 수 있고, 실패했을 때 이름을 잃는다
- 상품 이름 claim은 **등록비 차감보다 먼저** 확인한다 — 이름이 겹쳐 실패할 등록에서
  조각을 빼지 않는다. 삭제 · 내리기는 열쇠를 놓는다(`_release_title`)
- **이름 namespace는 거울과 스티커가 공유한다.** 같은 상점에 같은 이름이 둘 있으면
  사는 사람이 구분할 수 없다
- 겹치면 **409**이고 client가 사용자 말로 옮긴다. 자동 rename하지 않는다

#### ⚠️ claim collection이 **유일한** authority다 — 그래서 backfill이 필요했다

`set_display_name`도 `publish`도 사용자 문서나 listing을 다시 훑지 않는다.
빠르고 동시성이 정확하고 composite index가 필요 없지만, 대가가 하나 있다:
**규칙보다 먼저 만들어진 이름은 자리를 잡고 있지 않다.**

그대로 출시했으면 기존 `byeongchan`이 있는데도 새 사용자가 `byeongchan`을,
기존 `풍경 거울`이 있는데도 새 상품이 `풍경 거울`을 그대로 가져갔다.
`tests/test_name_claim_backfill.py`의 `..._is_unprotected_without_a_claim` 두 개가
그 구멍을 재현해 놓았다 — **그 테스트가 실패하기 시작하면** 다른 방어가 생겼다는
뜻이므로 backfill 전제를 다시 판단한다.

`scripts/backfill_name_claims.py`가 **기존 값을 그대로** index에 넣는다.
이름 변경 · 제목 변경 · 삭제 · 승자 선택을 하지 않고, 겹치는 것이 하나라도 보이면
**아무것도 쓰지 않고 멈춘다** — 누구의 이름인지는 사람이 정할 문제다.
`create()`로 하나씩 쓰므로 두 번 돌려도 결과가 같고, 그 사이에 실제 사용자가
자리를 잡았으면 그쪽이 이긴다.

2026-08-28 production 실행: username claim 1개 · listing title claim 6개 생성,
conflict 0 · owner mismatch 0 · **user/listing 문서 write 0**.

**앞으로 uniqueness claim을 새로 도입할 때는 기존 데이터 backfill을 같은 단계에서 한다.**
create 경로만 claim을 만들면 그 index는 과거를 모른다.
- **기존 데이터를 고치지 않았다.** production audit 결과 published 상품과 사용자 이름에
  실제 collision이 없어서, 새 규칙을 넣으면서 migration을 돌릴 필요가 없었다.
  `seed_display_name`은 이미 쓰이는 이름이면 조용히 넘어간다

### 상품이 내려가면 판매자에게 알린다 (Device QA)

운영자가 `takedown`하면 **같은 transaction에서** 알림을 쌓는다
(`NotificationType.MARKETPLACE_TAKEDOWN` · `takedown_event_id(listing_id)`).

- 기존 알림 infra를 그대로 쓴다 — 새 전송 경로를 만들지 않았다
- **실제 사유를 담는다.** `TAKEDOWN_REASON_LABELS`가 운영자가 고른 사유를
  그대로 옮긴다. "부적절한 내용" 하나로 납작하게 만들면 판매자가 무엇을 고쳐야
  하는지 알 수 없다
- **판매자에게만** 간다. 구매자·다른 사용자에게 가지 않는다
- event id가 listing 기준이라 재시도해도 **한 번**이다

### Marketplace 등록 (B-7C)

거울/스티커를 상점에 올린다. **등록비 차감과 게시가 한 Firestore commit**이다.

| | 등록비 | 원장 reason |
|---|---|---|
| 거울 | **10 조각** | `mirror_publish_fee` |
| 스티커 | **10 조각** | `sticker_publish_fee` |

- **서버가 비용의 authority다.** client에도 같은 숫자가 있지만 화면용이고,
  요청 body에 비용 · 판매자 · 상태 · counter를 실을 자리가 **없다**
- **무료 상품(`priceShards=0`)도 등록비는 같다** — "만드는 값"이지 "파는 값"이 아니다
- reason을 콘텐츠 종류별로 나눴다 — 원장만 보고 무엇이었는지 알 수 있어야 한다.
  **`mirror_publish_fee` 값은 바꾸지 않았다**(rename하면 과거 원장 파싱이 깨진다)
- draft 생성은 **무료**다. 만들다 만 것에 돈을 받지 않는다
- **서버에 없는 snapshot은 draft로도 만들지 않는다** — client가 준 문자열만 믿지 않는다
- 상태는 `draft` · `published` · `unlisted` **셋뿐**이다. 심사/보류를 MVP에 만들지 않는다
- **unpublish는 경제 mutation 0**이다. 낸 등록비는 돌아오지 않고 `publishFeePaid`도 그대로다
- **republish는 무료**이고 `publishedAt`(최초 업로드 날짜)을 **덮어쓰지 않는다**
- `downloadCount` · `likeCount`는 field만 있다. 실제 증가는 B-7E(소유권 획득) · like phase
- 남의 listing은 **404**다 — 존재 사실도 정보다(AI 스티커와 같은 규칙)

게시가 이미 돼 있으면 `published=false`로 조용히 끝난다 — 재시도 · 연타가 오류가 아니다
(B-4 `claimed` · B-6 `credited`와 같은 semantics). HTTP 오류로 만들지 않는다.

#### 공개 조회 (B-7D)

```
GET /marketplace/listings?contentType=mirror|sticker&sort=latest|popular|likes
GET /marketplace/listings/{id}
```

**로그인 없이 볼 수 있다** — 상점 구경에 로그인 벽을 세우지 않는다(Core Product Policy).
등록 · 게시 · 내리기는 그대로 인증 필수다.

- **`published`만 보인다.** draft · unlisted · 없는 것은 전부 404이고,
  **판매자 자신도** 이 경로로는 자기 draft를 볼 수 없다 —
  공개 조회와 판매자 관리를 한 endpoint에 섞지 않는다
- `publishedAt`이 없는 `published` 문서는 있을 수 없는 상태다.
  **거짓 날짜를 지어내지 않고** 공개에서 빼고 `marketplace_listing_malformed`로 남긴다
- **조회가 counter를 올리지 않는다.** `downloadCount`는 B-7E(소유권 획득 성공),
  `likeCount`는 별도 like phase다

##### 공개 응답에 담지 않는 것

```
sellerUserId (내부 user UUID) · snapshotId · publishFeePaid
schemaVersion · createdAt · updatedAt · status
```

Firestore 문서를 그대로 내보내지 않는다 — `PublicListingResponse`가 따로 있고,
담는 것은 `id · contentType · title · description · priceShards ·
downloadCount · likeCount · publishedAt` **여덟 개뿐**이다.
필드를 늘리려면 **왜 공개해야 하는지**부터 답한다.

판매자 표시 이름도 없다. seller profile이 없으므로 "익명" 같은 가짜 이름을 만들지 않는다.

##### 정렬 — client UI-P3와 같은 계약

| UI | authority | tie-breaker |
|---|---|---|
| 최신 순 | `publishedAt` DESC | listingId |
| **인기 순** | **`downloadCount` DESC** | publishedAt DESC → listingId |
| 좋아요 순 | `likeCount` DESC | downloadCount DESC → publishedAt DESC → listingId |

**"인기"는 다운로드 수 하나다.** 가중 점수를 만들지 않는다(테스트가 소스에서 금지).
마지막 열쇠가 언제나 `listingId`라 값이 모두 같아도 순서가 흔들리지 않는다.

##### 질의 전략 — composite index를 지금 만들지 않는다

Firestore 질의는 **`status == published` 하나뿐**이고, 종류 필터와 정렬은
application에서 한다. 정렬 셋마다 index를 만들면 production에 index 세 개를
지금 요구하게 된다 — 초기 상품 수가 작고 pagination도 없으므로 index 없이 시작한다.
규모가 커지면 index와 pagination을 **함께** 넣는다.

목록에서 snapshot을 추가 조회하지 않는다(N+1 금지) — 공개에 필요한 값은
listing 문서에 이미 다 있고, snapshot 검증은 게시 시점(B-7C)에 끝났다.

#### 구매 · 소유권 (B-7E)

```
POST /marketplace/listings/{id}/purchase     (인증, **body 없음**)
GET  /users/me/marketplace/purchases         (인증)
```

**구매자 차감 · 판매자 지급 · 소유권 생성 · `downloadCount +1`이 한 Firestore commit**이다.
counter를 별도 transaction으로 올리지 않는다 — 죽으면 수가 어긋난다.

- **가격의 authority는 transaction 안에서 읽은 listing**이다. 요청 body가 아예 없어
  가격 · 판매자 · 수량 · counter를 client가 정할 자리가 없다
- **거래 수수료 0%** — 구매자가 낸 만큼 판매자가 정확히 받는다
- **무료(`priceShards=0`)**: 지갑도 원장도 건드리지 않고 소유권과 counter만 생긴다.
  delta 0짜리 원장 줄을 만들지 않는다
- **자기 상품은 살 수 없다.** 판매자는 사지 않고도 쓸 권리가 있다 —
  접근 판단은 `소유권 있음 OR 판매자 본인`이다
- 내려간(`unlisted`) 상품은 **살 수 없지만 이미 산 사람의 권리는 그대로다**

##### 소유권 = 구매 기록 (한 문서)

`ggumirror_marketplace_ownership/{sha256(len:"marketplace_ownership"|len:userId|len:listingId)}`

**문서 ID가 곧 business 멱등 열쇠**다. `create()`로 쓰므로 중복 구매가 구조적으로 막힌다.
별도 `purchases` collection을 만들지 않았다(MVP 결정).

구매 당시의 `snapshotId` · `sellerUserId` · `pricePaid`를 **고정 저장한다** —
나중에 정책이 바뀌어도 산 사람의 권리가 흔들리지 않는다. 만든 뒤 고치지 않는다.

##### ⚠️ 원장 event id는 소유권 id다 (listingId가 아니다)

```
buyer  : idempotency_hash(buyer,  {mirror|sticker}_purchase, ownershipId)
seller : idempotency_hash(seller, {mirror|sticker}_sale,     ownershipId)
```

`listingId`를 쓰면 판매자 쪽 열쇠가 `(판매자, sale, listingId)`가 되어
**구매자가 달라도 같은 원장 문서를 겨룬다** — 8명이 사면 판매자가 한 번만 받는다.
동시성 test가 실제로 그것을 잡았다(`applied=False`).

##### reason

등록비와 **같은 규칙**으로 종류별로 나눈다:

```
mirror_purchase · mirror_sale · sticker_purchase · sticker_sale
```

`mirror_*` 값은 바꾸지 않았다 — 이름이 "거울 전용"이라는 뜻이 됐을 뿐이고,
rename하면 과거 원장을 읽는 코드가 깨진다.

##### `downloadCount`

의미는 **"최초 소유권 획득 성공"**이다. 중복 요청 · 재시도 · 판매자 본인 · 미리보기 ·
조회는 **올리지 않는다**. listing을 transaction에서 읽고 `읽은 값 + 1`을 쓰므로
동시 구매는 충돌로 재시도되어 정확히 직렬화된다(8명 → 정확히 +8).

##### 읽기 순서

Firestore transaction은 읽기가 쓰기보다 먼저여야 한다. **marketplace 문서(listing ·
소유권)를 전부 먼저 읽고** 그 뒤에 조각 primitive를 부른다 — primitive가 읽는 것은
원장·지갑이라 서로 다른 문서이고, 그 뒤로 marketplace 문서를 다시 읽지 않는다.
테스트가 소스에서 이 순서를 고정한다.

#### 좋아요 (B-7E.1)

```
PUT    /marketplace/listings/{id}/like     (인증, body 없음)
DELETE /marketplace/listings/{id}/like     (인증)
GET    /users/me/marketplace/likes         (인증)
```

`ggumirror_marketplace_likes/{sha256(len:"marketplace_like"|len:userId|len:listingId)}`

**관계 문서가 authority이고 `listing.likeCount`는 projection이다.** 관계 생성/삭제와
count 변경이 **한 Firestore commit**이다.

- **조각 경제와 무관하다.** 좋아요 경로에 원장도 지갑도 없다 —
  `apply_in_transaction(` · `shards.` · `.credit(` · `.debit(`가 없음을 테스트가 검사한다
- **사용자당 상품당 최대 1개.** 문서 ID가 그 조합의 hash이고 `create()`로 쓴다
- 새 좋아요는 **`published`만**. draft · unlisted는 404
- **`unlisted`여도 취소할 수 있다** — 판매자가 내렸다고 사용자가 자기 좋아요를 못 지우면
  count가 영구히 남는다
- **자기 상품에 좋아요 불가**(판매자가 자기 인기도를 올리지 못하게). **취소는 허용** —
  잘못 생긴 관계를 지우는 동작이다
- 반복 요청은 `changed=false`로 조용히 끝난다. HTTP 오류가 아니다
- `likeCount`가 **음수가 되지 않는다.** 관계가 있는데 count가 0이거나 count가 음수면
  `LikeCountInconsistent`다 — **조용히 보정하지 않는다**(거짓 값으로 덮으면 언제부터
  틀렸는지 알 수 없다)
- 공개 목록에 `likedByMe`를 넣으려고 **optional auth를 만들지 않았다** —
  client가 공개 목록과 `/users/me/marketplace/likes`를 합친다

count를 바꾸는 것은 LIKE/UNLIKE뿐이다. browse · detail · purchase · unpublish ·
republish는 `likeCount`를 건드리지 않고, 좋아요는 `downloadCount`를 건드리지 않는다.

**정렬 회귀 없음** — 좋아요가 늘어도 `popular`(=`downloadCount`) 순서는 그대로다.

`tests/test_marketplace.py`가 위 전부를 고정한다. 여기에는 **최소 fake db로
`FirestoreMarketplaceStore`를 직접 돌리는 harness**가 있어, 실제 `@firestore.transactional`
재시도 loop에서 count가 두 번 오르지 않는 것을 확인한다(production Firestore를 부르지 않는다).

#### Snapshot asset (B-7F)

```
POST /marketplace/snapshots                              (인증, multipart/form-data)
GET  /marketplace/listings/{id}/preview                  (**공개**)
GET  /marketplace/listings/{id}/template                 (인증, 판매자 또는 구매자)
GET  /marketplace/listings/{id}/template/assets/{assetId}(인증, 같은 권한)
```

**새 template schema를 만들지 않았다.** client를 먼저 읽고 확인한 것:

- `MyMirror` · `StickerProject`에 이미 완전한 `Codable`이 있다
- 모델에 **파일 경로가 없다** — 이미지는 `assetID`(UUID)로만 참조한다
- master geometry(1080 × 2340, insets)는 **상수**이고 좌표는 0…1 정규화다

그래서 package는 *client가 이미 저장하는 JSON + `assetID` 이름의 PNG*다.
서버는 manifest를 **해석하지 않는다** — 검증만 하고 바이트로 보관한다.
꾸미러 geometry가 문서에 없으므로 버전이 갈려도 좌표가 뒤틀리지 않는다.

저장 배치 — snapshotId마다 독립 prefix다:

```
marketplace/snapshots/{snapshotId}/manifest.json
marketplace/snapshots/{snapshotId}/preview.png
marketplace/snapshots/{snapshotId}/assets/{assetId}.png
```

**불변성은 저장소가 보증한다.** 업로드는 전부 `if_generation_match=0`이다 —
이미 있으면 412이고 우리는 `AssetAlreadyExists`로 바꾼다. snapshotId는 서버가 만들고,
덮어쓰기 · 수정 · 삭제 API가 **없다.** 구매자의 권리가 이 바이트에 걸려 있어서,
판매자가 나중에 내용을 바꾸면 산 것과 다른 것을 갖게 된다.

**문서를 마지막에 쓴다.** object를 전부 올린 다음 Firestore snapshot 문서를 만든다.
중간에 실패하면 문서가 없고, 문서가 없으면 어떤 listing도 그 snapshot을 참조할 수 없다.
이미 올라간 object는 best-effort로 치우고, 남더라도 참조 불가능한 orphan이다 —
**반쪽 snapshot이 상품이 되는 것보다 orphan이 낫다.**

검증(신뢰 경계) — 확장자·Content-Type을 믿지 않고 **바이트를 본다**:

| 대상 | 규칙 |
|---|---|
| manifest | UTF-8 JSON object · ≤ 256 KB · 중첩 ≤ 32단 |
| 이미지 | `\x89PNG\r\n\x1a\n` magic · 각 ≤ 2 MB |
| snapshot 전체 | ≤ 10 MB · asset ≤ 32개 |
| `assetId` | UUID 형식만. `../` · `/`가 애초에 통과하지 못한다 |
| 구조 field | `../` · `file://` · `http(s)://` · `data:` · `<script` 금지 |
| 참조 ↔ 업로드 | **manifest가 실제로 참조하는 것**과 업로드가 정확히 일치 |

`manifest_checksum`은 **서버가 계산한다** — client가 보낸 값을 믿지 않는다.
raw 저장 바이트 기준이고, parse/re-serialize한 값의 hash가 아니다.

#### contentType ↔ manifest 결합 (B-7F.1)

label만 바꿔 스티커를 거울로 등록할 수 없다. **완전한 Swift decoder를 Python에
다시 만들지 않는다** — 두 종류를 구분하기에 충분한 최소 구조만 본다:

| contentType | 요구 | 금지 |
|---|---|---|
| `mirror` | `id`(str) · `name`(str) · `style`(object) | 최상위 `design` |
| `sticker` | `id`(str) · `name`(str) · `design.style`(object) | — |

`design` 유무가 두 포맷을 가르는 지점이다(`StickerProject`에는 있고 `MyMirror`에는 없다).

**key 동일성을 요구하지 않는다** — client가 나중에 optional field를 더해도 옛
backend가 깨지지 않아야 한다. 모르는 key는 안전한 값이면 통과한다. 다만 `stickers` ·
`importedArtworks`가 **있는데 배열이 아니면** 우리가 아는 포맷이 아니라 거절한다.

API와 **service 양쪽**에서 확인한다. 한 경로에만 두면 다른 경로가 생기는 순간
구매자가 못 읽는 바이트가 팔리고, snapshot은 불변이라 되돌릴 수 없다.

#### 참조 asset은 manifest에서 뽑는다 (B-7F.1)

**client가 따로 보낸 목록을 authority로 쓰지 않는다.** B-7F에는 서버가 발명한
`assetIds` 필드가 있었는데 client는 그런 것을 적지 않는다 — 실제 client JSON을
넣어보니 *사진을 참조하는 거울이 asset 0개로 통과했다.* 구매자는 이미지가 비어
있는 템플릿을 받고, snapshot은 불변이라 고칠 수 없다. 그래서 구조에서 뽑는다:

| 종류 | 참조 위치 |
|---|---|
| 거울 | `stickers[].source.assetID` (`kind == "photo"`일 때만) · `importedArtworks[].assetID` |
| 스티커 | `finalAssetID`(optional) · `design.stickers[].source.assetID` · `design.importedArtworks[].assetID` |

**asset이 아닌 것**: `stickers[].id` · `importedArtworks[].id` · `strokes[].id` ·
`texts[].id`는 오브젝트 자기 식별자다(둘 다 UUID라서 구분하지 않으면 없는 PNG를
요구한다). `generationIDs`도 아니다 — AI 생성 기록 id이고 파일이 아니다.
`style.doodles[].symbol`은 SF Symbol 이름이다. client GC가 정확히 위 목록만
살려두는 것을 확인했다(`MyMirror.assetIDs(_:)` · `collectAssetGarbage(keeping:)`).

집합은 **정확히 같아야 한다.** 빠지면 조용히 깨지고, 남으면 manifest 어디서도
쓰지 않는 이미지를 몰래 넣은 것이다. 같은 assetID를 여러 오브젝트가 참조하는 것은
정상이고 업로드는 1개다 — **multipart로 같은 assetID를 두 번 보내면 거절한다**
(dict로 모으면 마지막 값이 조용히 이겨서, 업로더가 보낸 것과 다른 이미지가 팔린다).

#### 금지 문자열 검사는 산문을 비껴간다 (B-7F.1)

`TextObject.text`(100자 장식 문구)와 `name`은 사용자가 자유롭게 쓴다. 거울에
"https://insta.gr/me"라고 적는 것은 흔한 꾸미기인데, 전체 텍스트를 훑는 검사는
그 package를 통째로 거절했다. 그 문자열은 asset을 가리키지 않고 client는 `Text`로
그릴 뿐이다. 그래서 `PROSE_KEYS = {"name", "text"}`만 면제한다.

진짜 방어는 **참조 위치를 구조에서 뽑아 UUID만 허용하는 것**이다 — 경로·URL은
UUID 형식을 통과할 수 없으므로 asset 자리에서 경로 조작이 문자 수준에서 불가능하다.
면제는 참조 위치로 새지 않는다(같은 object 안에 `name`이 있어도 `assetID`는 검사한다).

깊게 중첩된 JSON은 `json.loads`가 `RecursionError`로 죽어 500이 됐다 — 이제
32단 상한으로 400이다.

전달 규칙:

- **미리보기는 공개다.** `published`만 — draft · unlisted · 없는 것 모두 404.
  로그인 없이 상점을 구경할 수 있어야 한다(Core Product Policy)
- **원본은 판매자 또는 구매자만.** 구경한 사람은 어떤 endpoint로도 원본을 못 받는다
- **원본은 `published`를 요구하지 않는다** — 판매자가 내려도 산 사람은 계속 받는다.
  이미 지불했고, 판매자의 나중 결정으로 회수되지 않는다
- 무료 상품도 소유권이 생기므로 규칙이 같다
- **signed URL을 만들지 않는다.** URL 자체가 credential이 되고 유출되면 회수할 수 없다.
  우리가 읽어서 흘려보낸다. 응답에 bucket · `gs://` · object key가 없다
- **조회가 counter를 올리지 않는다.** preview · template · asset을 10번 받아도
  `downloadCount` · `likeCount`는 그대로다. `downloadCount`는 소유권 획득뿐이다

bucket은 **`MARKETPLACE_ASSET_BUCKET`로 따로 받는다.**
**AI 결과 bucket(`AI_RESULT_BUCKET`)을 재사용하지 않는다** — 그쪽은 7일 lifecycle이고,
판매한 템플릿이 7일 뒤 사라지면 구매자가 산 것을 잃는다(환불로도 되돌릴 수 없다).
비어 있으면 **fail closed**다: 업로드·전달이 503이다. `AssetStorageUnavailable`을
`AssetNotFound`와 구분한다 — 404로 뭉개면 운영자가 설정 누락을 "데이터 없음"으로 오진한다.

**client production code는 아직 붙이지 않았다**(B-7G).

#### 판매자 자기 목록 (B-7G.1)

```
GET /users/me/marketplace/listings     (인증)
```

`draft` · `published` · `unlisted` **전부** 돌려준다. 공개 목록과 다른 것이다 —
그쪽은 `published`만 보여 주므로 판매자가 아직 안 올린 것과 내린 것을 다시 찾을
방법이 없었다. **client가 기억해 둔 listing id는 authority가 아니다** — 앱을 지우거나
기기를 바꾸면 관리가 끊긴다. 이 endpoint가 authority다.

`/users/me/...`뿐이다. 임의 userId로 남의 draft를 조회하는 경로를 만들지 않았고,
판매자 판단은 session의 user다(본문·query로 받지 않는다).

Firestore 질의는 `sellerUserId == currentUserId` **하나뿐**이다. `where` + `order_by`를
함께 걸면 composite index를 요구하게 되므로 정렬은 service가 한다
(`updatedAt` 내림차순, 같으면 id — 이미 있는 field이고 schema를 늘리지 않았다).

**응답은 판매자 전용 `ListingResponse`(`status` 포함)를 재사용한다.** 공개
`PublicListingResponse`는 그대로 8칸이고 거기에는 계속 `sellerUserId` ·
`snapshotId`가 없다. 판매자 DTO에도 내부 식별자는 넣지 않았다.

공개 목록과 달리 **malformed 문서를 걸러내지 않는다.** 공개는 거짓 정보를 보여
주지 않으려고 빼지만, 판매자에게는 자기 상품이 이상한 상태라는 것 자체가 보여야
한다 — 안 보이면 내릴 수도 없다.

#### 삭제 · 출처 연결 (Marketplace UX hardening)

```
DELETE /users/me/marketplace/listings/{listingId}      (인증, 판매자 본인)
```

**`deleted`는 끝 상태다.** `unlisted`(잠시 내림, 다시 올릴 수 있음)와 다른 것이다 —
사용자가 "삭제"를 골랐는데 되살아나는 상품처럼 행동하면 안 된다.
`publish`가 `status.is_terminal`을 보고 막는다.

**그런데 아무것도 실제로 지우지 않는다.** snapshot · GCS object · 소유권 · 원장 ·
`downloadCount` · `likeCount`가 전부 남는다 — **이미 산 사람이 계속 받아야** 하기
때문이다. 등록비도 돌려주지 않는다(경제 mutation 0). tombstone이다.

| | 삭제 후 |
|---|---|
| 공개 목록 · 상세 · 미리보기 | 사라진다 |
| 판매자 목록 | **남는다**(무엇을 지웠는지 알아야 한다) |
| 구매자 template · asset | **그대로 받는다** |
| 재등록 | **불가**(`InvalidTransition`) |

`unpublish`는 기존 문서와 호환 때문에 남겨 두지만 새 client UI는 쓰지 않는다.

#### sourceContentId — local 콘텐츠와의 연결

snapshot 문서에 **manifest top-level `id`**를 함께 적는다. `MyMirror.id` /
`StickerProject.id`이고 **새 식별자를 만들지 않는다.** 판매자가 "내 거울 → 판매 중"에서
자기 상품을 찾으려면 이 연결이 필요하다 — 제목으로 맞추면 같은 제목이 여러 개일 때 틀린다.

**검증을 통과한 manifest에서만** 뽑고, 경로가 될 수 있는 값(`/` · `..` · NUL)과
128자 초과는 거절한다. UUID를 요구하지 않는다 — 내장 템플릿에서 받은 거울은
`art-mint-flower`처럼 사람이 읽는 id다.

옛 snapshot에는 이 값이 없다. 그때는 **저장된 불변 manifest를 읽어** 응답에만 담는다 —
**production 문서를 다시 쓰지 않는다.** 알 수 없으면 빈 문자열이고 거짓 값을 만들지 않는다.

판매자 응답(`_SellerListingResponse`)에만 담는다. **공개 DTO는 8칸 그대로**이고
거기에는 계속 `sellerUserId` · `snapshotId` · `sourceContentId`가 없다.

#### 판매자 전용 미리보기 (B-7H hotfix)

```
GET /users/me/marketplace/listings/{listingId}/preview     (인증, 판매자 본인)
```

`draft` · `published` · `unlisted` **모두** 돌려준다. 판매자 관리 화면에 숫자만
보이고 생김새가 없어 어느 상품인지 알 수 없었다 — 아직 올리지 않은 것과 내린 것도
그림이 필요하다.

**공개 미리보기 정책은 그대로다.** `GET /marketplace/listings/{id}/preview`는
여전히 `published`만이다. 완화하면 사기 전에 원본 성격을 보여 주는 것이 된다.

판매자 본인만이고 남의 것이면 404다(존재 여부를 알려주지 않는다).
저장소 접근은 공개 미리보기와 **같은 key builder · 같은 reader**를 쓴다 —
새 storage 경로를 만들지 않았다. signed URL 없음, 응답에 bucket 경로 없음.
판매자 전용이라 `Cache-Control: private`다(공개 쪽은 `public`).

#### Marketplace bucket IAM (B-7G.1)

runtime SA의 bucket 권한을 `objectAdmin` → **`objectUser`**로 좁혔다.

`objectCreator` + `objectViewer`로는 **안 된다.** production path가 `delete()`를 쓴다
(`service.py`의 반쪽 업로드 정리). 이 bucket은 lifecycle rule이 0개이고 삭제 API도
없어서, delete 권한을 빼면 실패한 업로드가 남긴 orphan을 **영구히 지울 방법이 사라진다.**

`objectUser`는 `objectAdmin`보다 엄격히 좁다 — `setIamPolicy` · `getIamPolicy` ·
`setRetention` · `overrideUnlockedRetention`이 빠지고 create/get/delete는 남는다.
project 수준 storage role은 없다(`roles/datastore.user`만).

### 내장 템플릿 획득 통계 (catalog)

```
GET  /catalog/templates/stats?ids=a,b,c          (공개)
POST /catalog/templates/{templateId}/acquire     (인증, body 없음)
POST /catalog/templates/reconcile                (인증)
```

앱에 들어 있는 공식 템플릿 **32종**(artwork 24 + basic 8)이 몇 명에게 받아졌는지만
센다. **Marketplace와 다른 domain이다** — 저쪽은 소유권·조각·판매자가 있고 이쪽은
없다. 두 경로를 섞지 않는다(test가 고정).

**등록된 id만 받는다.** client가 보낸 문자열을 그대로 세면 아무 값이나 보내 공개
통계를 부풀릴 수 있다. 목록은 client `StoreCatalog`의 stable id를 그대로 옮겼고
**제목으로 검증하지 않는다**(제목은 바뀌고 겹친다).

`downloadCount` 의미는 Marketplace와 **같다**: 서로 다른 사용자의 최초 획득 수.
같은 사람의 재다운로드 · 구경은 **+0**.

| 컬렉션 | 문서 id | 뜻 |
|---|---|---|
| `ggumirror_catalog_acquisitions` | `sha256(len:userId|len:templateId)` | 누가 무엇을 처음 받았나 |
| `ggumirror_catalog_stats` | `templateId` | 공개 `downloadCount` |

**기록 생성과 카운터가 한 transaction**이다. 갈라지면 "받았는데 안 세어졌다"가 되고
나중에 고칠 방법이 없다. 문서 id가 `(userId, templateId)`이고 `create`로 쓰므로
동시 요청에도 +1이다(B-7E 소유권과 같은 규칙).

`reconcile`은 **멱등**이다 — 예전 버전에서 받은 것을 로그인 뒤 한 번 따라잡고,
몇 번을 불러도 수가 오르지 않는다. 그래서 실패한 획득의 복구 수단으로도 쓴다.

통계 조회는 **공개**다(상점 구경에 로그인 벽을 세우지 않는다). 카드마다 요청을
하나씩 만들지 않도록 한 번에 묻고, 기록이 없으면 **0**을 돌려준다 — 목록에서 빼면
화면이 자리를 비운다. **누가 받았는지는 공개 응답에 없다.**

### Admin Shard CLI (A-2) — 운영자 조정은 CLI 하나뿐

운영/테스트용 지급·회수는 **`scripts/admin_shards.py`** 로만 한다.
HTTP admin endpoint를 만들지 않는다 — 하나 생기면 그것이 곧 generic mutation 통로다.
앱에 hidden 버튼도 만들지 않는다.

```bash
python3 ggumirror-be/scripts/admin_shards.py \
  --user-id "<uid>" --delta 100 --note "AI sticker E2E"
```

결과가 애매하면(네트워크 끊김 등) **출력된 event id로 그대로 재실행**한다.
같은 event id는 딱 한 번만 반영된다:

```bash
python3 ggumirror-be/scripts/admin_shards.py \
  --user-id "<uid>" --delta 100 --note "AI sticker E2E" \
  --event-id "admin:..."
```

- **같은 event id에 다른 delta를 넣으면 거절한다.** event id는 금액이 아니라 *사건*을 가리킨다.
  `+10`으로 쓰인 event id에 `+100`을 넣으면 원장은 어차피 중복으로 무시하지만,
  운영자가 "Already applied"를 보고 `+100`이 반영됐다고 읽을 수 있다.
  그래서 mutation 전에 기존 entry를 **read-only로 읽어** 비교하고 fail closed한다.
  이건 **UX guard일 뿐**이고 exactly-once의 authority는 여전히 `ShardStore.apply`의 원자적
  `create()`다 — 열쇠도 B-3의 `idempotency_hash`를 그대로 쓴다(CLI가 새로 만들지 않는다)
- 조각은 **B-3 `ShardLedgerService.credit` / `debit`** 으로만 움직인다.
  wallet 문서를 직접 고치거나 ledger 문서를 손으로 쓰지 않는다.
  `tests/test_admin_shards.py`가 소스에서 우회 경로를 금지한다
- `reason=admin_adjustment`(이미 있던 값이다. 새 reason을 만들지 않았다)
- **"원복"도 지우지 않는다.** `+100` 뒤 `-20`은 반대 부호 줄이 하나 더 쌓이는 것이다
- project는 **`ggumirror-prod` 상수**다. 이 머신의 gcloud 기본 project는
  DailyOPIc(`opicmobile-45cd5`)이라 기본값을 신뢰하면 남의 production을 만진다.
  `firestore.Client(project=...)`에 명시적으로 넘기고,
  `--project`나 `GCP_PROJECT_ID` · `GOOGLE_CLOUD_PROJECT` · `GCLOUD_PROJECT` ·
  `CLOUDSDK_CORE_PROJECT`가 다른 곳을 가리키면 **fail closed**다
- 기본은 확인 질문이고 **기본값은 N**이다. tty가 아니면 묻지 않고 거절한다 —
  자동 실행은 `--yes`를 명시해야 한다. `--dry-run`은 계획만 보여준다
- 잔액이 음수가 되는 회수는 **원장이 거절한다**(`InsufficientShards`). CLI가 정책을 바꾸지 않는다
- `python3`로 불리면 repo venv로 한 번 re-exec한다(system python에는 google-cloud-firestore가 없다)

#### note는 Firestore에 남지 않는다

`ShardLedgerEntry`에 note/metadata 자리가 **없다**(`userId` · `delta` · `balanceAfter` ·
`reason` · `idempotencyKeyHash` · `createdAt` · `schemaVersion`가 전부다).
운영 편의 하나 때문에 경제 전체가 쓰는 schema를 넓히지 않았다 —
note에 PII가 섞여 들어올 자리를 만드는 일이기도 하다.

그래서 `--note`는 **필수 입력이되 운영자 화면에만** 남는다. durable audit은
원장이 이미 갖고 있는 것으로 한다: `reason=admin_adjustment` · `delta` ·
`balanceAfter` · `createdAt` · `idempotencyKeyHash`.
note를 원장에 남겨야 할 실제 요구가 생기면 그때 schema version을 올려서 넣는다.

> ⚠️ **Firestore Console에서 wallet balance를 직접 수정하지 마라.**
> 원장과 projection이 갈라지고, 그 뒤로는 어느 쪽이 진실인지 알 수 없다.

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

### SSV query는 두 곳에서 가린다 (영구 규칙)

`RedactSensitiveQuery`는 **우리 process 로그만** 가린다. Cloud Run이 따로 남기는
**platform request log**(`run.googleapis.com/requests`)의 `httpRequest.requestUrl`에는
query가 통째로 들어가고, 여기에는 Google signature와 reward context가 있다.
application filter로는 닿지 않는다 — canary로 실제 확인했다.

- `ggumirror-prod` `_Default` sink에 exclusion **`exclude-ggumirror-admob-ssv-request-log`**
  가 있고, `ggumirror-api`의 `/admob/rewarded/ssv` request log만 저장에서 제외한다
- **다른 경로 request log는 유지한다.** `/health` · `/auth/*` · `/users/me/*` 그대로
- 앱 stdout의 `?<redacted>` 줄과 `admob_ssv_*` 이벤트도 그대로 남는다
- query로 credential을 받는 endpoint를 새로 만들면 `SENSITIVE_QUERY_PATHS`와
  이 exclusion **양쪽**에 추가한다. 한쪽만 하면 반쪽만 가려진다
- exclusion 반영에는 몇 분 걸린다. 직후 canary는 아직 저장되는 것처럼 보인다

### AI 생성은 durable server resource다 (A-1B, 영구 규칙)

생성은 응답 한 번이 아니라 **서버가 소유하는 작업**이다(`ggumirror_ai_generations`).
A-1A의 두 구멍이 여기서 막힌다.

- **`generationId`는 `sha256(len:userId|len:requestId)`**이고 그것이 곧 문서 ID다.
  별도 index 문서를 두지 않는다 — transaction 안의 `create()`가
  "같은 requestId로 두 번 만들 수 없음"을 구조적으로 보장한다.
  조회해서 없으면 만드는 방식으로 바꾸지 않는다
- **client가 `requestId`(UUID)를 만든다.** 같은 값으로 다시 오면 provider를 부르지 않고
  차감도 하지 않는다. 응답을 잃었을 때의 유일한 재시도 수단이다
- **`prompt`는 만들 때만 필요하다.** 이어받을 때는 비워 보낸다 —
  응답을 잃은 client는 무엇을 적었는지 다시 보낼 수 없고 우리도 저장하지 않는다.
  그래서 `generate()`는 **프롬프트 검사보다 기존 작업 조회를 먼저** 한다
- 상태는 `pending → succeeded | refunded`, 환불까지 실패하면 `failed`로 남았다가
  다음 조회에서 되돌린다. **전이는 전부 조건부 transaction**이다

#### 성공 순서를 바꾸지 않는다

    provider 성공 → PNG 검증 → **storage upload** → status=succeeded → 그 다음 응답

upload가 status보다 먼저인 것이 복구의 근거다. process가 어디서 죽든:

| 죽은 지점 | 결과 |
|---|---|
| upload 전 | 결과 없음 → **환불** |
| upload 후 | 결과 있음 → **성공으로 확정**(조각 값을 받는다) |

#### late worker — Cloud Run timeout은 worker를 죽이지 않는다 (영구 규칙)

⚠️ **한때 "lease > Cloud Run timeout이므로 lease 만료 = process 사망"이라고 적혀 있었다.
틀렸다.** Cloud Run의 request timeout은 client 연결을 끊고 504를 돌려줄 뿐,
**container instance를 종료하지 않는다.** 그 요청을 처리하던 코드는 계속 돌 수 있고,
한참 뒤에 provider 성공을 받아 upload까지 마칠 수도 있다.

그래서 시간에 경제를 걸지 않는다. 안전은 **두 관문**이 만든다:

1. **terminal 권위** — `finish`는 terminal에서 나가는 전이를 거부한다.
   `refunded → succeeded`(공짜 결과) · `succeeded → refunded`(공짜 조각) 모두 불가능하다
2. **lease CAS** — 임차권을 뺏긴 worker는 쓰지 못한다

lease는 "지금 누가 들고 있다고 주장하는가"를 나타내는 **version token**일 뿐이다.

CAS에서 진 worker는 자기가 올린 object를 **직접 치운다**(orphan).
단, 지금 상태가 `succeeded`이고 바로 그 object를 가리키면 지우지 않는다 —
복구가 그것을 보고 성공으로 확정했을 수 있고, 지우면 조각을 쓴 사용자가 그림을 못 받는다.

#### 언제 환불하는가

| 상황 | 판단 |
|---|---|
| provider 거절/실패 · upload 실패 | **즉시 환불** (임차인이 직접 본 결정적 실패) |
| lease만 만료 (요청이 끊겼다) | **환불하지 않는다** — worker가 아직 돌 수 있다 |
| `RECOVERY_GRACE`(15분) 경과 | **recovery eligible**이 된다. 다음 요청이 결과 유무를 보고 정리 |

시간만으로 성급히 환불하지 않는다. `RECOVERY_GRACE`는 정상 worker의 최대 수명
(provider timeout 90초 + upload)을 한참 넘긴 값이다.

#### 결과의 권위는 Firestore terminal state다

`storage.exists`는 **증거일 뿐 결론이 아니다.** 확인한 뒤에 늦은 upload가 도착할 수 있다.
`GET /image`는 `status == succeeded`일 때만 내보낸다 —
object가 있어도 `refunded`/`failed`/`pending`이면 거절한다.
환불받은 사용자가 결과까지 받으면 공짜가 된다.

#### worker를 두지 않았다

Cloud Run은 요청 밖에서 CPU를 보장하지 않으므로(`min-instances=0`) background thread에
복구를 맡길 수 없고, Cloud Tasks / Pub/Sub / Scheduler는 새 infra · IAM · 실패 모드를 들여온다.
대신 **복구가 필요한 바로 그 순간** lazy하게 정리한다:
같은 requestId 재시도 · 상태 조회 · **앱 시작의 `GET /ai/stickers/config`**(사용자 scope sweep).
**시간이 지났다고 저절로 풀리지 않는다.** `RECOVERY_GRACE`가 지나면 그 작업은
*recovery eligible*이 될 뿐이고, 실제 정리는 그것을 건드리는 다음 요청에서 일어난다.
사용자가 돌아오지 않으면 조각은 계속 묶여 있다 — 자동 정리가 필요해지면 scheduler를 붙인다.

`stale_pending`은 등호 두 개짜리 질의라 composite index가 필요 없다 —
시각 비교는 읽은 뒤에 한다.

#### 결과 이미지 — private bucket + 우리 endpoint

- 꾸미러 전용 **private bucket**(`AI_RESULT_BUCKET`). public ACL을 쓰지 않는다.
  DailyOPIc bucket을 쓰지 않는다
- object 이름은 `ai/stickers/<generationId>.png`뿐이다 —
  프롬프트 · 이메일 · user id가 들어가지 않고, generationId도 hash다
- **signed URL을 만들지 않는다.** URL 자체가 credential이라 로그 · crash report · proxy에
  한 번 찍히면 누구나 받는다. 이미 Bearer session과 소유자 검증이 있으므로
  `GET /ai/stickers/{id}/image`로 스트리밍한다(`Cache-Control: no-store`)
- **남의 작업은 403이 아니라 404다** — 403으로 나누면 그 id가 존재한다는 사실이 새어 나간다
- 보관은 **7일**짜리 복구창이다. 사용자 저장소가 아니다(스티커의 주인은 기기).
  bucket lifecycle rule이 정리하고, 앱이 지우러 다니지 않는다.
  metadata는 감사/멱등 때문에 이미지보다 오래 남는다
- bucket이 없으면 **fail closed**다 — 복구할 수 없는 생성은 아예 하지 않는다

### AI 스티커 (A-1A) — 조각을 **쓰는** 첫 기능

프롬프트 한 줄 → 외부 image provider → 투명 PNG. 조각을 쓰는 첫 통로다.

- **차감이 provider 호출보다 먼저다.** provider 호출은 돈이 나가는 호출이라
  잔액 없이 부르면 안 된다. "먼저 조회해서 있으면 만든다"는 check-then-act라
  동시 요청 둘이 같은 6 조각으로 둘 다 만든다 — B-4에서 금지한 그 패턴이다.
  차감은 원장의 원자적 transaction이고 **잔액이 곧 상한이다**
- **하루 N회 counter를 만들지 않았다.** 조각이 이미 그 일을 한다.
  광고(B-5)처럼 무료로 받는 것에는 `PeriodQuota`가 필요하지만, 유료 소비에는 필요 없다
- provider가 실패하면 **환불한다.** 차감 줄을 지우지 않고
  `refund` + `ai_sticker_refund:<generation_id>`로 반대 부호 줄을 쌓는다(재시도해도 한 번)
- **환불 실패가 원래 오류를 덮지 않는다.** 사용자는 provider 실패를 보고,
  환불 실패는 사람이 볼 로그로 크게 남는다
- 고칠 수 있는 실패(빈 프롬프트 · 길이 초과 · provider 미설정)는 **차감 전에** 전부 거른다.
  환불할 일을 애초에 만들지 않는다
- **가격은 서버 상수 하나**(`DEFAULT_STICKER_PRICE`)이고 `GET /ai/stickers/config`로
  client에 내려간다. client가 가격을 알고 있지 않다 — 하드코딩하면 값을 바꿀 때 거짓말이 된다

#### provider — production model은 `gpt-image-2` + 기기 배경제거 (A-1B.2)

output contract는 **`valid PNG`**다. 투명을 요구하지 않는다.

| | |
|---|---|
| model | **`gpt-image-2`** (현재 production model) |
| size / quality / format | `1024x1024` / `low` / `png` |
| `background` | **보내지 않는다** |
| 투명 배경 | **기기가 만든다** — 기존 `PhotoStickerMaker`(Vision on-device) 재사용 |

capability probe로 확인한 사실:

- `gpt-image-1-mini`는 transparent를 **지원한다.** 하지만 **deprecated라 채택하지 않았다**
- **`gpt-image-2`는 transparent를 지원하지 않는다** —
  `400 / param=background / "Transparent background is not supported for this model."`

그래서 allowlist를 억지로 넓히지 않고 **계약을 바꿨다.** 꾸미러에는 이미 사진 배경제거가
있으므로 배경제거 API를 따로 붙이지 않는다 — 서버는 그림만 만들고 투명은 기기가 만든다.

`SUPPORTED_MODELS` 기준도 "투명을 지원하는가"에서 "우리 요청 모양으로 PNG를 주는 것이
확인됐는가"로 바뀌었다. 모르는 model이면 여전히 fail closed다.

#### provider — 추측하지 않고 확인한 것만

- **API key는 서버에만 있다**(`AI_IMAGE_API_KEY`). client bundle에 넣지 않는다.
  production에서는 **Secret Manager reference로 주입한다** — plain env value로 넣지 않는다
  (`--set-secrets=AI_IMAGE_API_KEY=ggumirror-openai-api-key:latest`).
  runtime SA에는 그 secret의 accessor 권한만 준다
- **OpenAI 계정은 꾸미러 전용 Project를 쓴다**(예: `ggumirror-production`).
  DailyOPIc의 기존 OpenAI key를 재사용하지 않는다 — project를 나눠야
  spend budget · rate limit · key 폐기를 따로 할 수 있다. 계정은 공유해도 project는 나눈다
- 비어 있으면 **fail closed**다. 서비스는 뜨고 다른 기능은 그대로이며,
  `config`가 `available=false`를 돌려줘 client가 CTA를 감춘다.
  **앱을 다시 내지 않고 서버 설정만으로 기능이 열린다** — xcconfig feature flag가 필요 없다
- model을 자유롭게 받지 않는다(`SUPPORTED_MODELS`). 모르는 model이면 `observed_model=`만
  남기고 fail closed다(B-5와 같은 진단)
- 응답이 PNG signature로 시작하지 않으면 거절한다 — 기기의 배경제거가 읽지 못한다.
  **alpha는 요구하지 않는다**
- httpx를 runtime dependency로 올리지 않았다. stdlib urllib이다(jwks · SSV verifier와 같다)

#### 저장하지 않는 것

- **프롬프트 원문.** Firestore에도 로그에도 남기지 않는다. 남기는 것은 `prompt_length`뿐이다
- **생성된 이미지.** 응답으로 한 번 흘려보내고 끝이다.
  Cloud Storage bucket이 없는 것은 아직 안 만들어서가 아니라 **필요가 없어서**다 —
  스티커의 주인은 기기다(`UserStickerAssets/<id>.png`)
- provider 응답 본문을 로그에 넣지 않는다 — 프롬프트가 그대로 되돌아온다

## 거울 보관 공간 (Mirror Capacity)

거울을 몇 개까지 담을 수 있는가. **client는 authority가 아니다.**

| | |
|---|---|
| 무료 기본 | `BASE_MIRROR_SLOTS = 5` |
| 확장 상품 | `mirror_slots_5` — **10조각 → +5칸** |
| 반복 구매 | 가능. 상한 없음 |
| 저장 위치 | `ggumirror_users/{userId}.purchasedMirrorSlots` |
| 구매 기록 | `ggumirror_mirror_capacity_operations/{hash(user, operationId)}` |

- **가격과 칸 수는 서버가 정한다.** request에는 `packId`와 `operationId` 자리밖에 없다 —
  `cost` · `slotDelta`를 실을 곳이 없다. 모르는 pack은 404다
- 예전 사용자 문서에는 field가 없다 → **0으로 본다.** migration을 돌리지 않는다
- 새 collection을 하나만 만들었다(구매 기록). 칸 수는 이미 있는 user 문서에 얹는다 —
  그 문서는 생성 때 한 번만 `set`되고 뒤에 덮어쓰이지 않아서 안전하다.
  지갑과 섞지 않는다(지갑에는 자기 collection이 있다)
- **몇 개를 쓰고 있는지는 서버가 모른다.** 그건 기기의 사실(`MirrorLibrary` 개수)이고,
  서버는 "몇 칸까지 담을 수 있는가"만 안다

### operationId — 반복 구매와 재시도를 가르는 것

`user + packId`를 멱등 열쇠로 쓰면 **두 번째 확장을 영원히 못 산다.**
반대로 재시도마다 새 열쇠를 만들면 응답을 잃었을 때 **조각이 두 번 빠진다.**

그래서 **의도 하나 = `operationId` 하나**가 authority다. client가 UUID를 만들고,
같은 구매를 재시도할 때는 같은 값을, 새 구매에는 새 값을 보낸다.
열쇠는 `sha256(len:user_id|len:operation_id)`라 남의 기록에 닿을 수 없다.

### 하나의 transaction

`FirestoreCapacityStore.purchase`가 한 commit 안에서:

```
1. 구매 기록 읽기(멱등)      ← 우리 문서를 전부 먼저 읽는다
2. 칸 수 읽기
3. 조각 -10                  ← apply_in_transaction (B-7B)
4. purchasedMirrorSlots +5   ← merge, 다른 field를 덮지 않는다
5. 원장 한 줄
6. 구매 기록 create
```

- **읽기가 전부 쓰기보다 앞선다.** Firestore transaction은 쓰기 뒤 읽기를 허용하지 않는다
- `scoped = shards.context(transaction)`을 **attempt마다** 새로 만든다(B-7B.1).
  안 그러면 ABORTED 재시도가 `WalletAlreadyChanged`로 잘못 거절된다
- 잔액이 모자라면 `InsufficientShards`가 올라와 **transaction 전체가 취소된다** —
  칸도 기록도 원장도 남지 않는다. 409로 나간다
- 새 지갑 시스템을 만들지 않았다. 조각 이동은 전부 기존 `ShardLedgerService`가 한다

원장 이유는 `mirror_capacity_purchase`다 — 상점 구매와 **다른 사건**이라 나눈다.

`tests/test_mirror_capacity.py`가 위 전부를 고정한다. 실제 SDK wrapper +
`AbortingCapacityTransaction`으로 **진짜 ABORTED 재시도 loop**를 돌려
-10과 +5가 시도마다 쌓이지 않는 것을 확인한다.

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
| Secret Manager | 실제 secret이 생길 때 `ggumirror-prod`에만. **`AI_IMAGE_API_KEY`가 첫 후보다** — 지금은 Cloud Run env var라 project viewer가 읽을 수 있다 |
| Firebase | 실제 요구(FCM · App Check 등)가 생길 때만, `ggumirror-prod` 기반 꾸미러 전용 설정으로 |
| RevenueCat | 꾸미러 전용 project / app / product. **구매 성공을 잔액 권위로 쓰지 않는다** — server ledger가 권위다 |

## Business Model Roadmap

B-3 원장 위에 얹는다. 전부 server가 지급 / 차감한다.

| Phase | 내용 | reason |
|---|---|---|
| B-4 ✅ | 출석 — 하루 1개 (Asia/Seoul day) | `daily_attendance` |
| B-5 ✅ | AdMob rewarded — 1개, 하루 5회, **SSV 필수** | `rewarded_ad` |
| A-1A ✅ | AI 스티커 — **−6개**, 실패하면 환불 | `ai_sticker` · `refund` |
| B-6 ✅ | 조각 IAP — 10 / 50 / 100, 환불 회수 · 되돌리기 | `iap_purchase` · `iap_refund` · `iap_refund_reversed` |
| B-7 | 꾸미러 Pass — ₩4,900 월 / ₩39,000 년 | 정책 확정 후 |
| B-7 | 마켓 — 등록 20 조각, 조각 구매 거울은 영구 소유 | `mirror_publish_fee` · `mirror_purchase` · `mirror_sale` |

## Next Phase

**B-6 — 조각 IAP.** 원장 · idempotency · 전용 endpoint · 원자적 상한이 모두 갖춰졌다.
B-6이 더할 것은 App Store 영수증 검증이고, transaction id를 external event id로 쓴다.

A-1A로 조각을 **쓰는** 곳이 처음 생겼으므로 B-6이 전보다 급해졌다 —
지금은 버는 길이 출석(하루 1) · 광고(하루 5)뿐이고 AI 스티커 한 장이 6이다.

그 전에 **B-5 production 설정**이 남아 있다 — AdMob app / rewarded ad unit 생성,
SSV callback URL 등록, `ADMOB_SSV_EXPECTED_AD_UNIT` · `ADMOB_REWARD_ITEM` 배포.
그때까지 SSV endpoint는 살아 있되 fail closed다.

Cloud Run 자동 배포 workflow는 GCP project · service account ·
Workload Identity가 확정된 뒤에 만든다.
