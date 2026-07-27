"""feat/subscription-entitlement 驗收(repo 慣例:python3 tests/test_subscription_entitlement.py)。

**全程離線**:用自簽的假憑證鏈(fake root → fake WWDR intermediate → fake leaf)
簽出結構與 Apple 完全相同的 JWS,信任錨指向我們自己的 fake root。所以這支測試
不打 Apple、不需要真收據,卻走的是與正式環境同一條驗簽程式碼路徑。

驗證:
1. 離線驗簽本體(apple_storekit.verify_jws):
   - 正常鏈 → 解出 payload。
   - 竄改 payload / 竄改簽章 → 拒絕。
   - 鏈接不到信任錨(別人的 root)→ 拒絕。
   - alg 動手腳(none / HS256 演算法混淆)→ 拒絕。
   - 缺 Apple leaf/intermediate OID → 拒絕。
   - 憑證已過期 / 還沒生效 → 拒絕。
2. 缺配置全靜默:沒設 APPLE_STOREKIT_* → verify/status/notify 都回 200 +
   configured=false + 免費層,且完全不寫 entitlements 表。
3. /app/v1/subscription/verify:有效訂閱 → tier=pro、active=true;寫入
   entitlements;冪等重送不變;bundle id 不符 → 400;沒帶收據 → 400;
   app 自稱 pro(body 塞 tier/expires)→ 完全不採信。
4. 跨裝置一致:同帳號的另一個 session(另一台裝置)/status 看到同一份
   entitlement;/auth/apple 與 /account 回應都帶 entitlement。
5. 過期判定:expiresDate 已過 → status=expired、active=false、tier 掉回 free
   (不需要任何 cron 掃表)。寬限期(gracePeriodExpiresDate 未到)→ grace 仍算付費。
6. 退款撤銷:REFUND 通知帶 revocationDate → status=revoked、active=false。
7. Server Notifications V2:簽章錯 → 401;TEST 通知 → 不改 DB;bundle 不符 →
   200 但不套用;通知比 app 先到 → 落無主列,之後 /verify 認領。
8. 單調性:較舊的續訂通知不得覆蓋較新狀態(不得讓已退款的人復活)。
"""
import base64
import datetime
import json
import os
import sqlite3
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="sub-entitlement-")
os.environ["POCKET_CANON_DB"] = os.path.join(_TMP, "canonical.db")
os.environ["POCKET_ACCOUNTS_DB"] = os.path.join(_TMP, "accounts.db")
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
# 訂閱模組配置:bundle id + 假 Apple Root CA(下面產生後才寫檔,但 bridge 只在
# 呼叫時才讀檔,import 期不讀,所以先設路徑沒問題)。
_ROOT_CA_PATH = os.path.join(_TMP, "fake-apple-root.pem")
os.environ["APPLE_STOREKIT_BUNDLE_ID"] = "com.pocketagent.kernel"
os.environ["APPLE_STOREKIT_ROOT_CA_PATH"] = _ROOT_CA_PATH
os.environ["APPLE_STOREKIT_ENVIRONMENTS"] = "Production,Sandbox"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.utils import (  # noqa: E402
    decode_dss_signature,
)
from cryptography.x509.oid import NameOID, ObjectIdentifier  # noqa: E402

import apple_storekit  # noqa: E402

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


def expect_reason(name, fn, reason):
    """fn 必須 raise StoreKitVerifyError 且 reason 相符。"""
    try:
        fn()
    except apple_storekit.StoreKitVerifyError as exc:
        check(f"{name} (reason={reason})", exc.reason == reason)
        if exc.reason != reason:
            print(f"     got reason={exc.reason} detail={exc.detail}")
        return
    except Exception as exc:  # noqa: BLE001
        check(f"{name} (reason={reason})", False)
        print(f"     raised {type(exc).__name__}: {exc}")
        return
    check(f"{name} (reason={reason})", False)
    print("     no exception raised — 驗簽居然通過了")


# ───────────────────── 假 Apple 憑證鏈(完全離線)─────────────────────────
_NOW = datetime.datetime.now(datetime.timezone.utc)


