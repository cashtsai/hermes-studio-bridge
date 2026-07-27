"""Apple StoreKit 2 / App Store Server Notifications V2 的 **離線** JWS 驗簽。

## 為什麼是離線驗簽(不打 Apple 伺服器)

StoreKit 2 的 `signedTransaction`、以及 App Store Server Notifications V2 的
`signedPayload`,都是 **ES256 JWS,header 帶完整 `x5c` 憑證鏈**
(`[leaf, Apple WWDR intermediate, Apple Root CA - G3]`)。也就是說「這筆交易
是不是 Apple 簽的」這件事,答案**整包就在 token 裡**:把鏈驗到我們自己釘死
的 Apple Root CA 即可,不需要任何網路往返。

相對地,舊 `verifyReceipt` / App Store Server API 的 online 驗證要:
  1. 每次驗證都打 Apple 一趟 —— bridge 跑在家用 Mac Studio + cloudflare
     tunnel 後面,Apple 掛掉或網路抖動就等於「使用者付了錢卻進不了付費層」;
  2. 額外保管一把 App Store Server API 的 .p8 金鑰(多一份會外洩的祕密);
  3. 有速率限制,而 App Store Server Notifications 是 Apple 主動狂推的。

離線驗簽把可用性和祕密管理的成本都拿掉,安全強度不減 —— 簽章鏈是同一組
密碼學保證。唯一放棄的是「即時跟 Apple 對帳」,而那正好由 Notifications V2
(續訂/退款 Apple 主動推)補上,兩者互補。

## 信任錨(trust anchor)

Apple Root CA - G3 由**設定**提供(`APPLE_STOREKIT_ROOT_CA_PATH`),repo 不放
憑證檔:沒設就是沒配置 → 整個訂閱模組靜默停用(所有人免費層),照 APNs
金鑰缺席的同一套規矩。匯出方式見 docs/APP_BRIDGE_CONTRACT.md。

本模組是**純函式**:不碰 DB、不碰 FastAPI、不讀 env(除了 caller 傳進來的
參數),方便用自簽假憑證鏈做完全離線的單元測試。
"""

from __future__ import annotations

import base64
import json
import time

# Apple 在 leaf / intermediate 憑證上蓋的自訂 OID。鏈驗到 Apple Root 之後再
# 認這兩個 OID,是縱深防禦:擋掉「同一個 Apple Root 底下另一條用途完全不同
# 的憑證」被拿來簽假交易。Apple 官方 app-store-server-library 也做同樣的事。
APPLE_LEAF_OID = "1.2.840.113635.100.6.11.1"
APPLE_INTERMEDIATE_OID = "1.2.840.113635.100.6.2.1"

MAX_JWS_BYTES = 256 * 1024
MAX_CHAIN_CERTS = 5


