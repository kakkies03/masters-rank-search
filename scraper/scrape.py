"""ジャンボウポイント（ナショナル）のポイントランキングを取得して CSV に保存する。

区分は 男性/女性 × 長水路/短水路 の4つ。年齢・種目は「全て」、重複あり。
出力: scraped/<年>_<区分番号>.csv と scraped/<年>_<区分番号>.meta.json

使い方:
  python scraper/scrape.py                 # 今年分（1〜3月は前年分も）
  python scraper/scrape.py --years 2025,2026
  python scraper/scrape.py --max-pages 3   # お試し（各区分3ページだけ）
"""
import argparse
import csv
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

# 検索フォームは ?page_id=734 のページに iframe で埋め込まれているので、中身のページを直接開く
URL = "https://jambowpoint.jp/jambowpoint-test/point-rank.aspx"
# 区分番号はサイトのデータ形式（0:男長 1:男短 2:女長 3:女短）に合わせる
GROUPS = [(0, "男性", "長水路"), (1, "男性", "短水路"), (2, "女性", "長水路"), (3, "女性", "短水路")]
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "scraped"
DEBUG = ROOT / "debug"
PAGE_DELAY = 1.0          # ページ送りの間隔（秒）。サイトに負荷をかけないため
WAIT_TIMEOUT = 60         # 1ページの読み込みを待つ最大秒数

# ページ内で使う補助関数（ページを読み込むたびに自動で入る）
HELPERS = r"""
window.__jb = {
  clean(s){ return (s || '').replace(/\s+/g, ''); },
  radioText(inp){
    if (inp.id) { const l = document.querySelector(`label[for="${CSS.escape(inp.id)}"]`); if (l) return this.clean(l.innerText); }
    const lab = inp.closest('label'); if (lab) return this.clean(lab.innerText);
    let t = '', n = inp.nextSibling;
    while (n && !(n.nodeType === 1 && ['INPUT', 'SELECT', 'BR'].includes(n.tagName))) { t += n.textContent || ''; n = n.nextSibling; }
    return this.clean(t);
  },
  clickRadio(text){
    const r = [...document.querySelectorAll('input[type=radio]')].find(i => this.radioText(i) === text);
    if (!r) return false;
    r.click(); r.checked = true; r.dispatchEvent(new Event('change', { bubbles: true }));
    return true;
  },
  radioState(text){
    const r = [...document.querySelectorAll('input[type=radio]')].find(i => this.radioText(i) === text);
    return r ? (r.checked ? 'checked' : 'unchecked') : 'missing';
  },
  selectText(name, text){
    const s = document.querySelector(`select[name="${name}"]`);
    if (!s) return 'no-select';
    const o = [...s.options].find(o => this.clean(o.text) === text);
    if (!o) return 'no-option';
    if (s.value === o.value) return 'same';
    s.value = o.value; s.dispatchEvent(new Event('change', { bubbles: true }));
    return 'changed';
  },
  selectYear(y){
    const s = [...document.querySelectorAll('select')].find(s =>
      s.options.length && [...s.options].every(o => /^\d{4}$/.test(o.text.trim())));
    if (!s) return 'no-select';
    const o = [...s.options].find(o => o.text.trim() === String(y));
    if (!o) return 'no-option:' + [...s.options].map(o => o.text.trim()).join(',');
    s.value = o.value; s.dispatchEvent(new Event('change', { bubbles: true }));
    return 'ok';
  },
  mark(text, attr){
    document.querySelectorAll(`[data-jb="${attr}"]`).forEach(e => e.removeAttribute('data-jb'));
    const el = [...document.querySelectorAll('button, a, input[type=button], input[type=submit]')]
      .find(e => this.clean(e.innerText || e.value) === text);
    if (!el) return null;
    const dis = el.disabled || /disabled/.test(el.className) || getComputedStyle(el).pointerEvents === 'none';
    el.setAttribute('data-jb', attr);
    return { disabled: dis };
  },
  read(){
    const t = [...document.querySelectorAll('table')].find(t => {
      const s = t.innerText; return s.includes('順位') && s.includes('タイム') && s.includes('ポイント');
    });
    const cond = ([...document.querySelectorAll('div, h1, h2, h3, h4, p, span')]
      .map(e => e.innerText || '').find(s => s.includes('プール種別') && s.includes('対象人数') && s.length < 300) || '');
    const sel = [...document.querySelectorAll('select')].find(s =>
      s.options.length && s.options[0].text.trim() === '1' && [...s.options].every(o => /^\d+$/.test(o.text.trim())));
    if (!t) return { cond, header: [], rows: [] };
    const trs = [...t.querySelectorAll('tr')];
    const head = trs.find(r => r.querySelector('th'));
    const header = head ? [...head.querySelectorAll('th, td')].map(c => this.clean(c.innerText)) : [];
    const rows = trs.filter(r => r !== head)
      .map(r => [...r.querySelectorAll('td')].map(c => c.innerText.replace(/\s+/g, ' ').trim()))
      .filter(r => r.length);
    return { cond, header, rows,
             page: sel ? sel.selectedIndex + 1 : null, pages: sel ? sel.options.length : null };
  }
};
"""