def _name(cn):
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Fake Apple Inc.")])


def _issue(subject_name, subject_key, issuer_name, issuer_key, *, ca,
           oids=(), not_before=None, not_after=None):
    not_before = not_before or (_NOW - datetime.timedelta(days=1))
    not_after = not_after or (_NOW + datetime.timedelta(days=365))
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject_name)
        .issuer_name(issuer_name)
        .public_key(subject_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
    )
    for oid in oids:
        # Apple 蓋在憑證上的自訂 OID 就是這種「有 OID、內容空」的擴充。
        builder = builder.add_extension(
            x509.UnrecognizedExtension(ObjectIdentifier(oid), b""), critical=False)
    return builder.sign(issuer_key, hashes.SHA256())


class FakeAppleChain:
    """root(P-384,自簽)→ intermediate(帶 WWDR OID)→ leaf(P-256,帶 leaf OID)。"""

    def __init__(self, *, leaf_oids=(apple_storekit.APPLE_LEAF_OID,),
                 intermediate_oids=(apple_storekit.APPLE_INTERMEDIATE_OID,),
                 leaf_not_before=None, leaf_not_after=None):
        self.root_key = ec.generate_private_key(ec.SECP384R1())
        root_name = _name("Fake Apple Root CA - G3")
        self.root = _issue(root_name, self.root_key, root_name, self.root_key, ca=True)
        self.inter_key = ec.generate_private_key(ec.SECP256R1())
        inter_name = _name("Fake Apple WWDR CA")
        self.intermediate = _issue(inter_name, self.inter_key, root_name, self.root_key,
                                   ca=True, oids=intermediate_oids)
        self.leaf_key = ec.generate_private_key(ec.SECP256R1())
        self.leaf = _issue(_name("Fake Apple Receipt Signing"), self.leaf_key,
                           inter_name, self.inter_key, ca=False, oids=leaf_oids,
                           not_before=leaf_not_before, not_after=leaf_not_after)

    @property
    def x5c(self):
        return [base64.b64encode(
            c.public_bytes(serialization.Encoding.DER)).decode("ascii")
            for c in (self.leaf, self.intermediate, self.root)]

    def root_pem(self):
        return self.root.public_bytes(serialization.Encoding.PEM)

    def sign(self, payload: dict, *, alg="ES256", x5c=None, break_signature=False):
        header = {"alg": alg, "x5c": self.x5c if x5c is None else x5c}
        h = _b64u(json.dumps(header, separators=(",", ":")).encode())
        p = _b64u(json.dumps(payload, separators=(",", ":")).encode())
        der_sig = self.leaf_key.sign((h + "." + p).encode("ascii"),
                                     ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der_sig)
        raw = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        if break_signature:
            raw = bytes((raw[0] ^ 0xFF,)) + raw[1:]
        return h + "." + p + "." + _b64u(raw)


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


CHAIN = FakeAppleChain()
with open(_ROOT_CA_PATH, "wb") as fh:
    fh.write(CHAIN.root_pem())
OTHER_CHAIN = FakeAppleChain()          # 別人的 root:鏈接不到我們的信任錨

import bridge  # noqa: E402  (import 即跑 _canon_init / _accounts_init)

from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(bridge.app)
ROOTS = apple_storekit.load_root_certificates(CHAIN.root_pem())

BUNDLE_ID = "com.pocketagent.kernel"
PRODUCT_ID = "com.pocketagent.kernel.pro.monthly"


def ms(dt_offset_seconds: float) -> int:
    import time as _t
    return int((_t.time() + dt_offset_seconds) * 1000)


def transaction_payload(*, otid="2000000123456789", txid=None, product_id=PRODUCT_ID,
                        expires_in=30 * 86400, revoked_in=None, signed_in=0,
                        bundle_id=BUNDLE_ID, environment="Production"):
    payload = {
        "transactionId": txid or otid,
        "originalTransactionId": otid,
        "bundleId": bundle_id,
        "productId": product_id,
        "type": "Auto-Renewable Subscription",
        "inAppOwnershipType": "PURCHASED",
        "environment": environment,
        "purchaseDate": ms(-86400),
        "originalPurchaseDate": ms(-86400),
        "signedDate": ms(signed_in),
    }
    if expires_in is not None:
        payload["expiresDate"] = ms(expires_in)
    if revoked_in is not None:
        payload["revocationDate"] = ms(revoked_in)
        payload["revocationReason"] = 1
    return payload


