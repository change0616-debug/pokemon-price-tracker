"""
有料プラン(API plan, 1日20,000クレジット, 6ヶ月分の価格履歴)向けのスクリプト。

各キャラクターについて、海外版(英語)・日本版それぞれの上位8枚のカードを取得し、
PSA10(鑑定済み・満点評価)の価格推移を基準に、3ヶ月前と今日を比較する。
3ヶ月で+100%(2倍)以上 かつ 上昇額+5000円以上のカードだけを抽出し、
「継続上昇」か「直近急騰」かも判定した上で data/summary.json に書き出す。
過去の結果は data/archive/ 以下に日付ごとに保存し、振り返れるようにする。

検索は英語名で行う(このAPIは英語名での検索を前提にしているため。
name_map.json に日本語名→英語名の対応表がある)。

PSA10の価格データは、実際のAPI応答で確認した「ebay.psa10.<日付>.average」
という日付キーの辞書形式から取り出す。

20キャラ処理するごとに、その時点までの結果を先にGitHubへコミットして
サイトに反映する(全部終わるまで待たせない)。

【安全装置】
1日のクレジット上限に近づいた/使い切ったとみられるエラー(402)が返ってきたら、
そこで処理を打ち切り、それまでに集まった分だけで summary.json を作る。
一時的なアクセス過多(429)は、日の上限とは区別して少し待ってリトライする。
PSA10データが1件も見つからない場合はエラー終了し、
ntfy.sh経由のスマホ通知(別ステップ)が発火するようにする。
"""
import datetime
import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

API_KEY = os.environ["POKEMON_PRICE_API_KEY"]
API_BASE = "https://www.pokemonpricetracker.com/api/v2/cards"

CHARACTERS_FILE = "characters.json"
NAME_MAP_FILE = "name_map.json"
SUMMARY_FILE = "data/summary.json"
ARCHIVE_DIR = "data/archive"
ARCHIVE_INDEX_FILE = "data/archive/index.json"
DEBUG_FILE = "data/debug/latest_sample.json"
ARCHIVE_KEEP_DAYS = 120  # 古いアーカイブはこれより前のものを削除してリポジトリを軽く保つ

CARDS_PER_CHARACTER = 8    # 1日の credit 予算(20,000)に収まるよう調整済み
HISTORY_DAYS = 100          # 3ヶ月(90日)より少し多めに取得して余裕を持たせる
TARGET_DAYS_AGO = 90        # 「3ヶ月前」の定義
MIN_CHANGE_PCT = 100.0      # 2倍以上 = +100%以上
MIN_YEN_INCREASE = 5000     # 上昇額が5000円未満のものはノイズとして除外
FALLBACK_USD_JPY = 150.0    # 為替レート取得に失敗した場合の保険用レート
DEBUG_SAMPLE_LIMIT = 1      # 生データを保存するキャラクター数(ファイルサイズを抑えるため最小限に)
CHECKPOINT_EVERY = 20       # これだけキャラを処理するごとに、途中経過をサイトに反映する

MARKETS = [
    {"key": "overseas", "label": "海外(英語版)", "language": None},
    {"key": "japan", "label": "日本版", "language": "japanese"},
]


class CreditLimitReached(Exception):
    pass


def load_characters():
    with open(CHARACTERS_FILE, encoding="utf-8") as f:
        return json.load(f)


def load_name_map():
    with open(NAME_MAP_FILE, encoding="utf-8") as f:
        return json.load(f)