class StoreKitVerifyError(Exception):
    """驗簽失敗。`reason` 是穩定的機器可讀代碼,會進 event log(不含祕密)。"""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def _b64u_decode(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def _der(cert) -> bytes:
    """憑證的 DER bytes —— 用來做「是不是我們釘的那張根憑證」的逐位元組比對。"""
    from cryptography.hazmat.primitives.serialization import Encoding
    return cert.public_bytes(Encoding.DER)


def load_root_certificates(data: bytes) -> list:
    """吃 PEM(可多張)或 DER,回 x509 憑證清單。"""
    from cryptography import x509

    certs: list = []
    text = data.lstrip()
    if text.startswith(b"-----BEGIN"):
        chunks = data.split(b"-----BEGIN CERTIFICATE-----")
        for chunk in chunks[1:]:
            body = chunk.split(b"-----END CERTIFICATE-----")[0]
            pem = b"-----BEGIN CERTIFICATE-----" + body + b"-----END CERTIFICATE-----\n"
            certs.append(x509.load_pem_x509_certificate(pem))
    else:
        certs.append(x509.load_der_x509_certificate(data))
    if not certs:
        raise StoreKitVerifyError("root_ca_empty", "no certificate in trust anchor file")
    return certs


def _verify_signature(public_key, signature: bytes, message: bytes, hash_alg) -> None:
    """憑證簽章驗證(DER 編碼的簽章)。EC 與 RSA 都吃;Apple 目前全 EC。"""
    from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa

    if isinstance(public_key, ec.EllipticCurvePublicKey):
        public_key.verify(signature, message, ec.ECDSA(hash_alg))
    elif isinstance(public_key, rsa.RSAPublicKey):
        public_key.verify(signature, message, padding.PKCS1v15(), hash_alg)
    elif isinstance(public_key, ed25519.Ed25519PublicKey):
        public_key.verify(signature, message)
    else:
        raise StoreKitVerifyError("unsupported_key", type(public_key).__name__)


def _cert_signed_by(cert, issuer) -> None:
    """cert 必須由 issuer 簽出。失敗一律轉成 StoreKitVerifyError —— 上層要靠
    這個型別在多個信任錨之間試接合,漏出 cryptography 的 InvalidSignature 會
    直接炸成 500 而不是乾淨的「拒絕」。"""
    if cert.issuer != issuer.subject:
        raise StoreKitVerifyError("chain_issuer_mismatch")
    try:
        _verify_signature(issuer.public_key(), cert.signature,
                          cert.tbs_certificate_bytes, cert.signature_hash_algorithm)
    except StoreKitVerifyError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise StoreKitVerifyError("chain_bad_signature", type(exc).__name__)


def _cert_has_oid(cert, oid: str) -> bool:
    from cryptography import x509
    from cryptography.x509.oid import ObjectIdentifier

    try:
        cert.extensions.get_extension_for_oid(ObjectIdentifier(oid))
        return True
    except x509.ExtensionNotFound:
        return False


def _check_validity(cert, now: float, label: str) -> None:
    not_before = cert.not_valid_before_utc.timestamp()
    not_after = cert.not_valid_after_utc.timestamp()
    if now < not_before:
        raise StoreKitVerifyError("cert_not_yet_valid", label)
    if now > not_after:
        raise StoreKitVerifyError("cert_expired", label)


def verify_jws(token: str, trusted_roots: list, *, now: float | None = None,
               check_cert_oids: bool = True,
               leaf_oid: str = APPLE_LEAF_OID,
               intermediate_oid: str = APPLE_INTERMEDIATE_OID) -> dict:
    """驗一段 Apple 簽的 JWS,回傳解出來的 payload dict。

    驗簽失敗一律 raise StoreKitVerifyError —— **絕不**回傳未驗證的內容。
    caller 不得在失敗時退回 `get_unverified_claims` 之類的東西。
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
    from cryptography import x509

    if not isinstance(token, str) or not token:
        raise StoreKitVerifyError("jws_missing")
    if len(token) > MAX_JWS_BYTES:
        raise StoreKitVerifyError("jws_too_large", str(len(token)))
    if not trusted_roots:
        raise StoreKitVerifyError("no_trust_anchor")

    parts = token.split(".")
    if len(parts) != 3:
        raise StoreKitVerifyError("jws_malformed")
    header_b64, payload_b64, sig_b64 = parts

    try:
        header = json.loads(_b64u_decode(header_b64))
        signature_raw = _b64u_decode(sig_b64)
        payload_bytes = _b64u_decode(payload_b64)
    except Exception as exc:  # noqa: BLE001
        raise StoreKitVerifyError("jws_decode_failed", type(exc).__name__)
    if not isinstance(header, dict):
        raise StoreKitVerifyError("jws_header_malformed")

    # ES256 寫死。不接受 alg=none、不接受 HS256(對稱演算法 + 攻擊者控制的
    # 憑證鏈 = 經典 JWT 演算法混淆漏洞),也不接受 RS256(Apple 不用)。
    if header.get("alg") != "ES256":
        raise StoreKitVerifyError("jws_bad_alg", str(header.get("alg"))[:32])

    x5c = header.get("x5c")
    if not isinstance(x5c, list) or not x5c:
        raise StoreKitVerifyError("jws_missing_x5c")
    if len(x5c) > MAX_CHAIN_CERTS:
        raise StoreKitVerifyError("chain_too_long", str(len(x5c)))
    try:
        chain = [x509.load_der_x509_certificate(base64.b64decode(str(c), validate=True))
                 for c in x5c]
    except Exception as exc:  # noqa: BLE001
        raise StoreKitVerifyError("chain_parse_failed", type(exc).__name__)

    now = time.time() if now is None else now
    for idx, cert in enumerate(chain):
        _check_validity(cert, now, f"x5c[{idx}]")

    # 信任錨接合:x5c 末端通常就是 Apple Root CA - G3 本身。DER 完全相同才算
    # 「就是我們釘的那張」;否則把末端當中介,要求它由某個信任錨簽出來。
    root_ders = {}
    for root in trusted_roots:
        _check_validity(root, now, "trust_anchor")
        root_ders[_der(root)] = root
    if _der(chain[-1]) in root_ders:
        full = list(chain)
    else:
        anchored = None
        for root in trusted_roots:
            try:
                _cert_signed_by(chain[-1], root)
                anchored = root
                break
            except StoreKitVerifyError:
                continue
        if anchored is None:
            raise StoreKitVerifyError("chain_untrusted_root")
        full = list(chain) + [anchored]

    if len(full) < 2:
        raise StoreKitVerifyError("chain_too_short")
    for i in range(len(full) - 1):
        _cert_signed_by(full[i], full[i + 1])

    leaf = full[0]
    if check_cert_oids:
        if leaf_oid and not _cert_has_oid(leaf, leaf_oid):
            raise StoreKitVerifyError("leaf_oid_missing", leaf_oid)
        if intermediate_oid and not _cert_has_oid(full[1], intermediate_oid):
            raise StoreKitVerifyError("intermediate_oid_missing", intermediate_oid)

    public_key = leaf.public_key()
    if not isinstance(public_key, ec.EllipticCurvePublicKey):
        raise StoreKitVerifyError("leaf_key_not_ec", type(public_key).__name__)
    if len(signature_raw) != 64:
        raise StoreKitVerifyError("jws_bad_signature_length", str(len(signature_raw)))
    r = int.from_bytes(signature_raw[:32], "big")
    s = int.from_bytes(signature_raw[32:], "big")
    signing_input = (header_b64 + "." + payload_b64).encode("ascii")
    try:
        public_key.verify(encode_dss_signature(r, s), signing_input,
                          ec.ECDSA(hashes.SHA256()))
    except Exception:  # noqa: BLE001
        raise StoreKitVerifyError("jws_bad_signature")

    try:
        payload = json.loads(payload_bytes)
    except Exception as exc:  # noqa: BLE001
        raise StoreKitVerifyError("payload_not_json", type(exc).__name__)
    if not isinstance(payload, dict):
        raise StoreKitVerifyError("payload_not_object")
    return payload


# ───────────────────────── payload 正規化 ──────────────────────────────────
# Apple 的時間欄位是 **毫秒** epoch。全部轉成 bridge 慣用的秒(float),
# 免得「到期日」在某處被當成秒、某處被當成毫秒 —— 那種 bug 會讓所有人
# 看起來訂閱到西元 57000 年。

def _ms(value):
    if value in (None, ""):
        return None
    try:
        return float(value) / 1000.0
    except (TypeError, ValueError):
        return None


def _text(value, limit: int = 200):
    if value in (None, ""):
        return None
    return str(value)[:limit]


def normalize_transaction(payload: dict) -> dict:
    """JWSTransactionDecodedPayload → bridge 內部欄位。"""
    return {
        "transaction_id": _text(payload.get("transactionId")),
        "original_transaction_id": _text(payload.get("originalTransactionId")),
        "bundle_id": _text(payload.get("bundleId")),
        "product_id": _text(payload.get("productId")),
        "type": _text(payload.get("type")),
        "environment": _text(payload.get("environment"), 32),
        "purchase_at": _ms(payload.get("purchaseDate")),
        "original_purchase_at": _ms(payload.get("originalPurchaseDate")),
        "expires_at": _ms(payload.get("expiresDate")),
        "revoked_at": _ms(payload.get("revocationDate")),
        "revocation_reason": payload.get("revocationReason"),
        "signed_at": _ms(payload.get("signedDate")),
        "app_account_token": _text(payload.get("appAccountToken"), 64),
        "ownership": _text(payload.get("inAppOwnershipType"), 32),
        "storefront": _text(payload.get("storefront"), 16),
    }


def normalize_renewal(payload: dict) -> dict:
    """JWSRenewalInfoDecodedPayload → bridge 內部欄位。"""
    auto_renew = payload.get("autoRenewStatus")
    return {
        "original_transaction_id": _text(payload.get("originalTransactionId")),
        "product_id": _text(payload.get("autoRenewProductId") or payload.get("productId")),
        "auto_renew": None if auto_renew is None else bool(int(auto_renew or 0)),
        "grace_expires_at": _ms(payload.get("gracePeriodExpiresDate")),
        "renewal_at": _ms(payload.get("renewalDate")),
        "expiration_intent": payload.get("expirationIntent"),
        "billing_retry": bool(payload.get("isInBillingRetryPeriod")),
        "signed_at": _ms(payload.get("signedDate")),
        "environment": _text(payload.get("environment"), 32),
    }