def renewal_payload(*, otid="2000000123456789", product_id=PRODUCT_ID,
                    auto_renew=1, grace_in=None, signed_in=0,
                    environment="Production"):
    payload = {
        "originalTransactionId": otid,
        "autoRenewProductId": product_id,
        "productId": product_id,
        "autoRenewStatus": auto_renew,
        "environment": environment,
        "signedDate": ms(signed_in),
    }
    if grace_in is not None:
        payload["gracePeriodExpiresDate"] = ms(grace_in)
    return payload


def notification(payload_tx, payload_renewal=None, *, notification_type="DID_RENEW",
                 subtype=None, bundle_id=BUNDLE_ID, chain=CHAIN,
                 include_transaction=True):
    data = {"bundleId": bundle_id, "environment": "Production",
            "appAppleId": 1234567890}
    if include_transaction:
        data["signedTransactionInfo"] = chain.sign(payload_tx)
    if payload_renewal is not None:
        data["signedRenewalInfo"] = chain.sign(payload_renewal)
    body = {"notificationType": notification_type, "notificationUUID": "uuid-1",
            "version": "2.0", "signedDate": ms(0), "data": data}
    if subtype:
        body["subtype"] = subtype
    return chain.sign(body)


def account(apple_user_id):
    """建一個帳號 + 回一個 account session token(等同一台已登入的裝置)。"""
    bridge._account_upsert_user(apple_user_id, email=None, display_name="Tester")
    token, _exp = bridge._account_session_create(apple_user_id)
    return {"X-Pocket-Account-Session": token}


def ent_rows():
    con = sqlite3.connect(bridge.ACCOUNTS_DB)
    try:
        return con.execute(
            "SELECT original_transaction_id, apple_user_id, status, tier, revoked_at "
            "FROM entitlements ORDER BY original_transaction_id").fetchall()
    finally:
        con.close()


# ═══════════ 1. 離線驗簽本體 ═══════════
tx = transaction_payload()
good = CHAIN.sign(tx)
decoded = apple_storekit.verify_jws(good, ROOTS)
check("1.1 正常鏈驗簽通過並解出 payload",
      decoded.get("originalTransactionId") == "2000000123456789"
      and decoded.get("productId") == PRODUCT_ID)

norm = apple_storekit.normalize_transaction(decoded)
check("1.1b 毫秒 → 秒轉換(到期日不會變成西元五萬年)",
      norm["expires_at"] is not None
      and abs(norm["expires_at"] - (norm["purchase_at"] + 31 * 86400)) < 3
      and norm["expires_at"] < 1e11)

expect_reason("1.2 竄改簽章 → 拒絕",
              lambda: apple_storekit.verify_jws(CHAIN.sign(tx, break_signature=True),
                                                ROOTS),
              "jws_bad_signature")

# 把 payload 換成「到期日 10 年後」但沿用原簽章 —— 最直觀的本機竄改攻擊。
h, p, s = good.split(".")
forged_payload = dict(tx, expiresDate=ms(3650 * 86400))
forged = h + "." + _b64u(json.dumps(forged_payload, separators=(",", ":")).encode()) + "." + s
expect_reason("1.3 竄改 payload(偽造到期日)沿用原簽章 → 拒絕",
              lambda: apple_storekit.verify_jws(forged, ROOTS),
              "jws_bad_signature")

expect_reason("1.4 鏈接不到我們釘的 root(別人的自簽鏈)→ 拒絕",
              lambda: apple_storekit.verify_jws(OTHER_CHAIN.sign(tx), ROOTS),
              "chain_untrusted_root")

# alg=none:經典「拿掉簽章」攻擊。
none_header = _b64u(json.dumps({"alg": "none", "x5c": CHAIN.x5c},
                               separators=(",", ":")).encode())
