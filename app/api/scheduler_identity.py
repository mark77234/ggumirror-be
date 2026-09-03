"""정기 발송 호출자 확인 (Phase J).

**Cloud Run IAM이 이 경로를 지켜 주지 않는다.** `ggumirror-api`는 `allUsers`에게
`roles/run.invoker`가 열려 있는 공개 service다 — 앱이 로그인 없이 상점을 보여 줘야
하기 때문이다. 그래서 "Cloud Scheduler만 부를 수 있다"를 **앱이 직접** 확인한다.

확인하는 것:

1. Google이 서명했는가 (signature · issuer)
2. 우리를 향한 token인가 (audience)
3. 아직 유효한가 (exp)
4. **우리가 정한 그 scheduler 계정인가** (email + email_verified)

4번이 핵심이다. 1~3만 보면 Google 계정을 가진 누구나 자기 token으로 우리 사용자
전체에게 push를 쏠 수 있다.

설정이 없으면 **막는다.** 예전에는 설정이 없을 때 통과시켰는데, 그건 공개
service에서 문을 열어 두는 것과 같았다.
"""

from __future__ import annotations

import logging
from typing import Protocol

logger = logging.getLogger(__name__)

GOOGLE_ISSUERS = frozenset({"https://accounts.google.com", "accounts.google.com"})


class SchedulerIdentityError(Exception):
    """부른 쪽을 신뢰할 수 없다. **왜인지는 호출자에게 알려주지 않는다.**"""


class IDTokenVerifier(Protocol):
    """Google ID token 하나를 검증하고 claim을 돌려준다.

    이 이음매 덕분에 test가 실제 Google을 부르지 않는다.
    """

    def verify(self, token: str, audience: str) -> dict: ...


class GoogleIDTokenVerifier:
    """실제 Google 검증. 서명 · issuer · audience · exp를 라이브러리가 본다.

    직접 JWKS를 받아 서명을 맞추지 않는다 — 그걸 우리가 지면 틀렸을 때
    그대로 인증 우회가 된다.
    """

    def verify(self, token: str, audience: str) -> dict:
        from google.auth.transport import requests as transport
        from google.oauth2 import id_token

        return id_token.verify_oauth2_token(token, transport.Request(), audience)


def verify_scheduler(
    token: str | None,
    *,
    expected_service_account: str,
    expected_audience: str,
    verifier: IDTokenVerifier,
) -> dict:
    """이 요청이 우리 scheduler에서 온 것인가.

    통과하면 claim을 돌려주고, 아니면 `SchedulerIdentityError`다.
    **token 값도 claim도 로그에 남기지 않는다.**
    """
    # 설정이 없으면 막는다. 공개 service에서 "설정 안 했으니 통과"는 문을 여는 것이다.
    if not expected_service_account or not expected_audience:
        logger.error("scheduler_identity_not_configured")
        raise SchedulerIdentityError("not configured")

    if not token:
        raise SchedulerIdentityError("missing token")

    try:
        claims = verifier.verify(token, expected_audience)
    except Exception as error:  # noqa: BLE001 — 라이브러리가 무엇을 던지든 거절이다
        logger.warning("scheduler_identity_rejected reason=%s", type(error).__name__)
        raise SchedulerIdentityError("invalid token") from error

    if claims.get("iss") not in GOOGLE_ISSUERS:
        logger.warning("scheduler_identity_rejected reason=issuer")
        raise SchedulerIdentityError("issuer")

    # **여기가 핵심이다.** 서명만 맞으면 Google 계정을 가진 누구나 통과한다.
    if claims.get("email") != expected_service_account:
        logger.warning("scheduler_identity_rejected reason=identity")
        raise SchedulerIdentityError("identity")

    if not claims.get("email_verified", False):
        logger.warning("scheduler_identity_rejected reason=email_unverified")
        raise SchedulerIdentityError("email not verified")

    return claims
