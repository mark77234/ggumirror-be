"""test에서 Google ADC lookup을 금지하는 guard.

`conftest.py`가 아니라 **별도 module**에 두는 이유가 있다. `tests/__init__.py`가
없어서 pytest는 conftest를 top-level `conftest`로 import하고, test가
`from tests.conftest import ...`로 부르면 `tests.conftest`가 **따로** import된다.
같은 이름의 class가 두 개가 되어 `pytest.raises`가 잡지 못한다. 여기서 정의하면
경로가 하나뿐이라 그 문제가 없다.
"""

from __future__ import annotations


class GoogleCredentialLookup(BaseException):
    """test가 Google ADC를 찾으려 했다. **`Exception`이 아니다.**

    `app/api/deps.py`의 dependency들이 store 생성 실패를 `except Exception`으로 잡아
    503으로 바꾼다. `Exception`을 물려받으면 이 사고가 그 자리에서 조용한 503으로
    삼켜진다 — CI에서 실제로 그렇게 69개가 무너졌고 원인이 보이지 않았다.
    `BaseException`이라 그 자리를 그대로 통과해 test를 시끄럽게 깨뜨린다.
    """


MESSAGE = (
    "test tried to look up Google credentials. "
    "create_app(...)에 빠진 store를 in-memory fake로 주입해라 "
    "(auth_store · shard_store · marketplace_store · catalog_store · "
    "push_store · notification_store · preference_store · delivery_store · ...). "
    "production fallback을 바꾸는 것이 아니다."
)


def blocked(*args: object, **kwargs: object) -> None:
    raise GoogleCredentialLookup(MESSAGE)
