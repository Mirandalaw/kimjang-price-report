"""발송 채널 모음. 본체(kimjang_report.py)는 메시지만 만들고, 어디로 보낼지는 여기서 정한다.

환경변수로 켜진 채널만 동작한다. 아무것도 없으면 발송 없이 저장만 한다.

알림톡 (솔라피)          위 문자 설정 + KAKAO_PF_ID(카카오 채널 ID), KAKAO_TEMPLATE_ID(검수 통과한 템플릿 ID)
                         켜져 있으면 문자 대신 알림톡으로 보내고, 알림톡 실패 시 같은 번호로 문자가 자동 대체 발송된다(솔라피 기본 동작).
문자 (솔라피 / CoolSMS)  SOLAPI_KEY, SOLAPI_SECRET, SMS_FROM(인증된 발신번호), SMS_TO(쉼표로 여러 명)
메일                     SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, MAIL_TO
텔레그램 (본인 확인용)   TELEGRAM_TOKEN, TELEGRAM_CHAT_ID

표준 라이브러리만 쓴다.
"""
import datetime as dt, hashlib, hmac, json, os, smtplib, urllib.request, uuid
from email.mime.text import MIMEText


def _post_json(url, body, headers):
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def _solapi_auth():
    key, secret = os.environ["SOLAPI_KEY"], os.environ["SOLAPI_SECRET"]
    date = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    salt = uuid.uuid4().hex
    sig = hmac.new(secret.encode(), (date + salt).encode(), hashlib.sha256).hexdigest()
    return {"Authorization": f"HMAC-SHA256 apiKey={key}, date={date}, salt={salt}, signature={sig}"}


def _solapi_send(messages):
    res = _post_json("https://api.solapi.com/messages/v4/send-many/detail", {"messages": messages}, _solapi_auth())
    return res.get("groupInfo", {}).get("_id", "?")


def _recipients():
    sender, to = os.environ.get("SMS_FROM", ""), os.environ.get("SMS_TO", "")
    return sender.replace("-", ""), [n.strip().replace("-", "") for n in to.split(",") if n.strip()]


def _sms_ready():
    sender, to = _recipients()
    return all([os.environ.get("SOLAPI_KEY"), os.environ.get("SOLAPI_SECRET"), sender, to])


def alimtalk_enabled():
    return _sms_ready() and bool(os.environ.get("KAKAO_PF_ID")) and bool(os.environ.get("KAKAO_TEMPLATE_ID"))


def send_alimtalk(text, variables):
    """솔라피 카카오 알림톡. variables 는 kimjang_report.template_vars() 가 만든 {"#{기준일}": "10/20(월)", ...}.
    템플릿의 모든 변수가 채워져 있어야 접수된다. text 는 알림톡 실패 시 자동 대체 발송되는 문자 본문."""
    if not alimtalk_enabled():
        return None
    pf, tpl = os.environ["KAKAO_PF_ID"], os.environ["KAKAO_TEMPLATE_ID"]
    sender, to = _recipients()
    messages = [{"to": n, "from": sender, "text": text, "subject": "김장 물가 알리미",
                 "kakaoOptions": {"pfId": pf, "templateId": tpl, "variables": variables}}
                for n in to]
    gid = _solapi_send(messages)
    return f"알림톡 {len(messages)}건 접수 (groupId {gid}, 실패 시 문자 대체)"


def send_sms(text):
    """솔라피 메시지 API. 90바이트 넘으면 서버가 LMS로 자동 전환한다(한도 2,000바이트).
    알림톡이 켜져 있으면 건너뛴다 — 알림톡 실패 시 솔라피가 대체문자를 보내므로 중복 발송 방지."""
    if not _sms_ready() or alimtalk_enabled():
        return None
    sender, to = _recipients()
    messages = [{"to": n, "from": sender, "text": text, "subject": "김장 물가 알리미"} for n in to]
    gid = _solapi_send(messages)
    return f"문자 {len(messages)}건 접수 (groupId {gid})"


def send_mail(text, subject):
    host, to = os.environ.get("SMTP_HOST"), os.environ.get("MAIL_TO")
    if not host or not to:
        return None
    m = MIMEText(text, "plain", "utf-8")
    m["Subject"], m["From"], m["To"] = subject, os.environ["SMTP_USER"], to
    with smtplib.SMTP_SSL(host, int(os.environ.get("SMTP_PORT", "465")), timeout=20) as s:
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
        s.sendmail(m["From"], [a.strip() for a in to.split(",")], m.as_string())
    return "메일 발송"


def send_telegram(text):
    token, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return None
    _post_json(f"https://api.telegram.org/bot{token}/sendMessage", {"chat_id": chat, "text": text}, {})
    return "텔레그램 발송"


def send_all(text, subject, variables=None):
    """켜진 채널 전부로 보내고 결과 문자열 목록을 돌려준다. 한 채널이 실패해도 나머지는 계속.
    variables(알림톡 템플릿 변수)가 있고 알림톡이 켜져 있으면 알림톡, 아니면 문자."""
    results = []
    channels = [(send_alimtalk, (text, variables or {})), (send_sms, (text,)),
                (send_mail, (text, subject)), (send_telegram, (text,))]
    for fn, args in channels:
        try:
            r = fn(*args)
            if r:
                results.append(r)
        except Exception as e:  # 발송 실패가 리포트 저장을 막으면 안 된다
            results.append(f"{fn.__name__} 실패: {e}")
    return results
