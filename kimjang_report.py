#!/usr/bin/env python3
"""김장 물가 알리미 — KAMIS Open API 소매가격으로 주간 김장 물가 리포트를 만든다.

사용법
  python kimjang_report.py codes              # 분류별 품목 이름·단위 확인 (items.json 맞출 때)
  python kimjang_report.py report             # 리포트 생성 + 로그 저장
  python kimjang_report.py report --date 2026-09-28
  python kimjang_report.py report --fixture sample   # 인터넷 없이 샘플로 시험
  python kimjang_report.py vars --fixture sample     # 알림톡 템플릿 변수만 출력 (템플릿 검수·확인용)

환경변수
  KAMIS_CERT_KEY, KAMIS_CERT_ID   (필수, 인증키와 KAMIS 회원 아이디)
  발송 채널 설정은 notify.py 참고 (알림톡·문자·메일·텔레그램, 없으면 저장만)
표준 라이브러리만 쓴다.
"""
import argparse, csv, datetime as dt, json, os, re, sys, time
import urllib.parse, urllib.request
from pathlib import Path

import notify

BASE = Path(__file__).resolve().parent
API = "https://www.kamis.or.kr/service/price/xml.do"
CATEGORIES = {"100": "식량작물", "200": "채소류", "300": "특용작물", "400": "과일류", "500": "축산물", "600": "수산물"}
WEEKDAY = "월화수목금토일"
# dailyPriceByCategoryList 응답 필드: dpr1 당일, dpr3 1주일전, dpr5 1개월전, dpr6 1년전, dpr7 평년
FIELDS = {"now": "dpr1", "week": "dpr3", "month": "dpr5", "year": "dpr6", "normal": "dpr7"}


# ---------- 조회 ----------
def fetch_category(category, regday, fixture=None):
    """해당 날짜·분류의 소매가격 목록. 데이터가 없으면 빈 리스트."""
    if fixture:
        f = Path(fixture) / f"{category}.json"
        raw = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    else:
        key, cid = os.environ.get("KAMIS_CERT_KEY"), os.environ.get("KAMIS_CERT_ID")
        if not key or not cid:
            sys.exit("KAMIS_CERT_KEY, KAMIS_CERT_ID 환경변수를 먼저 설정하세요.")
        q = urllib.parse.urlencode({
            "action": "dailyPriceByCategoryList", "p_cert_key": key, "p_cert_id": cid,
            "p_returntype": "json", "p_product_cls_code": "01",  # 01 소매
            "p_item_category_code": category, "p_country_code": "",  # 빈 값 = 전국 평균
            "p_regday": regday, "p_convert_kg_yn": "N",
        })
        with urllib.request.urlopen(f"{API}?{q}", timeout=20) as r:
            raw = json.loads(r.read().decode("utf-8"))
        time.sleep(0.5)  # 서버 부담 줄이기
    data = raw.get("data") if isinstance(raw, dict) else None
    if not isinstance(data, dict) or data.get("error_code") not in (None, "000"):
        return []
    items = data.get("item", [])
    return [items] if isinstance(items, dict) else list(items)


def latest_business_day(target, categories, fixture=None, max_back=10, ok=None):
    """주말·공휴일엔 가격이 없으므로 쓸 만한 데이터가 있는 가장 가까운 과거 날짜를 찾는다.
    KAMIS는 공휴일에도 품목 목록은 주고 가격만 '-'로 비우므로, 목록 유무가 아니라
    ok(rows) 판정(기본: 숫자 가격이 하나라도 있는지)으로 고른다."""
    if ok is None:
        ok = lambda rows: any(num(r.get("dpr1")) is not None for rs in rows.values() for r in rs)
    for back in range(max_back + 1):
        day = target - dt.timedelta(days=back)
        rows = {c: fetch_category(c, day.isoformat(), fixture) for c in categories}
        if ok(rows):
            if back:
                print(f"{target}에 가격이 없어 {day} 가격을 씁니다.", file=sys.stderr)
            return day, rows
    sys.exit(f"{target} 이전 {max_back}일 동안 가격 데이터가 없습니다.")


# ---------- 계산 ----------
def num(v):
    if isinstance(v, (int, float)):
        return float(v)
    if not isinstance(v, str):
        return None
    s = v.replace(",", "").strip()
    try:
        return float(s) if s and s != "-" else None
    except ValueError:
        return None