COLS = {"順位": "rank", "名前": "name", "年齢": "age", "種目": "event",
        "所属": "club", "競技会": "meet", "タイム": "time", "ポイント": "points"}


def log(*a):
    print(*a, flush=True)


def safe_eval(page, expr, arg=None):
    try:
        return page.evaluate(expr, arg) if arg is not None else page.evaluate(expr)
    except Exception:
        return None  # ページ遷移中など


def wait_for(page, ok, what):
    end = time.time() + WAIT_TIMEOUT
    while time.time() < end:
        r = safe_eval(page, "() => window.__jb && window.__jb.read()")
        if r and ok(r):
            return r
        time.sleep(0.4)
    raise RuntimeError(f"{what} を待ちましたが表示されませんでした")


def sig(r):
    return json.dumps([r["rows"][0], r["rows"][-1], len(r["rows"])], ensure_ascii=False)


SEX_LABEL = {"男性": "男子", "女性": "女子"}


def settle(page):
    """ポストバックによる再読み込みが終わるのを待つ"""
    page.wait_for_timeout(800)
    try:
        page.wait_for_load_state("load", timeout=WAIT_TIMEOUT * 1000)
    except Exception:
        pass
    end = time.time() + WAIT_TIMEOUT
    while time.time() < end:
        if safe_eval(page, "() => !!(window.__jb && document.readyState === 'complete')"):
            return
        time.sleep(0.3)


def select_radio(page, text):
    for _ in range(4):
        st = safe_eval(page, "t => window.__jb.radioState(t)", text)
        if st == "checked":
            return
        if st == "unchecked":
            safe_eval(page, "t => window.__jb.clickRadio(t)", text)
        settle(page)
    raise RuntimeError(f"「{text}」を選択できませんでした（状態: {st}）")


def select_dup(page):
    """年齢区分の「重複あり」（同じ選手の複数種目・複数記録をすべて出す）を選ぶ"""
    for _ in range(4):
        st = safe_eval(page, "() => window.__jb.selectText('DDLNenrei2', '重複あり')")
        if st == "same":
            return
        if st in ("no-select", "no-option"):
            raise RuntimeError(f"「重複あり」を選択できませんでした（{st}）")
        settle(page)
    raise RuntimeError("「重複あり」を選択できませんでした")


