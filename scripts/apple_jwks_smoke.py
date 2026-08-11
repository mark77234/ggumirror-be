"""Apple JWKS endpoint를 실제로 한 번 불러보는 수동 확인용.

unit test는 Apple을 부르지 않는다(인터넷 없이 통과해야 한다). CI에도 넣지 않는다.
`http_jwks_fetch`가 실제 endpoint와 맞는지 손으로 볼 때만 쓴다.

    .venv/bin/python scripts/apple_jwks_smoke.py

key 값은 출력하지 않는다. kid와 alg만 본다.
"""

from app.auth.jwks import AppleJWKSProvider, http_jwks_fetch


def main() -> None:
    document = http_jwks_fetch()()
    print(f"keys: {len(document.get('keys', []))}")
    for key in document.get("keys", []):
        print(f"  kid={key.get('kid')} alg={key.get('alg')} kty={key.get('kty')}")

    provider = AppleJWKSProvider()
    first_kid = document["keys"][0]["kid"]
    provider.key_for(first_kid)
    print(f"provider resolved kid={first_kid} ok")


if __name__ == "__main__":
    main()