def parse_unit(unit):
    """'1포기' → (1,'포기'), '100g' → (100,'g'), '1kg' → (1000,'g')"""
    m = re.search(r"([\d.]+)\s*(kg|g|포기|개|단|마리)", unit or "")
    if not m:
        return None
    n, u = float(m.group(1)), m.group(2)
    return (n * 1000, "g") if u == "kg" else (n, u)


def pick_row(rows, spec):
    cands = [r for r in rows
             if spec["match_item"] in str(r.get("item_name", ""))
             and spec.get("match_kind", "") in str(r.get("kind_name", ""))
             and num(r.get("dpr1")) is not None]
    if not cands:
        return None
    cands.sort(key=lambda r: 0 if "상품" in str(r.get("rank", "")) else 1)  # 상품 등급 우선
    return cands[0]


def pct(a, b):
    return None if a is None or not b else (a / b - 1) * 100


def verdict(p):
    """9~10월에 사는 재료용 판정. 규칙은 단순하게, 근거가 메시지에 그대로 보이게."""
    vs_normal, vs_week = pct(p["now"], p["normal"]), pct(p["now"], p["week"])
    if vs_normal is not None and vs_normal <= 0:
        return f"평년보다 {abs(vs_normal):.0f}% 쌈 → 사도 좋음"
    if vs_week is not None and vs_week <= -3:
        return f"지난주보다 {abs(vs_week):.0f}% 내림 → 1~2주 더 지켜보기"
    if vs_normal is not None and vs_normal >= 10:
        return f"평년보다 {vs_normal:.0f}% 비쌈 → 필요한 만큼만"
    return "평년 수준 → 사도 무방"


def build(specs, rows_by_cat):
    out = []
    for s in specs:
        row = pick_row(rows_by_cat.get(s["category"], []), s)
        rec = {"name": s["name"], "buy": s["buy"], "qty": s["qty"], "qty_unit": s["qty_unit"], "found": bool(row)}
        if row:
            u = parse_unit(row.get("unit", ""))
            rec.update(item_name=row.get("item_name"), kind_name=row.get("kind_name"),
                       rank=row.get("rank"), unit=row.get("unit"))
            if not u or u[1] != s["qty_unit"]:
                rec["found"], rec["error"] = False, f"단위 불일치: KAMIS '{row.get('unit')}' vs 설정 '{s['qty_unit']}'"
            else:
                factor = s["qty"] / u[0]
                for k, f in FIELDS.items():
                    v = num(row.get(f))
                    rec[k] = v                                   # KAMIS 단위 가격
                    rec[k + "_cost"] = round(v * factor) if v is not None else None  # 우리 집 수량 기준
        out.append(rec)
    return out


def total(recs, key):
    """비교 시점 가격이 있는 재료끼리만 합산해 공정하게 비교."""
    both = [r for r in recs if r["found"] and r.get(key + "_cost") is not None]
    return sum(r["now_cost"] for r in both), sum(r[key + "_cost"] for r in both)


# ---------- 메시지 ----------
def won(v):
    return f"{v/10000:.1f}만원" if v >= 10000 else f"{v:,.0f}원"


def qty_text(r):
    if r["qty_unit"] == "g":
        return f"{r['qty']/1000:g}kg" if r["qty"] >= 1000 else f"{r['qty']:g}g"
    return f"{r['qty']:g}{r['qty_unit']}"


def trend(a, b, label):
    p = pct(a, b)
    if p is None:
        return ""
    return f"{label} {'+' if p >= 0 else ''}{p:.0f}%"


def render(day, family, recs):
    ok = [r for r in recs if r["found"]]
    now_total = sum(r["now_cost"] for r in ok)
    lines = [f"[김장 물가 알리미] {day.month}/{day.day}({WEEKDAY[day.weekday()]}) 기준",
             f"{family} 예상 재료비 약 {won(now_total)}"]
    cmp = [trend(*total(recs, "week"), "지난주 대비"), trend(*total(recs, "year"), "작년 대비")]
    if any(cmp):
        lines.append("(" + ", ".join(c for c in cmp if c) + ")")
    now_items = [r for r in ok if r["buy"] == "now"]
    later_items = [r for r in ok if r["buy"] == "later"]
    if now_items:
        lines += ["", "■ 지금 사 둘 재료"]
        lines += [f"- {r['name']} {qty_text(r)} {won(r['now_cost'])}: {verdict(r)}" for r in now_items]
    if later_items:
        lines += ["", "■ 11월에 살 재료 (가격 흐름)"]
        for r in later_items:
            t = trend(r["now"], r["week"], "지난주") or trend(r["now"], r["normal"], "평년")
            lines.append(f"- {r['name']} {qty_text(r)} {won(r['now_cost'])}" + (f" ({t})" if t else ""))
    missing = [r["name"] for r in recs if not r["found"]]
    if missing:
        lines += ["", "※ 이번 주 가격 없음: " + ", ".join(missing)]
    lines += ["", "출처: KAMIS 소매가격(전국 평균)"]
    return "\n".join(lines)


