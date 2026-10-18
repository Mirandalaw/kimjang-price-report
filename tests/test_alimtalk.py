"""표준 라이브러리 unittest 만 쓴다:  python3 -m unittest -v
샘플 픽스처로 알림톡 변수와 발송 페이로드를 검사한다 (인터넷·솔라피 불필요)."""
import datetime as dt, json, os, sys, unittest
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
import kimjang_report as K  # noqa: E402
import notify  # noqa: E402

TEMPLATE_VARS = ["기준일", "가족기준", "총액", "주간증감", "연간증감",
                 "수량1", "금액1", "평년비1", "주간비1", "수량2", "금액2", "평년비2", "주간비2",
                 "수량3", "금액3", "평년비3", "주간비3", "수량4", "금액4", "주간비4", "수량5", "금액5", "주간비5",
                 "기타요약", "미조사품목", "다음발송일"]


def _sample():
    cfg = json.loads((BASE / "items.json").read_text(encoding="utf-8"))
    rows = {c: K.fetch_category(c, "2026-10-17", str(BASE / "sample")) for c in ["200", "600"]}
    return cfg, dt.date(2026, 10, 17), K.build(cfg["items"], rows)


class TemplateVars(unittest.TestCase):
    def test_all_template_variables_present_and_nothing_else(self):
        cfg, day, recs = _sample()
        v = K.template_vars(day, cfg, recs)
        self.assertEqual(set(v), {f"#{{{k}}}" for k in TEMPLATE_VARS})
        self.assertTrue(all(isinstance(x, str) and x for x in v.values()))

    def test_values_look_right(self):
        cfg, day, recs = _sample()
        v = K.template_vars(day, cfg, recs)
        self.assertEqual(v["#{기준일}"], "10/17(토)")
        self.assertEqual(v["#{수량4}"], "20포기")
        self.assertIn("쪽파", v["#{미조사품목}"])
        self.assertRegex(v["#{주간비4}"], r"^[+-]\d+%$|^변동 없음$|^자료 없음$")
        self.assertNotIn("-", [x.strip() for x in v.values()])   # '-' 한 글자 값은 없어야 한다
        self.assertRegex(v["#{다음발송일}"], r"^\d+/\d+\(월\)$")

    def test_signed_pct(self):
        self.assertEqual(K.signed_pct(103, 100), "+3%")
        self.assertEqual(K.signed_pct(100.2, 100), "변동 없음")
        self.assertEqual(K.signed_pct(97, 100), "-3%")
        self.assertEqual(K.signed_pct(None, 100), "자료 없음")

    def test_next_send_day_is_next_monday(self):
        self.assertEqual(K.next_send_day(dt.date(2026, 10, 19)), "10/26(월)")   # 월요일 실행 → 다음 주 월요일
        self.assertEqual(K.next_send_day(dt.date(2026, 10, 18)), "10/19(월)")   # 일요일 → 내일


class WeekFallback(unittest.TestCase):
    def test_holiday_week_ago_uses_previous_business_day(self):
        """10/16 기준인데 1주일 전(10/9 한글날) 칸이 비어 있으면 10/8 가격으로 채운다."""
        cfg = json.loads((BASE / "items.json").read_text(encoding="utf-8"))
        real = K.fetch_category

        def fake(category, regday, fixture=None):
            rows = real(category, regday, str(BASE / "sample"))
            if regday == "2026-10-16":            # 기준일: 1주일 전 칸 비움
                return [dict(r, dpr3="-") for r in rows]
            if regday == "2026-10-09":            # 한글날: 가격 없음
                return [dict(r, dpr1="-") for r in rows]
            if regday == "2026-10-08":            # 직전 영업일: 당일가 = 기준일 당일가의 90%
                return [dict(r, dpr1=str(round(K.num(r["dpr1"]) * 0.9))) if K.num(r.get("dpr1")) else r for r in rows]
            return rows

        with mock.patch.object(K, "fetch_category", fake):
            rows = {c: K.fetch_category(c, "2026-10-16") for c in ["200", "600"]}
            recs = K.build(cfg["items"], rows)
            self.assertTrue(all(r.get("week") is None for r in recs if r["found"]))
            K.fill_week(recs, cfg["items"], dt.date(2026, 10, 16), ["200", "600"])
        got = [r for r in recs if r["found"]]
        self.assertTrue(all(r.get("week_from") == "2026-10-08" for r in got))
        v = K.template_vars(dt.date(2026, 10, 16), cfg, recs)
        self.assertEqual(v["#{주간비4}"], "+11%")        # 1/0.9 - 1
        self.assertNotEqual(v["#{주간증감}"], "자료 없음")


class Sending(unittest.TestCase):
    ENV = {"SOLAPI_KEY": "k", "SOLAPI_SECRET": "s", "SMS_FROM": "010-1111-2222", "SMS_TO": "010-3333-4444, 01055556666",
           "KAKAO_PF_ID": "pf", "KAKAO_TEMPLATE_ID": "tpl"}

    def test_alimtalk_payload_and_sms_skipped(self):
        sent = []
        with mock.patch.dict(os.environ, self.ENV, clear=True), \
             mock.patch.object(notify, "_post_json", lambda url, body, headers: sent.append(body) or {"groupInfo": {"_id": "G1"}}):
            res = notify.send_all("본문", "제목", variables={"#{기준일}": "10/20(월)"})
        self.assertEqual(len(sent), 1)                      # 알림톡 1회, 문자는 건너뜀
        msgs = sent[0]["messages"]
        self.assertEqual([m["to"] for m in msgs], ["01033334444", "01055556666"])
        self.assertEqual(msgs[0]["kakaoOptions"], {"pfId": "pf", "templateId": "tpl", "variables": {"#{기준일}": "10/20(월)"}})
        self.assertEqual(msgs[0]["text"], "본문")            # 실패 시 대체 문자 본문
        self.assertTrue(res and res[0].startswith("알림톡 2건"))

    def test_sms_when_alimtalk_off(self):
        env = {k: v for k, v in self.ENV.items() if not k.startswith("KAKAO")}
        sent = []
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(notify, "_post_json", lambda url, body, headers: sent.append(body) or {"groupInfo": {"_id": "G2"}}):
            res = notify.send_all("본문", "제목", variables={"#{기준일}": "x"})
        self.assertEqual(len(sent), 1)
        self.assertNotIn("kakaoOptions", sent[0]["messages"][0])
        self.assertTrue(res[0].startswith("문자 2건"))


if __name__ == "__main__":
    unittest.main()
