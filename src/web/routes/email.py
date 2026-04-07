"""
邮箱服务配置 API 路由
"""

import logging
import uuid
from urllib.parse import urlparse
from typing import List, Optional, Dict, Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func

from ...database import crud
from ...database.session import get_db
from ...database.models import EmailService as EmailServiceModel
from ...database.models import Account as AccountModel
from ...config.settings import get_settings
from ...config.constants import OPENAI_PAGE_TYPES
from ...core.anyauto.oauth_client import OAuthClient
from ...core.anyauto.register_flow import EmailServiceAdapter
from ...core.anyauto.utils import decode_jwt_payload
from ...core.openai.overview import fetch_codex_overview, AccountDeactivatedError
from ...core.register import RegistrationEngine
from ...core.timezone_utils import utcnow_naive
from ...services import EmailServiceFactory, EmailServiceType

logger = logging.getLogger(__name__)
router = APIRouter()


# ============== Pydantic Models ==============

class EmailServiceCreate(BaseModel):
    """创建邮箱服务请求"""
    service_type: str
    name: str
    config: Dict[str, Any]
    enabled: bool = True
    priority: int = 0


class EmailServiceUpdate(BaseModel):
    """更新邮箱服务请求"""
    name: Optional[str] = None
    config: Optional[Dict[str, Any]] = None
    enabled: Optional[bool] = None
    priority: Optional[int] = None


class EmailServiceResponse(BaseModel):
    """??????"""
    id: int
    service_type: str
    name: str
    enabled: bool
    priority: int
    config: Optional[Dict[str, Any]] = None  # ??????????
    registration_status: Optional[str] = None
    registered_account_id: Optional[int] = None
    last_used: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class EmailServiceListResponse(BaseModel):
    """邮箱服务列表响应"""
    total: int
    services: List[EmailServiceResponse]


class ServiceTestResult(BaseModel):
    """服务测试结果"""
    success: bool
    message: str
    details: Optional[Dict[str, Any]] = None


class OutlookBatchImportRequest(BaseModel):
    """Outlook 批量导入请求"""
    data: str  # 多行数据，每行格式: 邮箱----密码 或 邮箱----密码----client_id----refresh_token
    enabled: bool = True
    priority: int = 0
    allow_imap_fallback: bool = False


class OutlookBatchImportResponse(BaseModel):
    """Outlook 批量导入响应"""
    total: int
    success: int
    failed: int
    accounts: List[Dict[str, Any]]
    errors: List[str]


class OutlookRegistrationCheckRequest(BaseModel):
    """Outlook 注册快检请求"""
    service_ids: List[int]
    remote_probe: bool = True


class OutlookRegistrationCheckItem(BaseModel):
    """Outlook 注册快检结果"""
    service_id: int
    email: str
    suitable: bool
    verdict: str
    summary: str
    local: Dict[str, Any]
    probe: Optional[Dict[str, Any]] = None


class OutlookRegistrationCheckResponse(BaseModel):
    """Outlook 注册快检批量响应"""
    total: int
    results: List[OutlookRegistrationCheckItem]


class OutlookOtpLoginRepairResponse(BaseModel):
    """Outlook 手动 OTP 补登录响应"""
    service_id: int
    email: str
    success: bool
    message: str
    account_id: Optional[int] = None
    created: bool = False
    updated: bool = False
    workspace_id: Optional[str] = None
    subscription_type: Optional[str] = None
    plan_type: Optional[str] = None
    overview_refreshed: bool = False
    session_token_found: bool = False
    logs: List[str] = []


# ============== Helper Functions ==============

# 敏感字段列表，返回响应时需要过滤
SENSITIVE_FIELDS = {
    'password',
    'api_key',
    'refresh_token',
    'access_token',
    'admin_token',
    'admin_password',
    'custom_auth',
}

OVERVIEW_EXTRA_DATA_KEY = "codex_overview"

