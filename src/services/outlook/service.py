"""Outlook email service."""

import logging
import threading
import time
from typing import Optional, Dict, Any, List

from ..base import BaseEmailService, EmailServiceError, EmailServiceStatus, EmailServiceType
from ...config.constants import EmailServiceType as ServiceType
from ...config.settings import get_settings
from .account import OutlookAccount
from .base import ProviderType, EmailMessage
from .email_parser import EmailParser, get_email_parser
from .health_checker import HealthChecker, FailoverManager
from .providers.base import OutlookProvider, ProviderConfig
from .providers.imap_old import IMAPOldProvider
from .providers.imap_new import IMAPNewProvider
from .providers.graph_api import GraphAPIProvider


logger = logging.getLogger(__name__)


# 榛樿鎻愪緵鑰呬紭鍏堢骇
# IMAP_OLD 鏈€鍏煎锛堝彧闇€ login.live.com token锛夛紝IMAP_NEW 娆′箣锛孏raph API 鏈€鍚?# 鍘熷洜锛氶儴鍒?client_id 娌℃湁 Graph API 鏉冮檺锛屼絾鏈?IMAP 鏉冮檺
DEFAULT_PROVIDER_PRIORITY = [
    ProviderType.GRAPH_API,
]
IMAP_FALLBACK_PROVIDER_PRIORITY = [
    ProviderType.GRAPH_API,
    ProviderType.IMAP_NEW,
    ProviderType.IMAP_OLD,
]

OTP_TIME_SKEW_SECONDS = 5


def get_email_code_settings() -> dict:
    """Get email code polling settings."""
    settings = get_settings()
    return {
        "timeout": settings.email_code_timeout,
        "poll_interval": settings.email_code_poll_interval,
    }