none_token = none_header + "." + p + "."
expect_reason("1.5 alg=none → 拒絕",
              lambda: apple_storekit.verify_jws(none_token, ROOTS),
              "jws_bad_alg")
expect_reason("1.6 alg=HS256(演算法混淆)→ 拒絕",
              lambda: apple_storekit.verify_jws(CHAIN.sign(tx, alg="HS256"), ROOTS),
              "jws_bad_alg")
expect_reason("1.7 header 沒有 x5c → 拒絕",
              lambda: apple_storekit.verify_jws(CHAIN.sign(tx, x5c=[]), ROOTS),
              "jws_missing_x5c")

no_leaf_oid = FakeAppleChain(leaf_oids=())
_no_leaf_roots = apple_storekit.load_root_certificates(no_leaf_oid.root_pem())
expect_reason("1.8 leaf 缺 Apple OID → 拒絕(縱深防禦)",
              lambda: apple_storekit.verify_jws(no_leaf_oid.sign(tx), _no_leaf_roots),
              "leaf_oid_missing")
check("1.8b 關閉 OID 釘選時同一條鏈可通過(逃生開關有效)",
      apple_storekit.verify_jws(no_leaf_oid.sign(tx), _no_leaf_roots,
                                check_cert_oids=False).get("productId") == PRODUCT_ID)

expired_chain = FakeAppleChain(
    leaf_not_before=_NOW - datetime.timedelta(days=60),
    leaf_not_after=_NOW - datetime.timedelta(days=1))
_expired_roots = apple_storekit.load_root_certificates(expired_chain.root_pem())
expect_reason("1.9 leaf 憑證已過期 → 拒絕",
              lambda: apple_storekit.verify_jws(expired_chain.sign(tx), _expired_roots),
              "cert_expired")

future_chain = FakeAppleChain(
    leaf_not_before=_NOW + datetime.timedelta(days=5),
    leaf_not_after=_NOW + datetime.timedelta(days=60))
_future_roots = apple_storekit.load_root_certificates(future_chain.root_pem())
expect_reason("1.10 leaf 憑證還沒生效 → 拒絕",
              lambda: apple_storekit.verify_jws(future_chain.sign(tx), _future_roots),
              "cert_not_yet_valid")

expect_reason("1.11 亂字串 → 拒絕",
              lambda: apple_storekit.verify_jws("not-a-jws", ROOTS),
              "jws_malformed")
expect_reason("1.12 空字串 → 拒絕",
              lambda: apple_storekit.verify_jws("", ROOTS),
              "jws_missing")


# ═══════════ 2. 缺配置全靜默 ═══════════
_headers_absent = account("apple-absent-user")
_saved = (bridge.APPLE_STOREKIT_BUNDLE_ID, bridge.APPLE_STOREKIT_ROOT_CA_PATH)
bridge.APPLE_STOREKIT_BUNDLE_ID = ""
bridge.APPLE_STOREKIT_ROOT_CA_PATH = ""
bridge._SUBSCRIPTION_DISABLED_LOGGED[0] = False
check("2.1 未配置 → subscription_configured() False", bridge.subscription_configured() is False)
r = client.post("/app/v1/subscription/verify", headers=_headers_absent,
                json={"signedTransaction": CHAIN.sign(tx)})
body = r.json()
check("2.2 未配置的 verify 回 200(不是錯誤)", r.status_code == 200)
check("2.3 未配置的 verify 回 configured=false + 免費層",
      body.get("configured") is False
      and body["entitlement"]["tier"] == "free"
      and body["entitlement"]["active"] is False
      and body["entitlement"]["configured"] is False)
r = client.get("/app/v1/subscription/status", headers=_headers_absent)
check("2.4 未配置的 status 回 200 + 免費層",
      r.status_code == 200 and r.json()["configured"] is False
      and r.json()["entitlement"]["tier"] == "free")
r = client.post("/app/v1/subscription/notify", json={"signedPayload": notification(tx)})
check("2.5 未配置的 notify 回 200(讓 Apple 別重試)+ 不處理",
      r.status_code == 200 and r.json().get("configured") is False)