# ---------- 알림톡 템플릿 변수 ----------
def signed_pct(a, b, none="자료 없음"):
    """'▲3%' / '▼2%' / '변동 없음' (오르면 ▲, 내리면 ▼ — 숫자 앞 +/- 보다 눈에 잘 띈다). 비교값이 없으면 none (알림톡에서 '-' 한 글자는 뜻이 안 보여서 말로 쓴다)."""
    p = pct(a, b)
    if p is None:
        return none
    r = round(p)
    return "변동 없음" if r == 0 else f"{'▲' if r > 0 else '▼'}{abs(r)}%"


def next_send_day(today=None, weekday=0):
    """다음 발송일(기본: 다음 주 월요일) → '10/20(월)'."""
    today = today or dt.date.today()
    d = today + dt.timedelta(days=(weekday - today.weekday() - 1) % 7 + 1)
    return f"{d.month}/{d.day}({WEEKDAY[d.weekday()]})"


def status_label(r):
    """알림톡용 상태 한 줄. 구매 권유 없이 사실만 적는다(정보성 기준): 평년 대비 수준 + 최근 흐름.
    예) '🟢 평년보다 낮음 · 하락 중' / '🔴 평년보다 크게 높음' / '⚪ 평년 수준'."""
    if not r or not r.get("found"):
        return "자료 없음"
    vn, vw = pct(r["now"], r["normal"]), pct(r["now"], r["week"])
    if vn is None:
        level = "⚪ 평년 자료 없음"
    elif vn <= -3:
        level = "🟢 평년보다 낮음"
    elif vn >= 10:
        level = "🔴 평년보다 크게 높음"
    elif vn >= 3:
        level = "🟠 평년보다 높음"
    else:
        level = "⚪ 평년 수준"
    flow = "" if vw is None else " · 하락 중" if vw <= -3 else " · 상승 중" if vw >= 3 else ""
    return level + flow


def status_mark(r):
    """v5 템플릿용 한 글자 표시. 🟢 평년보다 쌈(-3% 이하) · ⚪ 평년 수준 · 🔴 평년보다 비쌈(+3% 이상) · ⚫ 자료 없음.
    뜻은 템플릿 안의 범례 줄이 설명한다. 구매 권유 아님."""
    if not r or not r.get("found"):
        return "⚫"
    vn = pct(r["now"], r["normal"])
    if vn is None:
        return "⚪"
    return "🟢" if vn <= -3 else "🔴" if vn >= 3 else "⚪"


def group_total(recs, names, key):
    """이름 목록에 든 재료의 합계 (현재, 비교시점). 둘 다 값이 있는 재료만."""
    rs = [r for r in recs if r["name"] in names and r["found"] and r.get(key + "_cost") is not None]
    return sum(r["now_cost"] for r in rs), sum(r[key + "_cost"] for r in rs)


def level_words(a, b, base="평년"):
    """'평년보다 7% 쌈' / '평년 수준' / '평년보다 10% 비쌈' / '자료 없음'. 사실 서술만, 권유 없음 (v6 템플릿)."""
    p = pct(a, b)
    if p is None:
        return "자료 없음"
    r = round(p)
    if abs(r) < 3:
        return f"{base} 수준"
    return f"{base}보다 {abs(r)}% {'비쌈' if r > 0 else '쌈'}"


def change_words(a, b, base="지난주"):
    """'지난주보다 3% 내림' / '지난주보다 2% 오름' / '지난주와 비슷' / '자료 없음' (v6 템플릿)."""
    p = pct(a, b)
    if p is None:
        return "자료 없음"
    r = round(p)
    if abs(r) < 1:
        return f"{base}와 비슷"
    return f"{base}보다 {abs(r)}% {'오름' if r > 0 else '내림'}"


