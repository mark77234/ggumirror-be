"""계정 삭제.

**진짜 삭제다** — 비활성화도, 로그아웃도, 이메일 문의 안내도 아니다.

다만 지우면 안 되는 것이 있다. 이 사람이 팔았고 **다른 사람이 이미 산** 상품이다.
산 사람의 권리는 판 사람이 떠난다고 사라지지 않는다. 그래서 규칙이 셋이다:

1. 개인 데이터는 지운다 (프로필 · 지갑 · 원장 · 소유권 · 좋아요 · 세션)
2. 다른 사람의 권리에 필요한 것은 남긴다 (snapshot · GCS · 구매자의 소유권)
3. 지울 수 없지만 개인과 이어져 있는 것은 **연결만 끊는다** (IAP claim)

세 번째가 특히 중요하다. Apple 결제 claim을 지우면 **같은 결제를 다시 제출해
조각을 또 받을 수 있다** — 계정을 지웠다 다시 만들면 되는 셈이다. 그래서 claim
문서는 남기고 주인 표시만 지운다. 그러면 재제출은 "남의 결제"로 막힌다.

여러 collection에 걸쳐 있어 한 transaction으로 끝낼 수 없다. 그래서 **여러 번
실행해도 안전하게** 만든다 — 중간에 실패하면 그대로 다시 부르면 된다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from google.api_core import exceptions as gcp_exceptions
from google.cloud import firestore

from app.auth.models import utcnow
from app.auth.store import StoreUnavailable

logger = logging.getLogger(__name__)

#: 떠난 사람 자리에 남기는 표시. **어떤 실제 user id와도 같을 수 없다**(UUID가 아니다).
#: 이 값이 주인인 claim은 누구의 재제출도 받아 주지 않는다.
DELETED_OWNER = "deleted-account"

#: 한 번에 지우는 문서 수. Firestore batch 상한(500)보다 넉넉히 아래.
_PAGE = 200


class AccountDeleting(Protocol):
    """계정 삭제 한 가지. 부르는 쪽은 **인증된 본인의 id만** 넘긴다."""

    def delete(self, user_id: str) -> "AccountDeletionResult": ...


@dataclass(frozen=True)
class AccountDeletionResult:
    """무엇을 했는지. 숫자는 로그용이고 개인 정보를 담지 않는다."""

    listings_hidden: int
    likes_removed: int
    documents_deleted: int
    claims_anonymized: int


class AccountDeletionService:
    """`DELETE /users/me/account`가 부르는 것.

    **본인만 지울 수 있다** — user id를 인자로 받는 일반 삭제 도구가 아니다.
    부르는 쪽이 인증된 사용자 본인의 id만 넘긴다.
    """

    def __init__(self, db: firestore.Client, collections: dict[str, str]) -> None:
        self._db = db
        self._c = collections

    # MARK: - 공개

    def delete(self, user_id: str) -> AccountDeletionResult:
        """여러 번 불러도 안전하다. 이미 지워진 것은 조용히 넘어간다."""
        try:
            listings = self._retire_listings(user_id)
            likes = self._remove_likes(user_id)
            claims = self._anonymize_claims(user_id)
            deleted = self._delete_personal_documents(user_id)
        except gcp_exceptions.GoogleAPIError as error:
            raise StoreUnavailable("account deletion failed") from error

        # **user id를 로그에 남기지 않는다.** 무엇을 했는지만 남는다.
        logger.info(
            "account_deleted listings=%d likes=%d documents=%d claims=%d",
            listings, likes, deleted, claims,
        )
        return AccountDeletionResult(
            listings_hidden=listings, likes_removed=likes,
            documents_deleted=deleted, claims_anonymized=claims,
        )

    # MARK: - 상점

    def _retire_listings(self, user_id: str) -> int:
        """판매 중이던 상품을 상점에서 내린다. **지우지는 않는다.**

        기존 삭제 계약을 그대로 쓴다 — 끝 상태이고, 등록비는 돌려주지 않으며,
        snapshot과 GCS object는 남는다. 이미 산 사람이 계속 받아야 하기 때문이다.
        판매자 표시만 떼어 낸다.
        """
        query = self._db.collection(self._c["listings"]).where("sellerUserId", "==", user_id)
        count = 0
        for snapshot in query.stream():
            data = snapshot.to_dict() or {}
            if data.get("status") == "deleted" and data.get("sellerUserId") == DELETED_OWNER:
                continue  # 이미 처리했다. 다시 실행해도 안전하다.
            snapshot.reference.update({
                "status": "deleted",
                # 판매자를 익명으로. 이름 조회가 자연히 실패해 화면에서 사라진다.
                "sellerUserId": DELETED_OWNER,
                "deletionReason": "account_deleted",
                "updatedAt": utcnow(),
            })
            count += 1
        return count

    def _remove_likes(self, user_id: str) -> int:
        """좋아요를 거둔다. **`likeCount`를 정확히 줄인다.**

        세어 둔 값이라 그냥 문서만 지우면 숫자가 영영 부풀어 있는다.
        listing마다 transaction으로 처리해 동시에 들어온 좋아요와 겹치지 않는다.
        """
        query = self._db.collection(self._c["likes"]).where("userId", "==", user_id)
        count = 0
        for snapshot in query.stream():
            listing_id = (snapshot.to_dict() or {}).get("listingId")
            if self._remove_one_like(snapshot.reference, listing_id):
                count += 1
        return count

    def _remove_one_like(self, like_ref, listing_id: str | None) -> bool:
        listing_ref = (
            self._db.collection(self._c["listings"]).document(listing_id) if listing_id else None
        )

        @firestore.transactional
        def run(transaction: firestore.Transaction) -> bool:
            like = like_ref.get(transaction=transaction)
            if not like.exists:
                return False  # 이미 지웠다.
            current = None
            if listing_ref is not None:
                listing = listing_ref.get(transaction=transaction)
                current = (listing.to_dict() or {}).get("likeCount") if listing.exists else None
            transaction.delete(like_ref)
            if listing_ref is not None and isinstance(current, int):
                # 음수로 내려가지 않게 한다 — 세어 둔 값이 어긋난 적이 있어도 복구된다.
                transaction.update(listing_ref, {"likeCount": max(0, current - 1)})
            return True

        return run(self._db.transaction())

    # MARK: - 결제 흔적

    def _anonymize_claims(self, user_id: str) -> int:
        """Apple 결제 claim의 **주인 표시만** 지운다.

        문서를 지우면 같은 결제를 다시 제출해 조각을 또 받을 수 있다 —
        계정을 지웠다 다시 만들면 되는 셈이다. 문서는 남기고 주인만 바꾸면
        재제출이 "남의 결제"로 막힌다.
        """
        count = 0
        for name in ("iap_transactions", "iap_refunds", "capacity_operations"):
            collection = self._c.get(name)
            if collection is None:
                continue
            for snapshot in self._db.collection(collection).where("userId", "==", user_id).stream():
                snapshot.reference.update({"userId": DELETED_OWNER})
                count += 1
        return count

    # MARK: - 개인 데이터

    def _delete_personal_documents(self, user_id: str) -> int:
        """이 사람의 것만 지운다. 남의 문서는 건드리지 않는다."""
        count = 0

        # id가 곧 user id인 문서.
        # id가 곧 user id인 문서에 알림 설정도 들어간다.
        #
        # **없는 이름은 건너뛴다.** 아래 field 기반 목록과 같은 규칙이다 — 부르는
        # 쪽이 아직 그 collection을 모르면(옛 구성 · test fixture) 삭제 전체가
        # 죽는 것이 가장 나쁘다. 지울 것이 없으면 지울 것이 없는 것이다.
        for name in ("users", "wallets", "notification_preferences"):
            collection = self._c.get(name)
            if collection is None:
                continue
            reference = self._db.collection(collection).document(user_id)
            if reference.get().exists:
                reference.delete()
                count += 1

        # userId 필드로 찾는 문서.
        for name in ("sessions", "ledger", "ownership", "quotas", "acquisitions",
                     "reward_contexts", "ai_generations",
                     # Phase F. push token은 개인 데이터이고, 판매 알림 기록도
                     # 그 사람의 것이다. **구매자의 소유권·원장은 여기 없다** —
                     # 저쪽은 다른 사람의 권리라 남는다.
                     "push_devices", "notifications", "notification_deliveries"):
            collection = self._c.get(name)
            if collection is None:
                continue
            count += self._delete_where(collection, "userId", user_id)

        # Apple subject ↔ 계정 연결. **이걸 지워야 다시 로그인했을 때 새 계정이 된다.**
        count += self._delete_where(self._c["identities"], "userId", user_id)
        return count

    def _delete_where(self, collection: str, field: str, value: str) -> int:
        count = 0
        while True:
            page = list(
                self._db.collection(collection).where(field, "==", value).limit(_PAGE).stream()
            )
            if not page:
                return count
            batch = self._db.batch()
            for snapshot in page:
                batch.delete(snapshot.reference)
            batch.commit()
            count += len(page)
            if len(page) < _PAGE:
                return count