check("2.6 未配置期間完全沒寫 entitlements 表", ent_rows() == [])
r = client.get("/capabilities", headers={"Authorization": "Bearer test-unit-token"})
check("2.7 capabilities 誠實回報 subscription_configured=false",
      r.json().get("subscription_configured") is False
      and "subscription_entitlement" in r.json()["features"])
bridge.APPLE_STOREKIT_BUNDLE_ID, bridge.APPLE_STOREKIT_ROOT_CA_PATH = _saved
bridge._SUBSCRIPTION_DISABLED_LOGGED[0] = False
check("2.8 配置回來 → subscription_configured() True", bridge.subscription_configured() is True)


# ═══════════ 3. /verify:訂閱換 entitlement ═══════════
USER_A = "apple-user-a"
h_a = account(USER_A)

r = client.post("/app/v1/subscription/verify", headers=h_a,
                json={"signedTransaction": CHAIN.sign(tx),
                      "signedRenewalInfo": CHAIN.sign(renewal_payload())})
body = r.json()
ent = body.get("entitlement", {})
check("3.1 有效訂閱 → 200 + configured=true", r.status_code == 200 and body["configured"] is True)
check("3.2 tier=pro / active=true / status=active",
      ent.get("tier") == "pro" and ent.get("active") is True
      and ent.get("status") == "active")
check("3.3 product_id 與到期日由 bridge 自己從收據解出",
      ent.get("product_id") == PRODUCT_ID and isinstance(ent.get("expires_at"), float))
check("3.4 auto_renew 由 renewal info 解出", ent.get("auto_renew") is True)
rows = ent_rows()
check("3.5 entitlements 表寫入並綁到帳號",
      len(rows) == 1 and rows[0][0] == "2000000123456789"
      and rows[0][1] == USER_A and rows[0][2] == "active")

r2 = client.post("/app/v1/subscription/verify", headers=h_a,
                 json={"signedTransaction": CHAIN.sign(tx)})
check("3.6 重送同一筆收據冪等(不長出第二列)",
      r2.status_code == 200 and len(ent_rows()) == 1)

r = client.post("/app/v1/subscription/verify", headers=h_a, json={})
check("3.7 沒帶收據 → 400", r.status_code == 400
      and r.json()["error"]["code"] == "SUBSCRIPTION_MISSING_RECEIPT")

# app 自稱訂閱者:body 塞滿好料但沒有 Apple 簽章 → 一律不採信。
r = client.post("/app/v1/subscription/verify", headers=h_a,
                json={"tier": "pro", "active": True, "product_id": PRODUCT_ID,
                      "expires_at": 9999999999, "entitlement": {"tier": "pro"}})
check("3.8 app 直接宣稱『我是訂閱者』→ 400,不採信任何自我宣稱欄位",
      r.status_code == 400
      and r.json()["error"]["code"] == "SUBSCRIPTION_MISSING_RECEIPT")

r = client.post("/app/v1/subscription/verify", headers=h_a,
                json={"signedTransaction": OTHER_CHAIN.sign(tx)})
check("3.9 別人簽的收據 → 401", r.status_code == 401
      and r.json()["error"]["code"] == "SUBSCRIPTION_INVALID_RECEIPT")

r = client.post("/app/v1/subscription/verify", headers=h_a,
                json={"signedTransaction": CHAIN.sign(
                    transaction_payload(otid="9001", bundle_id="com.someoneelse.app"))})
check("3.10 別的 app 的合法收據(bundle 不符)→ 400",
      r.status_code == 400
      and r.json()["error"]["code"] == "SUBSCRIPTION_BUNDLE_MISMATCH")

r = client.post("/app/v1/subscription/verify", headers=h_a,
                json={"signedTransaction": CHAIN.sign(
                    transaction_payload(otid="9002", environment="Xcode"))})
check("3.11 不允許的環境(Xcode 本機 StoreKit 測試)→ 400",
      r.status_code == 400
      and r.json()["error"]["code"] == "SUBSCRIPTION_ENVIRONMENT_REJECTED")