def template_vars(day, cfg, recs):
    """카카오 알림톡 변수. 검수받은 템플릿의 #{변수} 이름과 1:1 — 빠지는 변수가 있으면 발송이 거절되므로 전부 채운다.

    템플릿(2026-10 검수본)
      #{기준일} #{가족기준} #{총액} #{주간증감} #{연간증감}
      슬롯 1~3 (지금 사는 재료): #{수량n} #{금액n} #{평년비n} #{주간비n} #{상태n}(v4) #{표시n} #{연간비n}(v5)
      슬롯 4~5 (11월 재료):      #{수량n} #{금액n} #{주간비n}
      #{기타요약} #{미조사품목} #{다음발송일}
      v5(간결형): #{채소금액} #{채소주간} #{채소연간} — later_slots+etc 재료 합계
      v6(최소형): #{연간말} #{평년말n} #{주간말n} #{기타금액} #{기타말} — 숫자 대신 말로 ('평년보다 7% 쌈', '지난주보다 3% 내림')
    v1~v5 어느 템플릿이든 쓸 수 있게 변수를 전부 만든다. 솔라피는 템플릿에 없는 변수를 무시한다.
    슬롯 ↔ 재료 대응은 items.json 의 alimtalk.slots(1~3) / later_slots(4~5). 가격이 없는 재료는 '가격 없음'·'자료 없음'으로 채우고 미조사품목에 이름을 적는다.
    템플릿에 없는 변수는 보내지 않는다(4~5 는 평년비 없음).
    """
    by = {r["name"]: r for r in recs}
    a = cfg.get("alimtalk", {})
    ok = [r for r in recs if r["found"]]
    v = {
        "기준일": f"{day.month}/{day.day}({WEEKDAY[day.weekday()]})",
        "가족기준": cfg.get("family", ""),
        "총액": won(sum(r["now_cost"] for r in ok)) if ok else "자료 없음",
        "주간증감": signed_pct(*total(recs, "week")),
        "연간증감": signed_pct(*total(recs, "year")),
    }
    for slots, with_normal in ((a.get("slots", {}), True), (a.get("later_slots", {}), False)):
        for n, name in slots.items():
            r = by.get(name)
            found = r is not None and r["found"]
            v[f"수량{n}"] = qty_text(r) if r else "-"
            v[f"금액{n}"] = won(r["now_cost"]) if found else "가격 없음"
            v[f"주간비{n}"] = signed_pct(r["now"], r["week"]) if found else "자료 없음"
            v[f"주간말{n}"] = change_words(r["now"], r["week"]) if found else "가격 없음"
            if with_normal:
                v[f"평년비{n}"] = signed_pct(r["now"], r["normal"]) if found else "자료 없음"
                v[f"상태{n}"] = status_label(r)
                v[f"표시{n}"] = status_mark(r)
                v[f"연간비{n}"] = signed_pct(r["now"], r["year"]) if found else "자료 없음"
                v[f"평년말{n}"] = level_words(r["now"], r["normal"]) if found else "가격 없음"
    etc = []
    for name in a.get("etc", []):
        r = by.get(name)
        if r and r["found"]:
            etc.append(f"{name} {won(r['now_cost'])}")
    v["기타요약"] = " · ".join(etc) if etc else "이번 주 가격 없음"
    missing = [r["name"] for r in recs if not r["found"]]
    v["미조사품목"] = ", ".join(missing) if missing else "없음"
    veg = list(a.get("later_slots", {}).values()) + list(a.get("etc", []))
    veg_now = group_total(recs, veg, "now")[0]
    v["채소금액"] = won(veg_now) if veg_now else "자료 없음"
    v["채소주간"] = signed_pct(*group_total(recs, veg, "week"))
    v["채소연간"] = signed_pct(*group_total(recs, veg, "year"))
    v["채소말"] = change_words(*group_total(recs, veg, "week"))
    etc_names = list(a.get("etc", []))
    etc_now = group_total(recs, etc_names, "now")[0]
    v["기타금액"] = won(etc_now) if etc_now else "가격 없음"
    v["기타말"] = change_words(*group_total(recs, etc_names, "week"))
    v["연간말"] = level_words(*total(recs, "year"), base="작년")
    v["다음발송일"] = next_send_day()
    return {f"#{{{k}}}": val for k, val in v.items()}


# ---------- 저장·발송 ----------
def save(day, recs, msg, outdir):
    (outdir / "logs").mkdir(parents=True, exist_ok=True)
    (outdir / "messages").mkdir(parents=True, exist_ok=True)
    (outdir / "logs" / f"{day}.json").write_text(
        json.dumps({"date": str(day), "items": recs}, ensure_ascii=False, indent=2), encoding="utf-8")
    (outdir / "messages" / f"{day}.txt").write_text(msg, encoding="utf-8")
    csv_path = outdir / "prices.csv"
    cols = ["date", "name", "buy", "unit", "now", "week", "month", "year", "normal", "now_cost"]
    rows = []
    if csv_path.exists():  # 같은 날짜로 다시 돌리면 덮어쓴다
        with csv_path.open(encoding="utf-8-sig") as f:
            rows = [r for r in csv.DictReader(f) if r["date"] != str(day)]
    rows += [{"date": str(day), **{c: r.get(c, "") for c in cols[1:]}} for r in recs if r["found"]]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:  # 엑셀에서 한글 안 깨지게
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)



