#!/usr/bin/env python3
"""김장 물가 알리미 — KAMIS Open API 소매가격으로 주간 김장 물가 리포트를 만든다.

사용법
  python kimjang_report.py codes              # 분류별 품목 이름·단위 확인 (items.json 맞출 때)
  python kimjang_report.py report             # 리포트 생성 + 로그 저장
  python kimjang_report.py report --date 2026-09-28
  python kimjang_report.py report --fixture sample   # 인터넷 없이 샘플로 시험

환경변수
  KAMIS_CERT_KEY, KAMIS_CERT_ID   (필수, 인증키와 KAMIS 회원 아이디)
  발송 채널 설정은 notify.py 참고 (문자·메일·텔레그램, 없으면 저장만)
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


def latest_business_day(target, categories, fixture=None, max_back=7):
    """주말·공휴일엔 가격이 없으므로 데이터가 있는 가장 가까운 과거 날짜를 찾는다."""
    for back in range(max_back + 1):
        day = target - dt.timedelta(days=back)
        rows = {c: fetch_category(c, day.isoformat(), fixture) for c in categories}
        if any(rows.values()):
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


def cmd_report(args):
    cfg = json.loads((BASE / "items.json").read_text(encoding="utf-8"))
    cats = sorted({i["category"] for i in cfg["items"]})
    day = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    day, rows = latest_business_day(day, cats, args.fixture)
    recs = build(cfg["items"], rows)
    msg = render(day, cfg.get("family", ""), recs)
    save(day, recs, msg, Path(args.out))
    print(msg)
    for r in recs:
        if r.get("error"):
            print(f"[확인 필요] {r['name']}: {r['error']}", file=sys.stderr)
    if not args.no_send:
        for r in notify.send_all(msg, f"김장 물가 알리미 {day}"):
            print(r, file=sys.stderr)


def main():
    p = argparse.ArgumentParser(description="김장 물가 알리미")
    p.add_argument("command", choices=["codes", "report"])
    p.add_argument("--date", help="기준일 YYYY-MM-DD (기본: 오늘, 주말이면 직전 평일)")
    p.add_argument("--fixture", help="샘플 JSON 폴더 (인터넷 없이 시험)")
    p.add_argument("--out", default=str(BASE / "data"), help="로그 저장 폴더")
    p.add_argument("--no-send", action="store_true", help="저장만 하고 발송하지 않음")
    a = p.parse_args()
    cmd_codes(a) if a.command == "codes" else cmd_report(a)


if __name__ == "__main__":
    main()