r = client.post("/app/v1/subscription/verify", headers=h_a,
                json={"signedTransaction": CHAIN.sign(tx),
                      "signedRenewalInfo": CHAIN.sign(renewal_payload(otid="7777"))})
check("3.12 續訂資訊與交易不同條訂閱(拼接證據)→ 400",
      r.status_code == 400
      and r.json()["error"]["code"] == "SUBSCRIPTION_RENEWAL_MISMATCH")

r = client.post("/app/v1/subscription/verify",
                json={"signedTransaction": CHAIN.sign(tx)})
check("3.13 沒有 account session → 401", r.status_code == 401)


# ═══════════ 4. 跨裝置一致 ═══════════
h_a2 = account(USER_A)          # 同一個 Apple 帳號在第二台裝置登入
r = client.get("/app/v1/subscription/status", headers=h_a2)
body = r.json()
check("4.1 第二台裝置(從未送過收據)status 就看到同一份 entitlement",
      r.status_code == 200 and body["entitlement"]["active"] is True
      and body["entitlement"]["tier"] == "pro"
      and body["entitlement"]["original_transaction_id"] == "2000000123456789")
check("4.2 status 附完整訂閱清單", len(body.get("entitlements") or []) == 1)

USER_B = "apple-user-b"
h_b = account(USER_B)
r = client.get("/app/v1/subscription/status", headers=h_b)
check("4.3 別的帳號拿不到 A 的訂閱(entitlement 不外洩)",
      r.json()["entitlement"]["tier"] == "free"
      and r.json()["entitlement"]["active"] is False
      and r.json()["entitlements"] == [])

r = client.get("/app/v1/account", headers=h_a)
check("4.4 /app/v1/account 回應帶 entitlement",
      r.status_code == 200 and r.json()["entitlement"]["tier"] == "pro")

# /auth/apple 登入即知訂閱狀態(mock 掉 Apple identity token 驗證)。
_real_verify = bridge._apple_verify_identity_token
bridge._apple_verify_identity_token = lambda token, audience=None: {
    "sub": USER_A, "aud": "com.pocketagent.kernel", "email": None}
try:
    r = client.post("/app/v1/auth/apple",
                    json={"apple_user_id": USER_A, "identityToken": "fake"})
    body = r.json()
    check("4.5 /auth/apple 回應順帶帶 entitlement(登入即知付費層)",
          r.status_code == 200 and body.get("entitlement", {}).get("tier") == "pro"
          and body["entitlement"]["active"] is True)
    r = client.post("/app/v1/auth/apple",
                    json={"apple_user_id": USER_B, "identityToken": "fake"})
    bridge._apple_verify_identity_token = lambda token, audience=None: {
        "sub": USER_B, "aud": "com.pocketagent.kernel", "email": None}
    r = client.post("/app/v1/auth/apple",
                    json={"apple_user_id": USER_B, "identityToken": "fake"})
    check("4.6 沒訂閱的帳號登入 → entitlement 是免費層",
          r.status_code == 200 and r.json()["entitlement"]["tier"] == "free")
finally:
    bridge._apple_verify_identity_token = _real_verify


# ═══════════ 5. 過期 / 寬限期 ═══════════
USER_EXP = "apple-user-expired"
h_exp = account(USER_EXP)
expired_tx = transaction_payload(otid="3000000000000001", expires_in=-3600)
r = client.post("/app/v1/subscription/verify", headers=h_exp,
                json={"signedTransaction": CHAIN.sign(expired_tx)})
ent = r.json()["entitlement"]
check("5.1 到期日已過 → status=expired / active=false / tier 掉回 free",
      r.status_code == 200 and ent["status"] == "expired"
      and ent["active"] is False and ent["tier"] == "free")
r = client.get("/app/v1/subscription/status", headers=h_exp)
check("5.2 status 端點同樣判定過期(不靠 cron 掃表)",
      r.json()["entitlement"]["status"] == "expired"
      and r.json()["entitlement"]["active"] is False)