def normalize_email_service_config(service_type: str, config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """兼容历史配置字段，避免不同入口写入的键名不一致。"""
    normalized = dict(config or {})

    if service_type in {"temp_mail", "cloudmail", "freemail"}:
        if normalized.get("default_domain") and not normalized.get("domain"):
            normalized["domain"] = normalized.pop("default_domain")

    if service_type == "cloudmail" and normalized.get("api_key") and not normalized.get("admin_password"):
        normalized["admin_password"] = normalized.pop("api_key")

    return normalized


def filter_sensitive_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """过滤敏感配置信息"""
    if not config:
        return {}

    filtered = {}
    for key, value in config.items():
        if key in SENSITIVE_FIELDS:
            # 敏感字段不返回，但标记是否存在
            filtered[f"has_{key}"] = bool(value)
        else:
            filtered[key] = value

    # 为 Outlook 计算是否有 OAuth
    if config.get('client_id') and config.get('refresh_token'):
        filtered['has_oauth'] = True

    return filtered


def service_to_response(service: EmailServiceModel) -> EmailServiceResponse:
    """?????????"""
    normalized_config = normalize_email_service_config(service.service_type, service.config)
    registration_status = None
    registered_account_id = None
    if service.service_type == "outlook":
        email = str(normalized_config.get("email") or service.name or "").strip()
        normalized_email = email.lower()
        if email:
            with get_db() as db:
                account = (
                    db.query(AccountModel)
                    .filter(func.lower(AccountModel.email) == normalized_email)
                    .first()
                )
            if account:
                registration_status = "registered"
                registered_account_id = account.id
            else:
                registration_status = "unregistered"

    return EmailServiceResponse(
        id=service.id,
        service_type=service.service_type,
        name=service.name,
        enabled=service.enabled,
        priority=service.priority,
        config=filter_sensitive_config(normalized_config),
        registration_status=registration_status,
        registered_account_id=registered_account_id,
        last_used=service.last_used.isoformat() if service.last_used else None,
        created_at=service.created_at.isoformat() if service.created_at else None,
        updated_at=service.updated_at.isoformat() if service.updated_at else None,
    )


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _normalize_oauth_issuer(auth_url: Optional[str]) -> str:
    text = str(auth_url or "").strip()
    if not text:
        return "https://auth.openai.com"
    parsed = urlparse(text)
    if not parsed.scheme or not parsed.netloc:
        return text.rstrip("/")
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


def _extract_session_token_from_cookie_jar(cookie_jar) -> str:
    if not cookie_jar:
        return ""
    try:
        for cookie in cookie_jar:
            name = str(getattr(cookie, "name", "") or "").strip()
            value = str(getattr(cookie, "value", "") or "").strip()
            if name == "__Secure-next-auth.session-token" and value:
                return value
    except Exception:
        return ""
    return ""


def _dump_cookie_text_from_jar(cookie_jar) -> str:
    if not cookie_jar:
        return ""
    pairs: List[str] = []
    seen: set[str] = set()
    try:
        for cookie in cookie_jar:
            name = str(getattr(cookie, "name", "") or "").strip()
            value = str(getattr(cookie, "value", "") or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            pairs.append(f"{name}={value}")
    except Exception:
        return ""
    return "; ".join(pairs)


def _extract_openai_identity_from_token(token: Optional[str]) -> Dict[str, str]:
    payload = decode_jwt_payload(token)
    if not isinstance(payload, dict):
        return {"account_id": "", "workspace_id": ""}

    auth_claims = payload.get("https://api.openai.com/auth") or {}
    account_id = ""
    workspace_id = ""

    for key in ("chatgpt_account_id", "account_id", "workspace_id"):
        value = str(auth_claims.get(key) or payload.get(key) or "").strip()
        if value:
            account_id = value
            break

    for key in ("workspace_id", "chatgpt_account_id", "account_id"):
        value = str(auth_claims.get(key) or payload.get(key) or "").strip()
        if value:
            workspace_id = value
            break

    return {
        "account_id": account_id,
        "workspace_id": workspace_id,
    }


def _extract_workspace_id_from_oauth_session(oauth_client: OAuthClient) -> str:
    try:
        session_data = oauth_client._decode_oauth_session_cookie()
    except Exception:
        session_data = None

    if not isinstance(session_data, dict):
        return ""

    workspaces = session_data.get("workspaces") or []
    if workspaces and isinstance(workspaces[0], dict):
        return str(workspaces[0].get("id") or "").strip()
    return ""


def _plan_to_subscription_type(plan_type: Optional[str]) -> Optional[str]:
    key = str(plan_type or "").strip().lower()
    if key.startswith("team"):
        return "team"
    if key.startswith("plus") or key.startswith("pro"):
        return "plus"
    return None


def _collect_outlook_registration_check(
    db,
    service: EmailServiceModel,
    *,
    remote_probe: bool = True,
) -> OutlookRegistrationCheckItem:
    config = normalize_email_service_config(service.service_type, service.config)
    email = str(config.get("email") or service.name or "").strip()
    normalized_email = email.lower()
    has_oauth = bool(config.get("client_id") and config.get("refresh_token"))
    allow_imap_fallback = _as_bool(config.get("allow_imap_fallback"))
    existing_account = None
    if normalized_email:
        existing_account = (
            db.query(AccountModel)
            .filter(func.lower(AccountModel.email) == normalized_email)
            .first()
        )
    resume_task = crud.get_latest_registration_resume_task(
        db,
        email,
        email_service_id=service.id,
    ) if email else None
    resume_result = resume_task.result if resume_task and isinstance(resume_task.result, dict) else {}
    resume_meta = resume_result.get("metadata") if isinstance(resume_result.get("metadata"), dict) else {}

    local = {
        "enabled": bool(service.enabled),
        "has_oauth": has_oauth,
        "allow_imap_fallback": allow_imap_fallback,
        "mailbox_ready": bool(has_oauth or allow_imap_fallback),
        "registered_account_id": getattr(existing_account, "id", None),
        "registration_status": "registered" if existing_account else "unregistered",
        "pending_resume": bool(resume_task),
        "pending_resume_state": str(resume_meta.get("resume_state") or "").strip(),
        "pending_resume_task_uuid": str(getattr(resume_task, "task_uuid", "") or "").strip(),
    }

    def _item(
        suitable: bool,
        verdict: str,
        summary: str,
        probe: Optional[Dict[str, Any]] = None,
    ) -> OutlookRegistrationCheckItem:
        return OutlookRegistrationCheckItem(
            service_id=service.id,
            email=email,
            suitable=suitable,
            verdict=verdict,
            summary=summary,
            local=local,
            probe=probe,
        )

    if service.service_type != "outlook":
        return _item(False, "unsupported_service", "仅支持 Outlook 账号快检")
    if not service.enabled:
        return _item(False, "service_disabled", "邮箱服务已禁用，不适合发起注册")
    if not email:
        return _item(False, "missing_email", "邮箱地址缺失，请先补全配置")
    if existing_account:
        return _item(False, "registered_local", "本地账号库已存在该邮箱，不建议再次注册")
    if resume_task:
        return _item(False, "pending_resume", "存在未完成注册记录，建议优先走续登/补会话")
    if not local["mailbox_ready"]:
        return _item(False, "mailbox_unavailable", "当前为 Graph Only 且缺少 OAuth，无法用于注册收件")
    if not remote_probe:
        return _item(True, "local_ready", "本地条件通过，可尝试发起注册")

    probe_logs: List[str] = []
    probe_payload: Dict[str, Any] = {
        "ok": False,
        "page_type": "",
        "logs": probe_logs,
    }
    try:
        from .registration import get_proxy_for_registration

        proxy_url, _proxy_id = get_proxy_for_registration(db)
        email_service = EmailServiceFactory.create(
            EmailServiceType.OUTLOOK,
            config,
            name=f"outlook_probe_{service.id}",
        )
        engine = RegistrationEngine(
            email_service=email_service,
            proxy_url=proxy_url,
            callback_logger=lambda msg: probe_logs.append(str(msg or "").strip()[:200]),
        )
        engine.email_info = {"email": email, "service_id": service.id}
        engine.email = email
        engine.inbox_email = email

        did, sen_token = engine._prepare_authorize_flow("快检")
        if not did:
            probe_payload["error"] = "device_id_unavailable"
            return _item(False, "probe_failed", "快检初始化失败，未拿到 Device ID", probe=probe_payload)

        result = engine._submit_signup_form(did, sen_token, record_existing_account=False)
        probe_payload["ok"] = bool(result.success)
        probe_payload["page_type"] = str(result.page_type or "").strip()
        if result.error_message:
            probe_payload["error"] = str(result.error_message)

        page_type = probe_payload["page_type"]
        if not result.success:
            return _item(False, "probe_failed", "OpenAI 探测失败，请稍后重试", probe=probe_payload)
        if page_type == OPENAI_PAGE_TYPES.get("PASSWORD_REGISTRATION", "create_account_password"):
            return _item(True, "ready_register", "OpenAI 探测通过，当前邮箱可尝试注册", probe=probe_payload)
        if page_type == OPENAI_PAGE_TYPES.get("LOGIN_PASSWORD", "login_password"):
            return _item(False, "registered_remote", "OpenAI 返回登录密码页，疑似该邮箱已存在", probe=probe_payload)
        if page_type == OPENAI_PAGE_TYPES.get("EMAIL_OTP_VERIFICATION", "email_otp_verification"):
            return _item(False, "registered_remote_otp", "OpenAI 已进入登录验证码页，该邮箱大概率已注册", probe=probe_payload)
        return _item(False, "probe_unknown", f"OpenAI 返回未知页面类型: {page_type or '-'}", probe=probe_payload)
    except Exception as e:
        probe_payload["error"] = str(e)
        return _item(False, "probe_exception", f"快检异常: {e}", probe=probe_payload)


def _run_outlook_otp_login_repair(
    db,
    service: EmailServiceModel,
) -> OutlookOtpLoginRepairResponse:
    config = normalize_email_service_config(service.service_type, service.config)
    email = str(config.get("email") or service.name or "").strip().lower()
    allow_imap_fallback = _as_bool(config.get("allow_imap_fallback"))
    has_oauth = bool(config.get("client_id") and config.get("refresh_token"))

    if service.service_type != "outlook":
        raise HTTPException(status_code=400, detail="仅支持 Outlook 账号 OTP 补登录")
    if not service.enabled:
        raise HTTPException(status_code=400, detail="当前 Outlook 服务已禁用，请先启用后再试")
    if not email:
        raise HTTPException(status_code=400, detail="当前 Outlook 服务缺少邮箱地址")
    if not has_oauth and not allow_imap_fallback:
        raise HTTPException(
            status_code=400,
            detail="当前邮箱默认仅走 Graph，但缺少 OAuth 且 IMAP 回退未开启，无法自动收取 OTP",
        )

    from .registration import get_proxy_for_registration

    proxy_url, _proxy_id = get_proxy_for_registration(db)
    logs: List[str] = []

    def _log(message: str) -> None:
        text = str(message or "").strip()
        if text:
            logs.append(text[:300])

    try:
        email_service = EmailServiceFactory.create(
            EmailServiceType.OUTLOOK,
            config,
            name=f"outlook_otp_login_repair_{service.id}",
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"创建邮箱服务失败: {exc}") from exc

    settings = get_settings()
    oauth_client = OAuthClient(
        config={
            "oauth_issuer": _normalize_oauth_issuer(getattr(settings, "openai_auth_url", None)),
            "oauth_client_id": str(getattr(settings, "openai_client_id", "") or "app_EMoamEEZ73f0CkXaXp7hrann"),
            "oauth_redirect_uri": str(getattr(settings, "openai_redirect_uri", "") or "http://localhost:1455/auth/callback"),
        },
        proxy=proxy_url,
        verbose=False,
        browser_mode="protocol",
    )
    oauth_client._log = _log

    adapter = EmailServiceAdapter(email_service, email, email, _log)
    device_id = str(uuid.uuid4())
    _log(f"OTP补登启动，邮箱: {email}")
    _log(f"Device ID: {device_id}")

    tokens = oauth_client.login_passwordless_and_get_tokens(
        email,
        device_id,
        skymail_client=adapter,
    )
    if not tokens or not tokens.get("access_token"):
        raise HTTPException(
            status_code=400,
            detail=str(oauth_client.last_error or "OTP 补登录失败，未获取到 access_token"),
        )

    session = getattr(oauth_client, "session", None)
    access_token = str(tokens.get("access_token") or "").strip()
    refresh_token = str(tokens.get("refresh_token") or "").strip()
    id_token = str(tokens.get("id_token") or "").strip()
    session_token = _extract_session_token_from_cookie_jar(getattr(session, "cookies", None))
    cookies_text = _dump_cookie_text_from_jar(getattr(session, "cookies", None))

    identity = _extract_openai_identity_from_token(access_token or id_token)
    workspace_id = _extract_workspace_id_from_oauth_session(oauth_client) or identity["workspace_id"] or identity["account_id"]
    remote_account_id = identity["account_id"] or workspace_id

    existing_account = (
        db.query(AccountModel)
        .filter(func.lower(AccountModel.email) == email)
        .first()
    )

    now = utcnow_naive()
    extra_data = dict(getattr(existing_account, "extra_data", None) or {})
    extra_data["otp_login_repair"] = {
        "service_id": service.id,
        "email_service": "outlook",
        "proxy_used": proxy_url,
        "updated_at": now.isoformat(),
        "session_token_found": bool(session_token),
    }

    created = False
    updated = False
    if existing_account:
        existing_account.access_token = access_token
        existing_account.refresh_token = refresh_token or existing_account.refresh_token
        existing_account.id_token = id_token or existing_account.id_token
        existing_account.session_token = session_token or existing_account.session_token
        existing_account.cookies = cookies_text or existing_account.cookies
        existing_account.client_id = str(config.get("client_id") or existing_account.client_id or "").strip() or None
        existing_account.account_id = remote_account_id or existing_account.account_id
        existing_account.workspace_id = workspace_id or existing_account.workspace_id
        existing_account.email_service = "outlook"
        existing_account.email_service_id = str(service.id)
        existing_account.proxy_used = proxy_url
        existing_account.status = "active"
        existing_account.source = existing_account.source or "login"
        existing_account.last_refresh = now
        existing_account.extra_data = extra_data
        db.commit()
        db.refresh(existing_account)
        account = existing_account
        updated = True
    else:
        account = crud.create_account(
            db,
            email=email,
            password=None,
            client_id=str(config.get("client_id") or "").strip() or None,
            session_token=session_token or None,
            email_service="outlook",
            email_service_id=str(service.id),
            account_id=remote_account_id or None,
            workspace_id=workspace_id or None,
            access_token=access_token,
            refresh_token=refresh_token or None,
            id_token=id_token or None,
            cookies=cookies_text or None,
            proxy_used=proxy_url,
            extra_data=extra_data,
            status="active",
            source="login",
        )
        account.last_refresh = now
        db.commit()
        db.refresh(account)
        created = True

    overview_refreshed = False
    plan_type = None
    subscription_type = str(getattr(account, "subscription_type", "") or "").strip() or None
    try:
        overview = fetch_codex_overview(account, proxy=proxy_url)
        if isinstance(overview, dict):
            plan_type = str(overview.get("plan_type") or "").strip() or None
            detected_sub = _plan_to_subscription_type(plan_type)
            merged_extra = dict(account.extra_data or {})
            merged_extra[OVERVIEW_EXTRA_DATA_KEY] = overview
            account.extra_data = merged_extra
            if detected_sub:
                account.subscription_type = detected_sub
                account.subscription_at = utcnow_naive()
                subscription_type = detected_sub
            account.last_refresh = utcnow_naive()
            db.commit()
            db.refresh(account)
            overview_refreshed = True
    except AccountDeactivatedError as exc:
        account.status = "banned"
        merged_extra = dict(account.extra_data or {})
        merged_extra["otp_login_repair_overview_error"] = str(exc)
        account.extra_data = merged_extra
        db.commit()
        db.refresh(account)
        _log(f"总览同步命中停用状态: {exc}")
    except Exception as exc:
        _log(f"总览同步失败，已保留登录结果: {exc}")

    message = "OTP补登成功，账号信息已同步"
    if not overview_refreshed:
        message = "OTP补登成功，但总览同步未完成"

    return OutlookOtpLoginRepairResponse(
        service_id=service.id,
        email=email,
        success=True,
        message=message,
        account_id=account.id,
        created=created,
        updated=updated,
        workspace_id=str(account.workspace_id or "").strip() or None,
        subscription_type=subscription_type,
        plan_type=plan_type,
        overview_refreshed=overview_refreshed,
        session_token_found=bool(session_token),
        logs=logs[-30:],
    )


# ============== API Endpoints ==============

@router.get("/stats")
async def get_email_services_stats():
    """获取邮箱服务统计信息"""
    with get_db() as db:
        # 按类型统计
        type_stats = db.query(
            EmailServiceModel.service_type,
            func.count(EmailServiceModel.id)
        ).group_by(EmailServiceModel.service_type).all()

        # 启用数量
        enabled_count = db.query(func.count(EmailServiceModel.id)).filter(
            EmailServiceModel.enabled == True
        ).scalar()

        settings = get_settings()
        tempmail_enabled = bool(settings.tempmail_enabled)
        yyds_enabled = bool(
            settings.yyds_mail_enabled
            and settings.yyds_mail_api_key
            and settings.yyds_mail_api_key.get_secret_value()
        )

        stats = {
            'outlook_count': 0,
            'custom_count': 0,
            'tempmail_builtin_count': 0,
            'yyds_mail_count': 0,
            'temp_mail_count': 0,
            'duck_mail_count': 0,
            'freemail_count': 0,
            'imap_mail_count': 0,
            'cloudmail_count': 0,
            'luckmail_count': 0,
            'tempmail_available': tempmail_enabled or yyds_enabled,
            'yyds_mail_available': yyds_enabled,
            'enabled_count': enabled_count
        }

        for service_type, count in type_stats:
            if service_type == 'outlook':
                stats['outlook_count'] = count
            elif service_type == 'moe_mail':
                stats['custom_count'] = count
            elif service_type == 'tempmail':
                stats['tempmail_builtin_count'] = count
            elif service_type == 'yyds_mail':
                stats['yyds_mail_count'] = count
            elif service_type == 'temp_mail':
                stats['temp_mail_count'] = count
            elif service_type == 'duck_mail':
                stats['duck_mail_count'] = count
            elif service_type == 'freemail':
                stats['freemail_count'] = count
            elif service_type == 'imap_mail':
                stats['imap_mail_count'] = count
            elif service_type == 'cloudmail':
                stats['cloudmail_count'] = count
            elif service_type == 'luckmail':
                stats['luckmail_count'] = count

        return stats


@router.get("/types")
async def get_service_types():
    """获取支持的邮箱服务类型"""
    return {
        "types": [
            {
                "value": "tempmail",
                "label": "Tempmail.lol",
                "description": "官方内置临时邮箱渠道，通过全局配置使用",
                "config_fields": [
                    {"name": "base_url", "label": "API 地址", "default": "https://api.tempmail.lol/v2", "required": False},
                    {"name": "timeout", "label": "超时时间", "default": 30, "required": False},
                ]
            },
            {
                "value": "yyds_mail",
                "label": "YYDS Mail",
                "description": "官方内置临时邮箱渠道，使用 X-API-Key 创建邮箱并轮询消息",
                "config_fields": [
                    {"name": "base_url", "label": "API 地址", "default": "https://maliapi.215.im/v1", "required": False},
                    {"name": "api_key", "label": "API Key", "required": True, "secret": True},
                    {"name": "default_domain", "label": "默认域名", "required": False, "placeholder": "public.example.com"},
                    {"name": "timeout", "label": "超时时间", "default": 30, "required": False},
                ]
            },
            {
                "value": "outlook",
                "label": "Outlook",
                "description": "Outlook 邮箱，需要配置账户信息",
                "config_fields": [
                    {"name": "email", "label": "邮箱地址", "required": True},
                    {"name": "password", "label": "密码", "required": True},
                    {"name": "client_id", "label": "OAuth Client ID", "required": False},
                    {"name": "refresh_token", "label": "OAuth Refresh Token", "required": False},
                ]
            },
            {
                "value": "moe_mail",
                "label": "MoeMail",
                "description": "自定义域名邮箱服务",
                "config_fields": [
                    {"name": "base_url", "label": "API 地址", "required": True},
                    {"name": "api_key", "label": "API Key", "required": True},
                    {"name": "default_domain", "label": "默认域名", "required": False},
                ]
            },
            {
                "value": "temp_mail",
                "label": "Temp-Mail（自部署）",
                "description": "自部署 Cloudflare Worker 临时邮箱，admin 模式管理",
                "config_fields": [
                    {"name": "base_url", "label": "Worker 地址", "required": True, "placeholder": "https://mail.example.com"},
                    {"name": "admin_password", "label": "Admin 密码", "required": True, "secret": True},
                    {"name": "custom_auth", "label": "Custom Auth（可选）", "required": False, "secret": True},
                    {"name": "domain", "label": "邮箱域名", "required": True, "placeholder": "example.com"},
                    {"name": "enable_prefix", "label": "启用前缀", "required": False, "default": True},
                ]
            },
            {
                "value": "duck_mail",
                "label": "DuckMail",
                "description": "DuckMail 接口邮箱服务，支持 API Key 私有域名访问",
                "config_fields": [
                    {"name": "base_url", "label": "API 地址", "required": True, "placeholder": "https://api.duckmail.sbs"},
                    {"name": "default_domain", "label": "默认域名", "required": True, "placeholder": "duckmail.sbs"},
                    {"name": "api_key", "label": "API Key", "required": False, "secret": True},
                    {"name": "password_length", "label": "随机密码长度", "required": False, "default": 12},
                ]
            },
            {
                "value": "freemail",
                "label": "Freemail",
                "description": "Freemail 自部署 Cloudflare Worker 临时邮箱服务",
                "config_fields": [
                    {"name": "base_url", "label": "API 地址", "required": True, "placeholder": "https://freemail.example.com"},
                    {"name": "admin_token", "label": "Admin Token", "required": True, "secret": True},
                    {"name": "domain", "label": "邮箱域名", "required": False, "placeholder": "example.com"},
                ]
            },
            {
                "value": "cloudmail",
                "label": "CloudMail",
                "description": "CloudMail 自部署 Cloudflare Worker 邮箱服务，使用管理口令创建邮箱并轮询验证码",
                "config_fields": [
                    {"name": "base_url", "label": "API 地址", "required": True, "placeholder": "https://cloudmail.example.com"},
                    {"name": "admin_password", "label": "Admin 密码", "required": True, "secret": True},
                    {"name": "domain", "label": "邮箱域名", "required": True, "placeholder": "example.com"},
                    {"name": "enable_prefix", "label": "启用前缀", "required": False, "default": True},
                    {"name": "timeout", "label": "超时时间", "required": False, "default": 30},
                ]
            },
            {
                "value": "imap_mail",
                "label": "IMAP 邮箱",
                "description": "标准 IMAP 协议邮箱（Gmail/QQ/163等），仅用于接收验证码，强制直连",
                "config_fields": [
                    {"name": "host", "label": "IMAP 服务器", "required": True, "placeholder": "imap.gmail.com"},
                    {"name": "port", "label": "端口", "required": False, "default": 993},
                    {"name": "use_ssl", "label": "使用 SSL", "required": False, "default": True},
                    {"name": "email", "label": "邮箱地址", "required": True},
                    {"name": "password", "label": "密码/授权码", "required": True, "secret": True},
                ]
            },
            {
                "value": "luckmail",
                "label": "LuckMail",
                "description": "LuckMail 接码服务（下单 + 轮询验证码）",
                "config_fields": [
                    {"name": "base_url", "label": "平台地址", "required": False, "default": "https://mails.luckyous.com/"},
                    {"name": "api_key", "label": "API Key", "required": True, "secret": True},
                    {"name": "project_code", "label": "项目编码", "required": False, "default": "openai"},
                    {"name": "email_type", "label": "邮箱类型", "required": False, "default": "ms_graph"},
                    {"name": "preferred_domain", "label": "优先域名", "required": False, "placeholder": "outlook.com"},
                    {"name": "poll_interval", "label": "轮询间隔(秒)", "required": False, "default": 3.0},
                ]
            }
        ]
    }


@router.get("", response_model=EmailServiceListResponse)
async def list_email_services(
    service_type: Optional[str] = Query(None, description="服务类型筛选"),
    enabled_only: bool = Query(False, description="只显示启用的服务"),
):
    """获取邮箱服务列表"""
    with get_db() as db:
        query = db.query(EmailServiceModel)

        if service_type:
            query = query.filter(EmailServiceModel.service_type == service_type)

        if enabled_only:
            query = query.filter(EmailServiceModel.enabled == True)

        services = query.order_by(EmailServiceModel.priority.asc(), EmailServiceModel.id.asc()).all()

        return EmailServiceListResponse(
            total=len(services),
            services=[service_to_response(s) for s in services]
        )


@router.post("/outlook/registration-check", response_model=OutlookRegistrationCheckResponse)
def batch_check_outlook_registration(request: OutlookRegistrationCheckRequest):
    """批量执行 Outlook 注册快检。"""
    service_ids = [int(item) for item in request.service_ids if str(item).strip()]
    if not service_ids:
        raise HTTPException(status_code=400, detail="请选择至少一个 Outlook 账号")
    if len(service_ids) > 50:
        raise HTTPException(status_code=400, detail="单次最多检测 50 个 Outlook 账号")

    results: List[OutlookRegistrationCheckItem] = []
    with get_db() as db:
        services = (
            db.query(EmailServiceModel)
            .filter(
                EmailServiceModel.id.in_(service_ids),
                EmailServiceModel.service_type == "outlook",
            )
            .order_by(EmailServiceModel.priority.asc(), EmailServiceModel.id.asc())
            .all()
        )
        service_map = {int(service.id): service for service in services}
        for service_id in service_ids:
            service = service_map.get(int(service_id))
            if not service:
                results.append(
                    OutlookRegistrationCheckItem(
                        service_id=int(service_id),
                        email="",
                        suitable=False,
                        verdict="service_not_found",
                        summary="Outlook 服务不存在或已删除",
                        local={},
                        probe=None,
                    )
                )
                continue
            results.append(
                _collect_outlook_registration_check(
                    db,
                    service,
                    remote_probe=bool(request.remote_probe),
                )
            )

    return OutlookRegistrationCheckResponse(total=len(results), results=results)


@router.post("/{service_id}/registration-check", response_model=OutlookRegistrationCheckItem)
def check_email_service_registration(service_id: int, remote_probe: bool = Query(True, description="是否执行 OpenAI 实时探测")):
    """执行单个 Outlook 服务注册快检。"""
    with get_db() as db:
        service = (
            db.query(EmailServiceModel)
            .filter(
                EmailServiceModel.id == service_id,
                EmailServiceModel.service_type == "outlook",
            )
            .first()
        )
        if not service:
            raise HTTPException(status_code=404, detail="Outlook 服务不存在")
        return _collect_outlook_registration_check(db, service, remote_probe=bool(remote_probe))


@router.post("/{service_id}/otp-login-repair", response_model=OutlookOtpLoginRepairResponse)
def otp_login_repair_outlook_service(service_id: int):
    """手动触发 Outlook 无密码 OTP 补登录。"""
    with get_db() as db:
        service = (
            db.query(EmailServiceModel)
            .filter(
                EmailServiceModel.id == service_id,
                EmailServiceModel.service_type == "outlook",
            )
            .first()
        )
        if not service:
            raise HTTPException(status_code=404, detail="Outlook 服务不存在")
        return _run_outlook_otp_login_repair(db, service)


@router.get("/{service_id}", response_model=EmailServiceResponse)
async def get_email_service(service_id: int):
    """获取单个邮箱服务详情"""
    with get_db() as db:
        service = db.query(EmailServiceModel).filter(EmailServiceModel.id == service_id).first()
        if not service:
            raise HTTPException(status_code=404, detail="服务不存在")
        return service_to_response(service)


@router.get("/{service_id}/full")
async def get_email_service_full(service_id: int):
    """获取单个邮箱服务完整详情（包含敏感字段，用于编辑）"""
    with get_db() as db:
        service = db.query(EmailServiceModel).filter(EmailServiceModel.id == service_id).first()
        if not service:
            raise HTTPException(status_code=404, detail="服务不存在")

        return {
            "id": service.id,
            "service_type": service.service_type,
            "name": service.name,
            "enabled": service.enabled,
            "priority": service.priority,
            "config": normalize_email_service_config(service.service_type, service.config),  # 返回完整配置
            "last_used": service.last_used.isoformat() if service.last_used else None,
            "created_at": service.created_at.isoformat() if service.created_at else None,
            "updated_at": service.updated_at.isoformat() if service.updated_at else None,
        }


@router.post("", response_model=EmailServiceResponse)
async def create_email_service(request: EmailServiceCreate):
    """创建邮箱服务配置"""
    # 验证服务类型
    try:
        EmailServiceType(request.service_type)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"无效的服务类型: {request.service_type}")

    with get_db() as db:
        # 检查名称是否重复
        existing = db.query(EmailServiceModel).filter(EmailServiceModel.name == request.name).first()
        if existing:
            raise HTTPException(status_code=400, detail="服务名称已存在")

        service = EmailServiceModel(
            service_type=request.service_type,
            name=request.name,
            config=normalize_email_service_config(request.service_type, request.config),
            enabled=request.enabled,
            priority=request.priority
        )
        db.add(service)
        db.commit()
        db.refresh(service)

        return service_to_response(service)


@router.patch("/{service_id}", response_model=EmailServiceResponse)
async def update_email_service(service_id: int, request: EmailServiceUpdate):
    """更新邮箱服务配置"""
    with get_db() as db:
        service = db.query(EmailServiceModel).filter(EmailServiceModel.id == service_id).first()
        if not service:
            raise HTTPException(status_code=404, detail="服务不存在")

        update_data = {}
        if request.name is not None:
            update_data["name"] = request.name
        if request.config is not None:
            # 合并配置而不是替换
            current_config = normalize_email_service_config(service.service_type, service.config)
            merged_config = {**current_config, **request.config}
            # 移除空值
            merged_config = {k: v for k, v in merged_config.items() if v}
            update_data["config"] = normalize_email_service_config(service.service_type, merged_config)
        if request.enabled is not None:
            update_data["enabled"] = request.enabled
        if request.priority is not None:
            update_data["priority"] = request.priority

        for key, value in update_data.items():
            setattr(service, key, value)

        db.commit()
        db.refresh(service)

        return service_to_response(service)


@router.delete("/{service_id}")
async def delete_email_service(service_id: int):
    """删除邮箱服务配置"""
    with get_db() as db:
        service = db.query(EmailServiceModel).filter(EmailServiceModel.id == service_id).first()
        if not service:
            raise HTTPException(status_code=404, detail="服务不存在")

        db.delete(service)
        db.commit()

        return {"success": True, "message": f"服务 {service.name} 已删除"}


@router.post("/{service_id}/test", response_model=ServiceTestResult)
async def test_email_service(service_id: int):
    """测试邮箱服务是否可用"""
    with get_db() as db:
        service = db.query(EmailServiceModel).filter(EmailServiceModel.id == service_id).first()
        if not service:
            raise HTTPException(status_code=404, detail="服务不存在")

        try:
            service_type = EmailServiceType(service.service_type)
            email_service = EmailServiceFactory.create(
                service_type,
                normalize_email_service_config(service.service_type, service.config),
                name=service.name,
            )

            health = email_service.check_health()

            if health:
                return ServiceTestResult(
                    success=True,
                    message="服务连接正常",
                    details=email_service.get_service_info() if hasattr(email_service, 'get_service_info') else None
                )
            else:
                return ServiceTestResult(
                    success=False,
                    message="服务连接失败"
                )

        except Exception as e:
            logger.error(f"测试邮箱服务失败: {e}")
            return ServiceTestResult(
                success=False,
                message=f"测试失败: {str(e)}"
            )


@router.post("/{service_id}/enable")
async def enable_email_service(service_id: int):
    """启用邮箱服务"""
    with get_db() as db:
        service = db.query(EmailServiceModel).filter(EmailServiceModel.id == service_id).first()
        if not service:
            raise HTTPException(status_code=404, detail="服务不存在")

        service.enabled = True
        db.commit()

        return {"success": True, "message": f"服务 {service.name} 已启用"}


@router.post("/{service_id}/disable")
async def disable_email_service(service_id: int):
    """禁用邮箱服务"""
    with get_db() as db:
        service = db.query(EmailServiceModel).filter(EmailServiceModel.id == service_id).first()
        if not service:
            raise HTTPException(status_code=404, detail="服务不存在")

        service.enabled = False
        db.commit()

        return {"success": True, "message": f"服务 {service.name} 已禁用"}


@router.post("/reorder")
async def reorder_services(service_ids: List[int]):
    """重新排序邮箱服务优先级"""
    with get_db() as db:
        for index, service_id in enumerate(service_ids):
            service = db.query(EmailServiceModel).filter(EmailServiceModel.id == service_id).first()
            if service:
                service.priority = index

        db.commit()

        return {"success": True, "message": "优先级已更新"}


@router.post("/outlook/batch-import", response_model=OutlookBatchImportResponse)
async def batch_import_outlook(request: OutlookBatchImportRequest):
    """
    批量导入 Outlook 邮箱账户

    支持两种格式：
    - 格式一（密码认证）：邮箱----密码
    - 格式二（XOAUTH2 认证）：邮箱----密码----client_id----refresh_token

    每行一个账户，使用四个连字符（----）分隔字段
    """
    lines = request.data.strip().split("\n")
    total = len(lines)
    success = 0
    failed = 0
    accounts = []
    errors = []

    with get_db() as db:
        for i, line in enumerate(lines):
            line = line.strip()

            # 跳过空行和注释
            if not line or line.startswith("#"):
                continue

            parts = line.split("----")

            # 验证格式
            if len(parts) < 2:
                failed += 1
                errors.append(f"行 {i+1}: 格式错误，至少需要邮箱和密码")
                continue

            email = parts[0].strip()
            password = parts[1].strip()

            # 验证邮箱格式
            if "@" not in email:
                failed += 1
                errors.append(f"行 {i+1}: 无效的邮箱地址: {email}")
                continue

            # 检查是否已存在
            existing = db.query(EmailServiceModel).filter(
                EmailServiceModel.service_type == "outlook",
                EmailServiceModel.name == email
            ).first()

            if existing:
                failed += 1
                errors.append(f"行 {i+1}: 邮箱已存在: {email}")
                continue

            # 构建配置
            config = {
                "email": email,
                "password": password,
                "allow_imap_fallback": bool(request.allow_imap_fallback),
            }

            # 检查是否有 OAuth 信息（格式二）
            if len(parts) >= 4:
                client_id = parts[2].strip()
                refresh_token = parts[3].strip()
                if client_id and refresh_token:
                    config["client_id"] = client_id
                    config["refresh_token"] = refresh_token

            # 创建服务记录
            try:
                service = EmailServiceModel(
                    service_type="outlook",
                    name=email,
                    config=config,
                    enabled=request.enabled,
                    priority=request.priority
                )
                db.add(service)
                db.commit()
                db.refresh(service)

                accounts.append({
                    "id": service.id,
                    "email": email,
                    "has_oauth": bool(config.get("client_id")),
                    "name": email
                })
                success += 1

            except Exception as e:
                failed += 1
                errors.append(f"行 {i+1}: 创建失败: {str(e)}")
                db.rollback()

    return OutlookBatchImportResponse(
        total=total,
        success=success,
        failed=failed,
        accounts=accounts,
        errors=errors
    )


@router.delete("/outlook/batch")
async def batch_delete_outlook(service_ids: List[int]):
    """批量删除 Outlook 邮箱服务"""
    deleted = 0
    with get_db() as db:
        for service_id in service_ids:
            service = db.query(EmailServiceModel).filter(
                EmailServiceModel.id == service_id,
                EmailServiceModel.service_type == "outlook"
            ).first()
            if service:
                db.delete(service)
                deleted += 1
        db.commit()

    return {"success": True, "deleted": deleted, "message": f"已删除 {deleted} 个服务"}


# ============== 临时邮箱测试 ==============

class TempmailTestRequest(BaseModel):
    """临时邮箱测试请求"""
    provider: str = "tempmail"
    api_url: Optional[str] = None
    api_key: Optional[str] = None


@router.post("/test-tempmail")
async def test_tempmail_service(request: TempmailTestRequest):
    """测试临时邮箱服务是否可用"""
    try:
        settings = get_settings()
        provider = str(request.provider or "tempmail").strip().lower()

        if provider == "yyds_mail":
            base_url = request.api_url or settings.yyds_mail_base_url
            api_key = request.api_key
            if api_key is None and settings.yyds_mail_api_key:
                api_key = settings.yyds_mail_api_key.get_secret_value()

            config = {
                "base_url": base_url,
                "api_key": api_key or "",
                "default_domain": settings.yyds_mail_default_domain,
                "timeout": settings.yyds_mail_timeout,
                "max_retries": settings.yyds_mail_max_retries,
            }
            service = EmailServiceFactory.create(EmailServiceType.YYDS_MAIL, config)
            success_message = "YYDS Mail 连接正常"
            fail_message = "YYDS Mail 连接失败"
        else:
            base_url = request.api_url or settings.tempmail_base_url
            config = {
                "base_url": base_url,
                "timeout": settings.tempmail_timeout,
                "max_retries": settings.tempmail_max_retries,
            }
            service = EmailServiceFactory.create(EmailServiceType.TEMPMAIL, config)
            success_message = "临时邮箱连接正常"
            fail_message = "临时邮箱连接失败"

        # 检查服务健康状态
        health = service.check_health()

        if health:
            return {"success": True, "message": success_message}
        else:
            return {"success": False, "message": fail_message}

    except Exception as e:
        logger.error(f"测试临时邮箱失败: {e}")
        return {"success": False, "message": f"测试失败: {str(e)}"}
