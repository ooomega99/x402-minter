import base64
import json
import logging
import secrets
import time
from datetime import datetime, timezone
from typing import Any

from eth_account.messages import encode_defunct
from eth_account.signers.local import LocalAccount
from requests import Session, Response
from requests.exceptions import RequestException, Timeout
from eth_account import Account

from x402.clients.requests import x402_requests
from x402.clients.base import PaymentError


logger = logging.getLogger(__name__)


class X402MintError(RuntimeError):
    """Fatal error raised when minting fails after all retries."""


class X402Minter:
    def __init__(
        self,
        private_key: str,
        *,
        timeout: float = 10.0,
        network: str = "base",
        scheme: str = "exact",
        x402_version: int = 1,
    ) -> None:
        self.account: LocalAccount = Account.from_key(private_key)
        self.four_last_address_chars: str = self.account.address[-4:]
        self.s: Session = x402_requests(self.account)
        self.timeout: float = timeout
        self.network = network
        self.scheme = scheme
        self.x402_version = x402_version

    def _sleep_with_backoff(self, attempt: int, base: float, cap: float) -> None:
        """Wait using exponential backoff with random jitter to prevent thundering herd issues."""
        delay = min(cap, base * (2 ** (attempt - 1)))
        delay = delay * (0.6 + 0.8 * secrets.randbelow(1000) / 1000.0)  # 60–140% jitter
        time.sleep(delay)

    # ---------- Validation ----------
    def _validate_payment_body(self, body: dict[str, Any]) -> None:
        """Ensure the payment body structure contains required fields before signing."""
        if not isinstance(body, dict):
            raise ValueError("body must be a dict")

        accepts = body.get("accepts")
        if not isinstance(accepts, list) or not accepts:
            raise ValueError("body['accepts'] must be a non-empty list")

        entry = accepts[0]
        for key in ("payTo", "maxAmountRequired"):
            if key not in entry:
                raise ValueError(f"Missing required field: body['accepts'][0]['{key}']")

        to = entry["payTo"]
        value = entry["maxAmountRequired"]

        if not (isinstance(to, str) and to.startswith("0x") and len(to) in {42, 66}):
            raise ValueError("payTo must be a valid Ethereum address string")
        if not isinstance(value, str):
            raise ValueError("maxAmountRequired must be a string (e.g., '0x...' or decimal string)")

    # ---------- Build x-payment header ----------
    def build_x_payment_header(self, body: dict[str, Any]) -> str:
        """Build and sign the x-payment header required for X402 requests."""
        self._validate_payment_body(body)

        now = datetime.now(timezone.utc)
        valid_after = int(now.timestamp())
        valid_before = valid_after + 900  # 15 minutes

        to: str = body["accepts"][0]["payTo"]
        value: str = body["accepts"][0]["maxAmountRequired"]
        nonce = "0x" + secrets.token_hex(32)

        authorization: dict[str, Any] = {
            "from": self.account.address,
            "nonce": nonce,
            "to": to,
            "validAfter": str(valid_after),
            "validBefore": str(valid_before),
            "value": value,
        }

        # Canonical JSON before signing
        auth_json_canonical: str = json.dumps(authorization, separators=(",", ":"), sort_keys=True)
        message = encode_defunct(text=auth_json_canonical)
        signed = self.account.sign_message(message)
        signature_hex = "0x" + signed.signature.hex()

        x_payment_payload: dict[str, Any] = {
            "network": self.network,
            "payload": {"authorization": authorization, "signature": signature_hex},
            "scheme": self.scheme,
            "x402Version": self.x402_version,
        }

        # Encode as Base64 JSON for header use
        x_payment_b64 = base64.b64encode(
            json.dumps(x_payment_payload, separators=(",", ":"), sort_keys=True).encode()
        ).decode()

        logger.info(f"x-payment header built; nonce={nonce[:10]}… valid for 15 minutes")
        return x_payment_b64

    # ---------- Mint once with retries ----------
    def _mint_once_with_retry(
        self,
        url: str,
        *,
        max_retries: int = 12,
        base_backoff: float = 0.4,
        backoff_cap: float = 6.0,
    ) -> Response:
        """Attempt minting once with automatic retry on transient failures."""
        start = time.time()
        attempt = 0

        while True:
            attempt += 1
            try:
                resp: Response = self.s.get(url, timeout=self.timeout)
            except (Timeout,) as e:
                logger.warning(f"Timeout (attempt {attempt}/{max_retries}): {e}")
            except (RequestException, PaymentError) as e:
                # PaymentError can be transient (e.g. busy gateway)
                logger.warning(f"Request/Payment error (attempt {attempt}/{max_retries}): {e}")
            else:
                code = resp.status_code
                if code == 200:
                    elapsed = (time.time() - start) * 1000
                    logger.info(f"Mint OK (HTTP 200) in {elapsed:.0f} ms")
                    return resp

                if code == 402:
                    # Payment Required – handled by x402 middleware; retry
                    logger.info(f"HTTP 402 (Payment Required); retrying ({attempt}/{max_retries})")
                elif code in {408, 425, 429, 500, 502, 503, 504}:
                    logger.info(f"HTTP {code}; transient server error; retrying ({attempt}/{max_retries})")
                elif code in {410, 404}:
                    # Permanent failure – do not retry
                    preview = resp.text[:200].replace("\n", " ")
                    logger.error(f"Permanent failure HTTP {code}; abort. Body: {preview!r}")
                    raise X402MintError(f"Permanent failure: {code}")
                else:
                    preview = resp.text[:200].replace("\n", " ")
                    logger.warning(f"Unexpected HTTP {code}; body: {preview!r} (retry if quota remains)")

            if attempt >= max_retries:
                raise X402MintError(f"Mint failed after {max_retries} attempts")

            self._sleep_with_backoff(attempt=attempt, base=base_backoff, cap=backoff_cap)

    # ---------- Public API ----------
    def mint(self, url: str, amount: int) -> list[dict[str, Any]]:
        """Perform multiple mint attempts and return all successful responses."""
        if amount <= 0:
            raise ValueError("amount must be > 0")

        results: list[dict[str, Any]] = []
        logger.info(f"Starting mint: {amount}x to {url}")

        for i in range(1, amount + 1):
            try:
                resp = self._mint_once_with_retry(url)
            except X402MintError as e:
                logger.error(f"Mint {i}/{amount} failed permanently: {e}")
                raise

            try:
                data: dict[str, Any] | None = resp.json()
            except ValueError:
                data = {"raw_text": resp.text}

            preview = str(data)[:160].replace("\n", " ")
            logger.info(f"Minted {i}/{amount} • preview: {preview!r}")
            results.append(data)

        logger.info(f"Done. Total successful mints: {len(results)}")
        return results