USER_GRACE = "apple-user-grace"
h_grace = account(USER_GRACE)
grace_tx = transaction_payload(otid="3000000000000002", expires_in=-3600)
r = client.post("/app/v1/subscription/verify", headers=h_grace,
                json={"signedTransaction": CHAIN.sign(grace_tx),
                      "signedRenewalInfo": CHAIN.sign(renewal_payload(
                          otid="3000000000000002", auto_renew=1, grace_in=7 * 86400))})
ent = r.json()["entitlement"]
check("5.3 已過期但在帳單寬限期內 → status=grace、仍算付費(active=true, tier=pro)",
      ent["status"] == "grace" and ent["active"] is True and ent["tier"] == "pro")


# ═══════════ 6. 退款撤銷 ═══════════
refund_tx = transaction_payload(otid="2000000123456789", expires_in=30 * 86400,
                                revoked_in=-60, signed_in=10)
r = client.post("/app/v1/subscription/notify",
                json={"signedPayload": notification(
                    refund_tx, notification_type="REFUND")})
body = r.json()
check("6.1 REFUND 通知 → 200 + applied", r.status_code == 200
      and body.get("applied") is True and body.get("status") == "revoked")
r = client.get("/app/v1/subscription/status", headers=h_a)
ent = r.json()["entitlement"]
check("6.2 退款後 status=revoked、active=false、tier=free(跨裝置立即生效)",
      ent["status"] == "revoked" and ent["active"] is False and ent["tier"] == "free")


# ═══════════ 7. Server Notifications V2 ═══════════
r = client.post("/app/v1/subscription/notify",
                json={"signedPayload": notification(tx, chain=OTHER_CHAIN)})
check("7.1 簽章驗不過的通知 → 401,不寫任何東西",
      r.status_code == 401
      and r.json()["error"]["code"] == "SUBSCRIPTION_INVALID_RECEIPT")

r = client.post("/app/v1/subscription/notify", json={})
check("7.2 沒帶 signedPayload → 400", r.status_code == 400)

r = client.post("/app/v1/subscription/notify",
                json={"signedPayload": notification(
                    tx, notification_type="TEST", include_transaction=False)})
check("7.3 TEST 通知 → 200 但不改 DB",
      r.status_code == 200 and r.json()["applied"] is False)

r = client.post("/app/v1/subscription/notify",
                json={"signedPayload": notification(
                    transaction_payload(otid="8001"), bundle_id="com.someoneelse.app")})
check("7.4 別的 app 的通知 → 200(不重試)但不套用",
      r.status_code == 200 and r.json()["applied"] is False)
check("7.5 被丟掉的通知沒有留下 entitlement 列",
      all(row[0] != "8001" for row in ent_rows()))

r = client.post("/app/v1/subscription/notify",
                json={"signedPayload": notification(
                    transaction_payload(otid="8002"),
                    notification_type="RENEWAL_EXTENSION", include_transaction=False)})
check("7.6 沒有交易資訊的通知 → 200 不套用(沒有可信證據就不改狀態)",
      r.status_code == 200 and r.json()["applied"] is False)

# 通知比 app 的 /verify 先到:先落無主列,之後由 /verify 認領。
orphan_tx = transaction_payload(otid="4000000000000001", expires_in=30 * 86400)
r = client.post("/app/v1/subscription/notify",
                json={"signedPayload": notification(
                    orphan_tx, renewal_payload(otid="4000000000000001"),
                    notification_type="SUBSCRIBED")})
check("7.7 通知先到 → 套用成功", r.status_code == 200 and r.json()["applied"] is True)
orphan = bridge._entitlement_by_original_transaction("4000000000000001")
check("7.8 通知先到時 apple_user_id 為 NULL(無主列,不亂綁帳號)",
      orphan is not None and orphan["apple_user_id"] is None)
USER_C = "apple-user-c"
h_c = account(USER_C)
r = client.get("/app/v1/subscription/status", headers=h_c)
check("7.9 無主 entitlement 不會被任何帳號看到",
      r.json()["entitlement"]["tier"] == "free")
r = client.post("/app/v1/subscription/verify", headers=h_c,
                json={"signedTransaction": CHAIN.sign(orphan_tx)})
check("7.10 app 帶 account session 來 /verify → 認領該訂閱",
      r.status_code == 200 and r.json()["entitlement"]["active"] is True)