def fetch_usd_jpy_rate():
    """USD→JPYの為替レートを取得する(取得できなければ保険のレートを使う)"""
    try:
        url = "https://api.frankfurter.dev/v1/latest?from=USD&to=JPY"
        with urllib.request.urlopen(url, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        rate = body.get("rates", {}).get("JPY")
        if rate:
            return float(rate)
    except Exception as e:
        print(f"  [warn] 為替レート取得に失敗、保険のレート({FALLBACK_USD_JPY})を使います: {e}")
    return FALLBACK_USD_JPY


def fetch_cards(search_name, language):
    """PSA10のデータも含めてカードを検索する(includeEbay=trueでPSA情報を要求)
    search_name には英語名を渡す(このAPIは英語名での検索を前提にしているため)"""
    params = {
        "search": search_name,
        "sortBy": "price",
        "sortOrder": "desc",
        "limit": str(CARDS_PER_CHARACTER),
        "includeHistory": "true",
        "includeEbay": "true",
        "days": str(HISTORY_DAYS),
    }
    if language:
        params["language"] = language

    url = API_BASE + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {API_KEY}"})

    max_retries = 2
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            return body.get("data") or [], body
        except urllib.error.HTTPError as e:
            if e.code == 402:
                # 402: 支払い/1日のクレジット上限に関するエラー → 本当の上限とみなす
                raise CreditLimitReached(f"HTTP {e.code} for {search_name} ({language})")
            if e.code == 429:
                # 429: 一時的なアクセス過多。サーバー指定の待ち時間(Retry-After)があれば
                # それを優先し、無ければ短めの待ち時間でリトライする(無駄な長時間待ちを避ける)
                retry_after = e.headers.get("Retry-After") if e.headers else None
                if retry_after and retry_after.isdigit():
                    wait_seconds = min(int(retry_after) + 1, 15)
                else:
                    wait_seconds = 6 * (attempt + 1)
                print(f"  [warn] {search_name} ({language}): HTTP 429、{wait_seconds}秒待ってリトライします")
                time.sleep(wait_seconds)
                continue
            print(f"  [error] {search_name} ({language}): HTTP {e.code}")
            return [], None
        except Exception as e:
            print(f"  [error] {search_name} ({language}): {e}")
            return [], None

    # 429のリトライを規定回数繰り返しても解決しなかった場合
    print(f"  [warn] {search_name} ({language}): 429のリトライが上限に達したためスキップします")
    return [], None


def find_ebay_container(card):
    """PSA10などのeBay販売統計が入っているコンテナを探す(名前が複数考えられるため)"""
    for key in ["ebay", "ebaySales", "salesHistory", "gradedSales", "sales"]:
        value = card.get(key)
        if isinstance(value, dict) and isinstance(value.get("psa10"), dict):
            return value
    return None


def get_psa10_points(card):
    """PSA10の「日付→その日の平均価格」を、{date, price}のリストに変換する"""
    container = find_ebay_container(card)
    if not container:
        return None
    psa10 = container.get("psa10")
    if not isinstance(psa10, dict) or not psa10:
        return None

    points = []
    for date_str, stats in psa10.items():
        if not isinstance(stats, dict):
            continue
        avg = stats.get("average")
        if isinstance(avg, (int, float)) and avg > 0:
            points.append({"date": date_str[:10], "price": float(avg)})

    if not points:
        return None
    points.sort(key=lambda p: p["date"])
    return points


def find_price_at(history_points, target_date):
    """target_date以前で一番近い日付の価格を探す。無ければ一番古い点で代用する"""
    best = None
    for point in history_points:
        date_str = point.get("date") if isinstance(point, dict) else None
        if not date_str:
            continue
        try:
            p_date = datetime.date.fromisoformat(date_str[:10])
        except ValueError:
            continue
        if p_date <= target_date:
            if best is None or p_date > datetime.date.fromisoformat(best["date"][:10]):
                best = point
    if best is None:
        candidates = [p for p in history_points if isinstance(p, dict) and p.get("date")]
        if not candidates:
            return None
        candidates.sort(key=lambda p: p["date"])
        best = candidates[0]

    price = best.get("price")
    if price is None:
        price = (best.get("tcgplayer") or {}).get("market")
    if not price or price <= 0:
        return None
    return {"price": float(price), "date": best.get("date", "")[:10]}


def classify_trend(p90, p60, p30, p_now):
    """3ヶ月間の値上がりが、継続的な上昇か、直近だけの急騰かを判定する"""
    total_gain = p_now - p90
    if total_gain <= 0:
        return "unknown"

    gain_90_to_60 = p60 - p90
    gain_60_to_30 = p30 - p60
    gain_30_to_now = p_now - p30

    if gain_30_to_now >= total_gain * 0.7:
        return "spike"
    if gain_90_to_60 > 0 and gain_60_to_30 > 0 and gain_30_to_now > 0:
        return "sustained"
    return "mixed"


def downsample(history_points, max_points=25):
    """グラフ表示用に、日次データを間引いて軽くする"""
    clean = []
    for p in history_points:
        if not isinstance(p, dict):
            continue
        date_str = p.get("date")
        price = p.get("price")
        if price is None:
            price = (p.get("tcgplayer") or {}).get("market")
        if date_str and isinstance(price, (int, float)) and price > 0:
            clean.append({"date": date_str[:10], "price": round(float(price), 2)})
    clean.sort(key=lambda p: p["date"])
    if len(clean) <= max_points:
        return clean
    step = len(clean) / max_points
    return [clean[int(i * step)] for i in range(max_points)] + [clean[-1]]


def extract_price_change(card):
    """カード1件分のデータから、PSA10の90/60/30日前と今日の価格を取り出し、変化率とトレンドを判定する"""
    history_points = get_psa10_points(card)
    if not history_points:
        return None

    # 一番新しい日付の平均価格を「現在価格」として使う
    current = history_points[-1]["price"]
    if current <= 0:
        return None

    today = datetime.date.today()
    p90 = find_price_at(history_points, today - datetime.timedelta(days=90))
    p60 = find_price_at(history_points, today - datetime.timedelta(days=60))
    p30 = find_price_at(history_points, today - datetime.timedelta(days=30))
    if not p90:
        return None

    old_price = p90["price"]
    if old_price <= 0:
        return None

    change_pct = (current - old_price) / old_price * 100
    trend = classify_trend(
        old_price,
        p60["price"] if p60 else old_price,
        p30["price"] if p30 else old_price,
        current,
    )

    return {
        "old_price": round(old_price, 2),
        "new_price": round(current, 2),
        "change_pct": round(change_pct, 1),
        "old_date": p90["date"],
        "trend": trend,
        "chart": downsample(history_points),
    }


def save_debug_sample(samples):
    os.makedirs(os.path.dirname(DEBUG_FILE), exist_ok=True)
    with open(DEBUG_FILE, "w", encoding="utf-8") as f:
        json.dump(samples, f, ensure_ascii=False, indent=2)


def save_archive(summary):
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    date_str = summary["generated_at"]
    with open(f"{ARCHIVE_DIR}/{date_str}.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    if os.path.exists(ARCHIVE_INDEX_FILE):
        with open(ARCHIVE_INDEX_FILE, encoding="utf-8") as f:
            index = json.load(f)
    else:
        index = []
    if date_str not in index:
        index.append(date_str)
    index.sort()

    cutoff = (datetime.date.today() - datetime.timedelta(days=ARCHIVE_KEEP_DAYS)).isoformat()
    kept = []
    for d in index:
        if d < cutoff:
            old_file = f"{ARCHIVE_DIR}/{d}.json"
            if os.path.exists(old_file):
                os.remove(old_file)
        else:
            kept.append(d)

    with open(ARCHIVE_INDEX_FILE, "w", encoding="utf-8") as f:
        json.dump(kept, f, ensure_ascii=False, indent=2)


def git_checkpoint_commit(message):
    """途中経過を今すぐGitHub上に反映する(失敗しても処理は止めない)"""
    try:
        subprocess.run(["git", "add", "data/"], check=True, timeout=30)
        result = subprocess.run(["git", "commit", "-m", message], timeout=30)
        if result.returncode != 0:
            # 差分が無い場合などはコミットが失敗するが、これは問題ない
            return
        subprocess.run(["git", "push"], check=True, timeout=60)
        print(f"  [info] 途中経過をコミットしました: {message}")
    except Exception as e:
        print(f"  [warn] 途中経過のコミットに失敗しましたが、処理は続けます: {e}")


def build_summary(results, total_characters, usd_jpy_rate, checked_count, stopped_early, psa10_data_found):
    results_sorted = sorted(results, key=lambda r: r["change_pct"], reverse=True)
    return {
        "generated_at": datetime.date.today().isoformat(),
        "target_days": TARGET_DAYS_AGO,
        "min_change_pct": MIN_CHANGE_PCT,
        "min_yen_increase": MIN_YEN_INCREASE,
        "usd_jpy_rate": usd_jpy_rate,
        "checked_characters": checked_count,
        "total_characters": total_characters,
        "stopped_early": stopped_early,
        "psa10_data_found": psa10_data_found,
        "cards": results_sorted,
    }


def write_and_save(summary):
    os.makedirs("data", exist_ok=True)
    with open(SUMMARY_FILE, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    save_archive(summary)


def main():
    characters = load_characters()
    name_map = load_name_map()
    usd_jpy_rate = fetch_usd_jpy_rate()
    print(f"USD/JPY rate: {usd_jpy_rate}")

    results = []
    debug_samples = []
    stopped_early = False
    psa10_data_found = False
    i = -1

    for i, character in enumerate(characters):
        search_name = name_map.get(character, character)
        for market in MARKETS:
            try:
                cards, raw_body = fetch_cards(search_name, market["language"])
            except CreditLimitReached as e:
                print(f"credit limit reached, stopping early: {e}")
                stopped_early = True
                break

            if i < DEBUG_SAMPLE_LIMIT and raw_body is not None:
                trimmed_body = dict(raw_body)
                trimmed_body["data"] = (raw_body.get("data") or [])[:2]  # 軽量化のため2枚分だけ保存
                debug_samples.append(
                    {"character": character, "search_name": search_name, "market": market["key"], "raw_response": trimmed_body}
                )

            for card in cards:
                change = extract_price_change(card)
                if change:
                    psa10_data_found = True
                if not change or change["change_pct"] < MIN_CHANGE_PCT:
                    continue

                yen_increase = (change["new_price"] - change["old_price"]) * usd_jpy_rate
                if yen_increase < MIN_YEN_INCREASE:
                    continue

                results.append(
                    {
                        "character": character,
                        "market": market["label"],
                        "market_key": market["key"],
                        "card_name": card.get("name"),
                        "set_name": card.get("setName"),
                        "rarity": card.get("rarity"),
                        "yen_increase": round(yen_increase),
                        **change,
                    }
                )
            time.sleep(1.5)  # レート制限に余裕を持たせるための間隔
        if stopped_early:
            break

        # 途中経過をこまめにサイトへ反映する(全部終わるまで待たせない)
        if (i + 1) % CHECKPOINT_EVERY == 0:
            save_debug_sample(debug_samples)
            checkpoint_summary = build_summary(
                results, len(characters), usd_jpy_rate, i + 1, False, psa10_data_found
            )
            write_and_save(checkpoint_summary)
            git_checkpoint_commit(f"Checkpoint: {i + 1}/{len(characters)} characters processed")

    save_debug_sample(debug_samples)

    if not psa10_data_found:
        print(
            "[warn] PSA10のデータが1件も見つかりませんでした。"
            f" {DEBUG_FILE} の中身を確認し、必要ならget_psa10_points/"
            "find_ebay_containerの探索先を実際の構造に合わせて修正してください。"
        )

    checked_count = i + (0 if stopped_early else 1)
    summary = build_summary(
        results, len(characters), usd_jpy_rate, checked_count, stopped_early, psa10_data_found
    )
    write_and_save(summary)

    print(f"wrote {len(results)} qualifying cards ({'一部のみ' if stopped_early else '全件'} 処理)")

    if not psa10_data_found:
        raise SystemExit(
            "PSA10 data not found in any card. Check data/debug/latest_sample.json."
        )


if __name__ == "__main__":
    main()
