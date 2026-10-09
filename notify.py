"""발송 채널 모음. 본체(kimjang_report.py)는 메시지만 만들고, 어디로 보낼지는 여기서 정한다.

환경변수로 켜진 채널만 동작한다. 아무것도 없으면 발송 없이 저장만 한다.

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


def send_sms(text):
    """솔라피 메시지 API. 90바이트 넘으면 서버가 LMS로 자동 전환한다(한도 2,000바이트)."""
    key, secret = os.environ.get("SOLAPI_KEY"), os.environ.get("SOLAPI_SECRET")
    sender, to = os.environ.get("SMS_FROM"), os.environ.get("SMS_TO")
    if not all([key, secret, sender, to]):
        return None
    date = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    salt = uuid.uuid4().hex
    sig = hmac.new(secret.encode(), (date + salt).encode(), hashlib.sha256).hexdigest()
    auth = f"HMAC-SHA256 apiKey={key}, date={date}, salt={salt}, signature={sig}"
    body = {"messages": [{"to": n.strip().replace("-", ""), "from": sender.replace("-", ""),
                          "text": text, "subject": "김장 물가 알리미"}
                         for n in to.split(",") if n.strip()]}
    res = _post_json("https://api.solapi.com/messages/v4/send-many/detail", body, {"Authorization": auth})
    return f"문자 {len(body['messages'])}건 접수 (groupId {res.get('groupInfo', {}).get('_id', '?')})"


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


def send_all(text, subject):
    """켜진 채널 전부로 보내고 결과 문자열 목록을 돌려준다. 한 채널이 실패해도 나머지는 계속."""
    results = []
    for fn, args in ((send_sms, (text,)), (send_mail, (text, subject)), (send_telegram, (text,))):
        try:
            r = fn(*args)
            if r:
                results.append(r)
        except Exception as e:  # 발송 실패가 리포트 저장을 막으면 안 된다
            results.append(f"{fn.__name__} 실패: {e}")
    return results