claimed = bridge._entitlement_by_original_transaction("4000000000000001")
check("7.11 認領後綁定 apple_user_id",
      claimed is not None and claimed["apple_user_id"] == USER_C)

# 認領後 Apple 又推一則續訂通知(不帶帳號資訊)→ 不得把綁定洗成 NULL。
r = client.post("/app/v1/subscription/notify",
                json={"signedPayload": notification(
                    transaction_payload(otid="4000000000000001",
                                        expires_in=60 * 86400, signed_in=30),
                    renewal_payload(otid="4000000000000001", signed_in=30),
                    notification_type="DID_RENEW")})
still = bridge._entitlement_by_original_transaction("4000000000000001")
check("7.12 後續通知不會清掉已綁定的 apple_user_id",
      r.status_code == 200 and still["apple_user_id"] == USER_C)
r = client.get("/app/v1/subscription/status", headers=h_c)
check("7.13 續訂後到期日往後延(跨裝置一致)",
      r.json()["entitlement"]["active"] is True
      and r.json()["entitlement"]["expires_at"] > claimed["expires_at"])


# ═══════════ 8. 單調性:舊事件不得覆蓋新狀態 ═══════════
# 已退款的 2000000123456789 收到一則「較早簽發」的續訂通知 → 不得復活。
r = client.post("/app/v1/subscription/notify",
                json={"signedPayload": notification(
                    transaction_payload(otid="2000000123456789",
                                        expires_in=90 * 86400, signed_in=-3600),
                    notification_type="DID_RENEW")})
r2 = client.get("/app/v1/subscription/status", headers=h_a)
ent = r2.json()["entitlement"]
check("8.1 遲到的舊續訂通知不得讓已退款的訂閱復活",
      ent["status"] == "revoked" and ent["active"] is False)

# 反過來:較新的撤銷一定要生效,即使它比既有紀錄「舊」。
late_revoke_tx = transaction_payload(otid="4000000000000001", expires_in=60 * 86400,
                                     revoked_in=-30, signed_in=-7200)
r = client.post("/app/v1/subscription/notify",
                json={"signedPayload": notification(
                    late_revoke_tx, notification_type="REVOKE")})
r2 = client.get("/app/v1/subscription/status", headers=h_c)
ent = r2.json()["entitlement"]
check("8.2 撤銷是終局狀態:即使 signedDate 較舊也一定生效(退款不能漏)",
      r.status_code == 200 and ent["status"] == "revoked" and ent["active"] is False)


# 只帶交易、不帶續訂資訊的通知(Apple 常態)不得把既有的寬限期洗掉 ——
# 否則寬限期內的使用者會因為一則 DID_FAIL_TO_RENEW 瞬間掉出付費層。
r = client.post("/app/v1/subscription/notify",
                json={"signedPayload": notification(
                    transaction_payload(otid="3000000000000002", expires_in=-1800,
                                        signed_in=60),
                    notification_type="DID_FAIL_TO_RENEW", subtype="GRACE_PERIOD")})
r2 = client.get("/app/v1/subscription/status", headers=h_grace)
ent = r2.json()["entitlement"]
check("8.3 沒帶續訂資訊的通知不得洗掉既有寬限期/自動續訂",
      r.status_code == 200 and ent["status"] == "grace"
      and ent["active"] is True and ent["grace_expires_at"] is not None
      and ent["auto_renew"] is True)

# 撤銷之後又用同一條訂閱重新訂閱(較新的交易、無 revocationDate)→ 應復活。
r = client.post("/app/v1/subscription/verify", headers=h_a,
                json={"signedTransaction": CHAIN.sign(transaction_payload(
                    otid="2000000123456789", expires_in=30 * 86400, signed_in=7200))})
ent = r.json()["entitlement"]
check("8.4 退款後以較新交易重新訂閱 → 恢復付費層(撤銷不是永久黑名單)",
      r.status_code == 200 and ent["status"] == "active" and ent["tier"] == "pro")

print()
if fails:
    print(f"FAILED {len(fails)}: " + ", ".join(fails))
    sys.exit(1)
print("ALL PASS")
