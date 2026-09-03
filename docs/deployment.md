# 배포

`.github/workflows/backend.yml` 하나가 CI와 production 배포를 모두 한다.

## Branch 전략

```
feature/*  ──PR──▶  dev            CI만
dev        ──PR──▶  main           CI만
                    main (merge)   CI ▶ image build/push ▶ Cloud Run 배포 ▶ health smoke
```

| branch | 뜻 | 배포 |
|---|---|---|
| `feature/*` | 작업 | 없음 |
| `dev` | 개발 통합 | **없다** |
| `main` | production source of truth | **merge = release** |

**main에 merge하는 것이 곧 production release다.** 그래서 별도 승인 게이트를 두지 않았다 —
merge 자체가 승인이다.

hotfix:

```
main ──▶ hotfix/* ──PR──▶ main ──▶ 배포
그 다음 main ──▶ dev 로 되돌려 맞춘다 (안 하면 dev가 production보다 뒤처진다)
```

일반 feature는 main으로 직접 넣지 않는다. `release-source` job이 막는다.

## 어떤 event가 무엇을 하는가

| event | test | release-source | deploy |
|---|---|---|---|
| `feature/*` push (PR 없음) | — | — | — |
| PR → `dev` | ✅ | — | **없음** |
| push `dev` (merge) | ✅ | — | **없음** |
| PR `dev` → `main` | ✅ | ✅ | **없음** |
| PR `feature/*` → `main` | ✅ | ❌ **실패** | **없음** |
| push `main` (merge) | ✅ | — | ✅ |

`deploy` job의 조건은 `github.event_name == 'push' && github.ref == 'refs/heads/main'`이다.
**pull_request event에서는 어떤 경우에도 배포되지 않는다.**

feature branch에 PR 없이 push하면 workflow가 아예 돌지 않는다. PR을 열면 그때부터
매 push마다 test가 돈다 — 리뷰 대상이 되는 순간부터 CI가 붙는다.

## GitHub → GCP 인증

**Workload Identity Federation + GitHub OIDC.** service account JSON key를 만들지 않고
GitHub Secret에 넣지도 않는다. deploy job만 `id-token: write`를 갖는다.

runtime SA와 deployer SA를 나눈다:

| | |
|---|---|
| runtime | `ggumirror-api-runtime@ggumirror-prod.iam.gserviceaccount.com` — 서비스가 실행되는 신원 |
| deployer | `ggumirror-github-deployer@ggumirror-prod.iam.gserviceaccount.com` — CI가 배포하는 신원 |

deployer에는 배포에 필요한 것만 준다. Firestore · Storage 권한이 **없다** —
CI가 production 데이터에 닿을 이유가 없다.

## 필요한 GitHub Variables

민감하지 않은 값이라 **Secret이 아니라 Variable**이다. WIF provider resource name과
service account email은 그 자체로 아무것도 인증하지 못한다.

| Variable | 값 |
|---|---|
| `GCP_WORKLOAD_IDENTITY_PROVIDER` | `projects/764151610434/locations/global/workloadIdentityPools/github/providers/ggumirror-be` |
| `GCP_DEPLOY_SERVICE_ACCOUNT` | `ggumirror-github-deployer@ggumirror-prod.iam.gserviceaccount.com` |

`GCP_PROJECT_ID` · `GCP_REGION` · `CLOUD_RUN_SERVICE` · `ARTIFACT_REGISTRY_REPO`는
**workflow에 박혀 있다.** variable로 빼면 variable 한 줄을 고쳐 다른 production project로
배포할 수 있다 — 이 머신의 gcloud 기본 project가 DailyOPIc(`opicmobile-45cd5`)이라
그 사고는 이론이 아니다. 배포 대상을 바꾸는 것은 review를 지나는 code change여야 한다.

이 넷을 variable로 **두어도 된다.** 두면 `Guard the deploy target` step이 workflow에
박힌 값과 대조하고, 다르면 배포하지 않는다(fail closed). 없으면 그냥 넘어간다.

**GitHub Secret은 하나도 필요 없다.**

## 일회성 설정 (아직 안 되어 있다)

2026-09-03 audit 결과: `ggumirror-prod`에 workload identity pool **없음**,
deployer service account **없음**, repo variable · secret · environment **없음**.
아래를 한 번 실행해야 배포가 동작한다.