# ---------- 명령 ----------
def cmd_codes(args):
    day = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    day, rows = latest_business_day(day, ["200", "300", "600"], args.fixture)
    print(f"# {day} 소매가격 품목 목록")
    for c, items in rows.items():
        print(f"\n## {c} {CATEGORIES[c]} ({len(items)}건)")
        for r in items:
            print(f"{r.get('item_name')} | {r.get('kind_name')} | {r.get('rank')} | {r.get('unit')} | 당일 {r.get('dpr1')}")


def _collect(args):
    cfg = json.loads((BASE / "items.json").read_text(encoding="utf-8"))
    cats = sorted({i["category"] for i in cfg["items"]})
    day = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    half = len(cfg["items"]) / 2
    enough = lambda rows: sum(1 for r in build(cfg["items"], rows) if r["found"]) >= half
    day, rows = latest_business_day(day, cats, args.fixture, ok=enough)
    recs = build(cfg["items"], rows)
    fill_week(recs, cfg["items"], day, cats, args.fixture)
    return cfg, day, recs


def fill_week(recs, specs, day, cats, fixture=None):
    """KAMIS 의 '1주일 전' 칸(dpr3)은 그날이 공휴일이면 비어 있다(예: 10/16 기준 → 10/9 한글날).
    비어 있는 재료만, 7일 전부터 거슬러 올라가 가격이 있는 가장 가까운 영업일의 당일 가격으로 채운다.
    rec['week_from'] 에 실제 비교일을 남긴다."""
    need = [r for r in recs if r["found"] and r.get("week") is None]
    if not need:
        return
    target = day - dt.timedelta(days=7)
    for back in range(0, 6):
        d = target - dt.timedelta(days=back)
        rows = {c: fetch_category(c, d.isoformat(), fixture) for c in cats}
        prev = {b["name"]: b for b in build(specs, rows)}
        for r in list(need):
            p = prev.get(r["name"])
            if p and p["found"] and p.get("now") is not None:
                factor = r["now_cost"] / r["now"] if r["now"] else 0
                r["week"], r["week_cost"], r["week_from"] = p["now"], round(p["now"] * factor), d.isoformat()
                need.remove(r)
        if not need:
            break


def cmd_vars(args):
    """알림톡 변수를 그대로 출력한다. 솔라피 템플릿 테스트 발송 화면에 붙여 넣어 확인할 때 쓴다."""
    cfg, day, recs = _collect(args)
    for k, v in template_vars(day, cfg, recs).items():
        print(f"{k:14s} {v}")


def cmd_report(args):
    cfg, day, recs = _collect(args)
    msg = render(day, cfg.get("family", ""), recs)
    save(day, recs, msg, Path(args.out))
    print(msg)
    for r in recs:
        if r.get("error"):
            print(f"[확인 필요] {r['name']}: {r['error']}", file=sys.stderr)
    found = sum(1 for r in recs if r["found"])
    if found < len(recs) / 2:
        print(f"[발송 중단] 재료 {len(recs)}개 중 {found}개만 찾음. "
              f"`python3 kimjang_report.py codes` 결과와 items.json을 맞추세요.", file=sys.stderr)
        return
    if not args.no_send:
        for r in notify.send_all(msg, f"김장 물가 알리미 {day}", variables=template_vars(day, cfg, recs)):
            print(r, file=sys.stderr)


def main():
    p = argparse.ArgumentParser(description="김장 물가 알리미")
    p.add_argument("command", choices=["codes", "report", "vars"])
    p.add_argument("--date", help="기준일 YYYY-MM-DD (기본: 오늘, 주말이면 직전 평일)")
    p.add_argument("--fixture", help="샘플 JSON 폴더 (인터넷 없이 시험)")
    p.add_argument("--out", default=str(BASE / "data"), help="로그 저장 폴더")
    p.add_argument("--no-send", action="store_true", help="저장만 하고 발송하지 않음")
    a = p.parse_args()
    {"codes": cmd_codes, "report": cmd_report, "vars": cmd_vars}[a.command](a)


if __name__ == "__main__":
    main()