class OutlookService(BaseEmailService):
    """Outlook email service with provider failover."""

    def __init__(self, config: Dict[str, Any] = None, name: str = None):
        """
        鍒濆鍖?Outlook 鏈嶅姟

        Args:
            config: 閰嶇疆瀛楀吀锛屾敮鎸佷互涓嬮敭:
                - accounts: Outlook 璐︽埛鍒楄〃
                - provider_priority: 鎻愪緵鑰呬紭鍏堢骇鍒楄〃
                - health_failure_threshold: 杩炵画澶辫触娆℃暟闃堝€?                - health_disable_duration: 绂佺敤鏃堕暱锛堢锛?                - timeout: 璇锋眰瓒呮椂鏃堕棿
                - proxy_url: 浠ｇ悊 URL
            name: 鏈嶅姟鍚嶇О
        """
        super().__init__(ServiceType.OUTLOOK, name)

        # 榛樿閰嶇疆
        default_config = {
            "accounts": [],
            "provider_priority": None,
            "allow_imap_fallback": False,
            "health_failure_threshold": 5,
            "health_disable_duration": 60,
            "timeout": 30,
            "proxy_url": None,
        }

        self.config = {**default_config, **(config or {})}

        allow_imap_fallback = self.config.get("allow_imap_fallback", False)
        if isinstance(allow_imap_fallback, str):
            allow_imap_fallback = allow_imap_fallback.strip().lower() in {"1", "true", "yes", "on"}
        self.allow_imap_fallback = bool(allow_imap_fallback)

        # 瑙ｆ瀽鎻愪緵鑰呬紭鍏堢骇
        self.provider_priority = [
            ProviderType(p) for p in (self.config.get("provider_priority") or [])
        ]
        if not self.provider_priority:
            self.provider_priority = list(
                IMAP_FALLBACK_PROVIDER_PRIORITY
                if self.allow_imap_fallback
                else DEFAULT_PROVIDER_PRIORITY
            )
        self.config["provider_priority"] = [p.value for p in self.provider_priority]
        self.config["allow_imap_fallback"] = self.allow_imap_fallback

        # Provider config
        self.provider_config = ProviderConfig(
            timeout=self.config.get("timeout", 30),
            proxy_url=self.config.get("proxy_url"),
            health_failure_threshold=self.config.get("health_failure_threshold", 3),
            health_disable_duration=self.config.get("health_disable_duration", 300),
        )

        # 鑾峰彇榛樿 client_id锛堜緵鏃?client_id 鐨勮处鎴蜂娇鐢級
        try:
            _default_client_id = get_settings().outlook_default_client_id
        except Exception:
            _default_client_id = "24d9a0ed-8787-4584-883c-2fd79308940a"

        # 瑙ｆ瀽璐︽埛
        self.accounts: List[OutlookAccount] = []
        self._current_account_index = 0
        self._account_lock = threading.Lock()

        # 鏀寔涓ょ閰嶇疆鏍煎紡
        if "email" in self.config and "password" in self.config:
            account = OutlookAccount.from_config(self.config)
            if not account.client_id and _default_client_id:
                account.client_id = _default_client_id
            if account.validate():
                self.accounts.append(account)
        else:
            for account_config in self.config.get("accounts", []):
                account = OutlookAccount.from_config(account_config)
                if not account.client_id and _default_client_id:
                    account.client_id = _default_client_id
                if account.validate():
                    self.accounts.append(account)

        if not self.accounts:
            logger.warning("鏈厤缃湁鏁堢殑 Outlook 璐︽埛")

        # 鍋ュ悍妫€鏌ュ櫒鍜屾晠闅滃垏鎹㈢鐞嗗櫒
        self.health_checker = HealthChecker(
            failure_threshold=self.provider_config.health_failure_threshold,
            disable_duration=self.provider_config.health_disable_duration,
        )
        self.failover_manager = FailoverManager(
            health_checker=self.health_checker,
            priority_order=self.provider_priority,
        )

        # 閭欢瑙ｆ瀽鍣?        self.email_parser = get_email_parser()

        # 鎻愪緵鑰呭疄渚嬬紦瀛? (email, provider_type) -> OutlookProvider
        self._providers: Dict[tuple, OutlookProvider] = {}
        self._provider_lock = threading.Lock()

        # IMAP 杩炴帴闄愬埗锛堥槻姝㈤檺娴侊級
        self._imap_semaphore = threading.Semaphore(5)

        # 楠岃瘉鐮佸幓閲嶆満鍒讹紙鎸夆€滄椂闂存埑+閭欢ID+楠岃瘉鐮佲€濇寚绾癸級
        self._used_codes: Dict[str, set] = {}
        # 楠岃瘉鐮侀樁娈垫爣璁帮紙鎸?otp_sent_at 閲嶇疆鍘婚噸锛岄伩鍏嶁€滅浜屽皝楠岃瘉鐮佷笌绗竴灏佺浉鍚屸€濊璇垽涓烘棫鐮侊級
        self._used_codes_stage_marker: Dict[str, int] = {}

    def _get_provider(
        self,
        account: OutlookAccount,
        provider_type: ProviderType,
    ) -> OutlookProvider:
        """
        鑾峰彇鎴栧垱寤烘彁渚涜€呭疄渚?
        Args:
            account: Outlook 璐︽埛
            provider_type: 鎻愪緵鑰呯被鍨?
        Returns:
            鎻愪緵鑰呭疄渚?        """
        cache_key = (account.email.lower(), provider_type)

        with self._provider_lock:
            if cache_key not in self._providers:
                provider = self._create_provider(account, provider_type)
                self._providers[cache_key] = provider

            return self._providers[cache_key]

    def _create_provider(
        self,
        account: OutlookAccount,
        provider_type: ProviderType,
    ) -> OutlookProvider:
        """
        鍒涘缓鎻愪緵鑰呭疄渚?
        Args:
            account: Outlook 璐︽埛
            provider_type: 鎻愪緵鑰呯被鍨?
        Returns:
            鎻愪緵鑰呭疄渚?        """
        if provider_type == ProviderType.IMAP_OLD:
            return IMAPOldProvider(account, self.provider_config)
        elif provider_type == ProviderType.IMAP_NEW:
            return IMAPNewProvider(account, self.provider_config)
        elif provider_type == ProviderType.GRAPH_API:
            return GraphAPIProvider(account, self.provider_config)
        else:
            raise ValueError(f"鏈煡鐨勬彁渚涜€呯被鍨? {provider_type}")

    def _get_provider_priority_for_account(self, account: OutlookAccount) -> List[ProviderType]:
        """兼容旧调用，转发到新逻辑。"""
        return self._resolve_provider_priority_for_account(account)

    def _resolve_provider_priority_for_account(self, account: OutlookAccount) -> List[ProviderType]:
        """Return the allowed provider priority for this account."""
        if account.has_oauth():
            return list(self.provider_priority)

        if not self.allow_imap_fallback:
            logger.warning(
                "[%s] No enabled provider is available for this account, skipping this poll cycle",
                account.email,
            )
            return []

        imap_priority = [
            provider_type
            for provider_type in self.provider_priority
            if provider_type in (ProviderType.IMAP_NEW, ProviderType.IMAP_OLD)
        ]
        if not imap_priority:
            imap_priority = [ProviderType.IMAP_OLD]
        return imap_priority

    def _try_providers_for_emails(
        self,
        account: OutlookAccount,
        count: int = 20,
        only_unseen: bool = True,
    ) -> List[EmailMessage]:
        """Try providers in priority order and return recent emails."""
        errors = []

        priority = self._resolve_provider_priority_for_account(account)
        if not priority:
            logger.warning(
                "[%s] No enabled provider is available for this account, skipping this poll cycle",
                account.email,
            )
            return []

        for provider_type in priority:
            if not self.health_checker.is_available(provider_type):
                logger.debug(
                    "[%s] Provider %s is unavailable, skipping",
                    account.email,
                    provider_type.value,
                )
                continue

            try:
                provider = self._get_provider(account, provider_type)
                with self._imap_semaphore:
                    with provider:
                        emails = provider.get_recent_emails(count, only_unseen)

                if emails:
                    self.health_checker.record_success(provider_type)
                    logger.debug(
                        "[%s] Provider %s fetched %s emails",
                        account.email,
                        provider_type.value,
                        len(emails),
                    )
                    return emails
            except Exception as e:
                error_msg = str(e)
                errors.append(f"{provider_type.value}: {error_msg}")
                self.health_checker.record_failure(provider_type, error_msg)
                logger.warning(
                    "[%s] Provider %s failed to fetch emails: %s",
                    account.email,
                    provider_type.value,
                    e,
                )

        logger.error("[%s] All providers failed: %s", account.email, '; '.join(errors))
        return []

    def create_email(self, config: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        閫夋嫨鍙敤鐨?Outlook 璐︽埛

        Args:
            config: 閰嶇疆鍙傛暟锛堟湭浣跨敤锛?
        Returns:
            鍖呭惈閭淇℃伅鐨勫瓧鍏?        """
        if not self.accounts:
            self.update_status(False, EmailServiceError("娌℃湁鍙敤鐨?Outlook 璐︽埛"))
            raise EmailServiceError("娌℃湁鍙敤鐨?Outlook 璐︽埛")

        # 杞閫夋嫨璐︽埛
        with self._account_lock:
            account = self.accounts[self._current_account_index]
            self._current_account_index = (self._current_account_index + 1) % len(self.accounts)

        email_info = {
            "email": account.email,
            "service_id": account.email,
            "account": {
                "email": account.email,
                "has_oauth": account.has_oauth()
            }
        }

        logger.info(f"閫夋嫨 Outlook 璐︽埛: {account.email}")
        self.update_status(True)
        return email_info

    def get_verification_code(
        self,
        email: str,
        email_id: str = None,
        timeout: int = None,
        pattern: str = None,
        otp_sent_at: Optional[float] = None,
    ) -> Optional[str]:
        """Poll Outlook providers and extract the latest verification code."""
        account = None
        normalized_email = str(email or "").strip().lower()
        normalized_email_id = str(email_id or "").strip().lower()

        for candidate in self.accounts:
            candidate_email = str(candidate.email or "").strip().lower()
            if candidate_email == normalized_email or (normalized_email_id and candidate_email == normalized_email_id):
                account = candidate
                break

        if not account:
            self.update_status(False, EmailServiceError(f"未找到对应的 Outlook 账户: {email}"))
            return None

        code_settings = get_email_code_settings()
        actual_timeout = timeout or code_settings["timeout"]
        poll_interval = code_settings["poll_interval"]

        logger.info(
            "[%s] 开始获取验证码，超时 %ss，提供者优先级: %s",
            email,
            actual_timeout,
            [p.value for p in self.provider_priority],
        )

        if normalized_email not in self._used_codes:
            self._used_codes[normalized_email] = set()
        used_fingerprints = self._used_codes[normalized_email]

        if otp_sent_at:
            try:
                stage_marker = int(float(otp_sent_at))
                prev_marker = self._used_codes_stage_marker.get(normalized_email)
                if prev_marker is None or abs(stage_marker - prev_marker) > 3:
                    if used_fingerprints:
                        logger.info(
                            "[%s] Detected a new OTP stage, clearing %s cached fingerprints",
                            email,
                            len(used_fingerprints),
                        )
                    used_fingerprints.clear()
                    self._used_codes_stage_marker[normalized_email] = stage_marker
            except Exception:
                pass

        min_timestamp = (otp_sent_at - OTP_TIME_SKEW_SECONDS) if otp_sent_at else 0
        start_time = time.time()
        poll_count = 0

        while time.time() - start_time < actual_timeout:
            poll_count += 1
            only_unseen = poll_count <= 3

            try:
                emails = self._try_providers_for_emails(
                    account,
                    count=15,
                    only_unseen=only_unseen,
                )
                if emails:
                    code = self.email_parser.find_verification_code_in_emails(
                        emails,
                        target_email=email,
                        min_timestamp=min_timestamp,
                        used_fingerprints=used_fingerprints,
                    )
                    if code:
                        logger.info(
                            "[%s] 找到验证码: %s，总耗时 %ss，轮询 %s 次",
                            email,
                            code,
                            int(time.time() - start_time),
                            poll_count,
                        )
                        self.update_status(True)
                        return code
            except Exception as e:
                logger.warning("[%s] 获取验证码时发生异常: %s", email, e)

            time.sleep(poll_interval)

        logger.warning(
            "[%s] 验证码超时 (%ss)，共轮询 %s 次",
            email,
            actual_timeout,
            poll_count,
        )
        self.update_status(False, EmailServiceError(f"等待验证码超时: {email}"))
        return None

    def check_health(self) -> bool:
        """Run a lightweight provider connectivity check."""
        if not self.accounts:
            self.update_status(False, EmailServiceError("没有配置 Outlook 账户"))
            return False

        test_account = self.accounts[0]
        for provider_type in self.provider_priority:
            try:
                provider = self._get_provider(test_account, provider_type)
                if provider.test_connection():
                    self.update_status(True)
                    return True
            except Exception as e:
                logger.warning("[%s] Provider %s health check failed: %s", test_account.email, provider_type.value, e)

        self.update_status(False, EmailServiceError("所有 Outlook provider 检测均失败"))
        return False

    def get_provider_status(self) -> Dict[str, Any]:
        """Return current provider status and configured accounts."""
        total = len(self.accounts)
        oauth_count = sum(1 for acc in self.accounts if acc.has_oauth())
        return {
            "total_accounts": total,
            "oauth_accounts": oauth_count,
            "password_accounts": total - oauth_count,
            "accounts": [acc.to_dict() for acc in self.accounts],
            "provider_status": self.get_provider_status_map(),
        }

    def get_provider_status_map(self) -> Dict[str, Any]:
        """Expose health checker provider status in a serializable shape."""
        return self.health_checker.get_all_health_status()

    def add_account(self, account_config: Dict[str, Any]) -> bool:
        """Add a new Outlook account."""
        try:
            account = OutlookAccount.from_config(account_config)
            if not account.validate():
                return False
            self.accounts.append(account)
            logger.info("Added Outlook account: %s", account.email)
            return True
        except Exception as e:
            logger.error("Failed to add Outlook account: %s", e)
            return False

    def remove_account(self, email: str) -> bool:
        """Remove an Outlook account by email."""
        for i, acc in enumerate(self.accounts):
            if acc.email.lower() == email.lower():
                self.accounts.pop(i)
                logger.info("Removed Outlook account: %s", email)
                return True
        return False

    def reset_provider_health(self):
        """Reset provider health state."""
        self.health_checker.reset_all()
        logger.info("Reset all Outlook provider health state")

    def force_provider(self, provider_type: ProviderType):
        """Force-enable a provider in the health checker."""
        self.health_checker.force_enable(provider_type)