> 모든 명령에 `--project=ggumirror-prod`가 있다. **local/기본 gcloud project를 믿지 않는다.**
> 이 머신의 기본값은 DailyOPIc다.

```bash
export PROJECT=ggumirror-prod
export PROJECT_NUMBER=764151610434
export REPO=mark77234/ggumirror-be
```

### 1. API 켜기

```bash
gcloud services enable iamcredentials.googleapis.com sts.googleapis.com --project=$PROJECT
```

### 2. 배포 전용 service account

```bash
gcloud iam service-accounts create ggumirror-github-deployer --project=$PROJECT --display-name="ggumirror GitHub deployer"
```

### 3. 최소 권한

```bash
gcloud projects add-iam-policy-binding $PROJECT --member="serviceAccount:ggumirror-github-deployer@$PROJECT.iam.gserviceaccount.com" --role="roles/run.developer" --condition=None
```

```bash
gcloud artifacts repositories add-iam-policy-binding ggumirror --project=$PROJECT --location=asia-northeast3 --member="serviceAccount:ggumirror-github-deployer@$PROJECT.iam.gserviceaccount.com" --role="roles/artifactregistry.writer"
```

Cloud Run revision은 runtime SA로 실행되므로 deployer가 그 SA를 **impersonate**할 수 있어야
한다. project 전체가 아니라 **그 SA 하나에만** 준다:

```bash
gcloud iam service-accounts add-iam-policy-binding ggumirror-api-runtime@$PROJECT.iam.gserviceaccount.com --project=$PROJECT --member="serviceAccount:ggumirror-github-deployer@$PROJECT.iam.gserviceaccount.com" --role="roles/iam.serviceAccountUser"
```

Artifact Registry 권한도 repository 하나로 좁힌다(project 수준 `artifactregistry.writer`를
주지 않는다). Firestore · Storage role은 **주지 않는다.**

### 4. Workload Identity Pool + provider

```bash
gcloud iam workload-identity-pools create github --project=$PROJECT --location=global --display-name="GitHub Actions"
```

```bash
gcloud iam workload-identity-pools providers create-oidc ggumirror-be --project=$PROJECT --location=global --workload-identity-pool=github --display-name="ggumirror-be main" --issuer-uri="https://token.actions.githubusercontent.com" --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref" --attribute-condition="assertion.repository == '$REPO' && assertion.ref == 'refs/heads/main'"
```

`attribute-condition`이 **repository와 branch를 둘 다 묶는다.** 다른 repo의 workflow도,
같은 repo의 `main`이 아닌 branch도 이 provider로 token을 받지 못한다 —
workflow의 `if` 조건이 실수로 느슨해져도 GCP 쪽에서 한 번 더 막힌다.

### 5. 그 provider에서 온 신원만 deployer를 빌릴 수 있게

```bash
gcloud iam service-accounts add-iam-policy-binding ggumirror-github-deployer@$PROJECT.iam.gserviceaccount.com --project=$PROJECT --role="roles/iam.workloadIdentityUser" --member="principalSet://iam.googleapis.com/projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/github/attribute.repository/$REPO"
```

### 6. GitHub Variables

```bash
gh variable set GCP_WORKLOAD_IDENTITY_PROVIDER --repo $REPO --body "projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/github/providers/ggumirror-be"
```

```bash
gh variable set GCP_DEPLOY_SERVICE_ACCOUNT --repo $REPO --body "ggumirror-github-deployer@$PROJECT.iam.gserviceaccount.com"
```

### 7. GitHub Environment `production`

deploy job이 `environment: production`을 쓴다. 없으면 첫 배포 때 자동으로 만들어지지만,
미리 만들어 두면 deployment history와 production 전용 variable을 여기에 모을 수 있다.

이 branch 전략에서는 **manual reviewer gate를 걸지 않는다** — main merge가 승인이다.

## 배포가 실제로 하는 일

1. **대상 확인** — variable이 workflow의 project·region·service와 다르면 중단
2. **WIF 인증** → gcloud
3. **인증된 project 확인** — `ggumirror-prod`가 아니면 중단
4. **지금 traffic을 받는 revision 기록** (rollback 대상)
5. **배포 전 config snapshot**
6. **image build/push** — `…/ggumirror-api:${GITHUB_SHA}`
7. **`gcloud run deploy --image` 하나만**
8. **새 revision이 traffic을 받는지 확인**
9. **image만 바뀌었는지 검증**
10. **`GET /health` smoke** — 최대 90초(3초 간격 30회)
11. 실패하면 **traffic rollback**