def scrape_group(page, year, g, sex, pool, max_pages):
    log(f"--- {year}年 {sex} {pool} ---")
    page.goto(URL, wait_until="load", timeout=90_000)
    settle(page)
    # 選択肢を押すたびにページが再読み込み（ポストバック）されるので、1つずつ反映を待つ
    for text in ["ナショナルポイント", "年齢区分", pool, sex, "年単位", "ポイントランキング"]:
        select_radio(page, text)
    select_dup(page)
    res = safe_eval(page, "y => window.__jb.selectYear(y)", str(year))
    if res != "ok":
        raise RuntimeError(f"年の選択に失敗しました ({res})")
    if not safe_eval(page, "() => window.__jb.mark('検索', 'search')"):
        raise RuntimeError("検索ボタンが見つかりません")
    page.click('[data-jb="search"]')

    # 検索結果の条件表示では 男性→男子・女性→女子 と書かれる
    want = [f"性別:{SEX_LABEL[sex]}", f"プール種別:{pool}", f"{year}年"]
    r = wait_for(page, lambda r: r["rows"] and all(w in r["cond"] for w in want), "検索結果")
    m = re.search(r"対象人数[:：]\s*([\d,]+)", r["cond"])
    expected = int(m.group(1).replace(",", "")) if m else None
    log(f"条件: {r['cond'].strip()}  / 期待件数: {expected}  / 全{r['pages']}ページ")

    header = r["header"]
    idx = {COLS[h]: i for i, h in enumerate(header) if h in COLS}
    if len(idx) < len(COLS):
        raise RuntimeError(f"表の見出しが想定と違います: {header}")

    rows, n = [], 0
    while True:
        n += 1
        rows += [{k: row[i] for k, i in idx.items()} for row in r["rows"] if len(row) >= len(header)]
        if n % 20 == 0 or n == 1:
            log(f"  {n}/{r['pages'] or '?'} ページ  累計 {len(rows)} 行")
        if max_pages and n >= max_pages:
            break
        if expected and len(rows) >= expected:
            break
        if r["page"] and r["pages"] and r["page"] >= r["pages"]:
            break
        nb = safe_eval(page, "() => window.__jb.mark('>', 'next')")
        if not nb or nb["disabled"]:
            break
        prev = sig(r)
        time.sleep(PAGE_DELAY)
        for attempt in range(3):
            page.click('[data-jb="next"]')
            try:
                r = wait_for(page, lambda x: x["rows"] and sig(x) != prev, f"{n + 1}ページ目")
                break
            except RuntimeError:
                if attempt == 2:
                    raise
                log(f"  {n + 1}ページ目の読み込みをやり直します")
                safe_eval(page, "() => window.__jb.mark('>', 'next')")

    log(f"  取得 {len(rows)} 行（期待 {expected}）")
    if not max_pages and expected and len(rows) < expected * 0.98:
        raise RuntimeError(f"取得件数が足りません: {len(rows)} / {expected}")

    OUT.mkdir(exist_ok=True)
    with open(OUT / f"{year}_{g}.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(COLS.values()))
        w.writeheader()
        w.writerows(rows)
    (OUT / f"{year}_{g}.meta.json").write_text(json.dumps(
        {"year": year, "group": g, "sex": sex, "pool": pool, "expected": expected,
         "rows": len(rows), "complete": not max_pages}, ensure_ascii=False), encoding="utf-8")


def default_years():
    now = datetime.now(timezone(timedelta(hours=9)))
    return [now.year - 1, now.year] if now.month <= 3 else [now.year]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", default="")
    ap.add_argument("--max-pages", type=int, default=0)
    a = ap.parse_args()
    years = [int(y) for y in re.split(r"[,\s]+", a.years.strip()) if y] or default_years()
    log(f"取得する年: {years}")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(locale="ja-JP", viewport={"width": 1280, "height": 900})
        ctx.add_init_script(HELPERS)
        page = ctx.new_page()
        try:
            for y in years:
                for g, sex, pool in GROUPS:
                    scrape_group(page, y, g, sex, pool, a.max_pages)
        except Exception as e:
            DEBUG.mkdir(exist_ok=True)
            try:
                page.screenshot(path=str(DEBUG / "error.png"), full_page=True)
                (DEBUG / "error.html").write_text(page.content(), encoding="utf-8")
            except Exception:
                pass
            log(f"エラー: {e}")
            sys.exit(1)
        finally:
            browser.close()


if __name__ == "__main__":
    main()
