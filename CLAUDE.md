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

## Current Implementation (Phase B-6B 완료)

FastAPI + Apple token 검증 + Firestore User / Session + Bearer auth +
server-authoritative 조각 원장 + 하루 한 번 출석 + AdMob rewarded SSV + AI 스티커 생성 +
조각 IAP(Apple 공식 library로 JWS 검증. client/UI는 B-6C).

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
app/shards/service.py  ShardLedgerService
app/shards/attendance.py  KST 날짜 규칙 + 출석 지급
app/core/config.py   환경변수 + logging
app/api/iap.py       POST /users/me/iap/shards (body는 signedTransaction 하나)
app/iap/models.py    catalog(10/50/100) · 전역 claim id · appAccountToken 대조
app/iap/verifier.py  검증 seam (protocol + fail-closed Unconfigured)
app/iap/apple_verifier.py  Apple 공식 SignedDataVerifier wrapper (environment별)
app/iap/certs/       Apple root certificate (DER, 공개값)
app/iap/service.py   검증 → bundle/type/env/catalog/token → 원장
scripts/admin_shards.py  운영자 조각 지급/회수 CLI (B-3 원장 재사용)
tests/               pytest (Apple · Firestore 호출 없음)
```

API는 health / auth / users / admob / ai / iap뿐이다. store · listing · marketplace는 없다.

조각을 움직이는 통로는 **넷뿐**이고 전부 client가 값을 정할 수 없다:

| 통로 | 무엇이 인증하나 | 방향 |
|---|---|---|
| `POST /users/me/attendance` | Bearer session (body 없음) | +1 |
| `GET /admob/rewarded/ssv` | **Google ECDSA 서명** (Bearer 없음) | +1 |
| `POST /ai/stickers` | Bearer session (body는 프롬프트뿐) | **−6** |
| `POST /users/me/iap/shards` | Bearer session + **Apple 서명 JWS** | +10 / +50 / +100 |

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

#### client 복구 계약 (B-6C)

1. 앱 시작 즉시 `Transaction.updates` listener를 띄운다
2. 인증된 서버 세션이 준비된 뒤 `Transaction.unfinished`를 sweep한다
3. `VerificationResult.verified`만 backend에 제출한다
4. backend가 지급을 확정한 뒤에만 `transaction.finish()`

`Transaction.currentEntitlements`는 **consumable 복구에 쓰지 않는다** — 소모품은 거기에 남지 않는다.

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
| B-6 | 조각 IAP — 10 / 50 / 100 | `iap_purchase` |
| B-7 | 꾸미러 Pass — ₩4,900 월 / ₩39,000 년 | 정책 확정 후 |
| B-8 | 마켓 — 등록 20 조각, 조각 구매 거울은 영구 소유 | `mirror_publish_fee` · `mirror_purchase` · `mirror_sale` |

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