### image tag

`asia-northeast3-docker.pkg.dev/ggumirror-prod/ggumirror/ggumirror-api:<commit SHA>`

full commit SHA다. `latest`를 production authority로 쓰지 않는다 — 어떤 revision이 어떤
commit인지 revision 목록에서 바로 보여야 한다. GitHub runner는 amd64라
`--platform linux/amd64`가 필요 없다(Apple Silicon에서 수동 배포할 때는 필요하다).

### ⚠️ `--set-env-vars` · `--set-secrets`를 쓰지 않는다

production service에는 이미 env var **17개**와 Secret Manager reference **2개**
(`AI_IMAGE_API_KEY` · `APNS_PRIVATE_KEY`)가 붙어 있다. 그 flag는 **선언하지 않은 것을
지운다.** workflow가 하는 일은 새 image를 올리는 것 하나다.

`Verify only the image changed` step이 배포 전후를 비교해서 runtime SA · env(이름과
출처) · min/max instances · concurrency · timeout · ingress · resources · VPC 설정이
그대로인지 확인한다. 하나라도 달라지면 실패하고 rollback한다. **값은 로그에 남기지
않는다** — 어떤 field가 달라졌는지만 말한다.

> README의 "배포 (수동)" 명령은 `--set-env-vars`로 5개만 선언한다.
> **지금 그대로 실행하면 env var 12개와 secret reference 2개가 사라진다.**
> 수동 배포가 필요하면 `--image`만 바꾼다.

### smoke test

URL을 workflow에 하드코딩하지 않고 Cloud Run에게 묻는다
(`--format='value(status.url)'`). `GET /health`가 200 + `{"status":"ok"}`를 줄 때까지
3초 간격으로 최대 30번. 끝까지 안 되면 job은 **성공이 아니다.**

### rollback

smoke(또는 traffic·config 검증)가 실패하면 배포 전에 기록해 둔 revision으로
traffic 100%를 되돌린다.

- **Cloud Run traffic만 되돌린다.** production data rollback은 하지 않는다 —
  이 단계에 Firestore가 없다
- rollback이 성공하면 traffic이 그 revision에 **고정**된다. 다음 배포의
  `Make sure the new revision serves the traffic` step이 자동으로 푼다
- rollback 자체가 실패하면 로그에 `ROLLBACK FAILED`와 수동 명령을 크게 남긴다

### concurrency

`group: ggumirror-production`, `cancel-in-progress: false`.
production 배포는 한 번에 하나이고, **도중에 취소하지 않는다** —
새 revision을 만들어 둔 채 traffic·smoke·rollback이 끊긴 상태로 남는다.

## Branch protection (GitHub UI에서 직접)

코드로 설정할 수 없다. Settings → Branches에서 rule을 만든다.

### `dev`

- PR 권장 (강제하지 않아도 된다 — solo project다)
- **Require status checks to pass**: `test`

### `main`

- **Require a pull request before merging** (direct push 금지)
- **Require status checks to pass**:
  - `test`
  - `release-source`
- approval 수는 강제하지 않아도 된다 (solo project)

**`release-source`를 required check로 걸지 않으면 guard가 실제로 막지 못한다** —
job은 빨갛게 실패하지만 merge 버튼은 살아 있다.

required status check 이름은 **job id**다(workflow 이름이 아니다):
`test` · `release-source`. 예전 workflow의 check 이름은 `pytest`였으므로,
그것이 required로 걸려 있으면 지워야 한다 — 없는 check를 기다리며 영원히 pending이 된다.

## 로컬에서 CI를 재현하기

CI 실패의 대부분은 "개발 기기에는 있는데 runner에는 없는 것" 때문이다.
가장 흔한 것이 **Google ADC**다. `tests/conftest.py`의 `no_google_credentials` guard가
그 차이를 없애 두었지만, 환경 자체를 재현하려면:

```bash
env -i PATH=/usr/bin:/bin HOME=$(mktemp -d) GCE_METADATA_HOST=localhost:1 ./.venv/bin/python -m pytest
```

빈 `HOME`이라 `~/.config/gcloud`가 없고, metadata server도 닿지 않는다 —
GitHub Actions runner와 같은 상태다.
