import asyncio, re, sys, os, time, json, unicodedata, random, ssl, certifi, argparse
from datetime import datetime

# ==========================================
# 🛑 基本設定
# ==========================================
TARGET_URL = "https://www.ubereats.com/jp" 
DATABASE_FILE = "uber_master_database.csv"
AREAS_FILE = "target_areas.txt"

EXCLUDE_KEYWORDS = [
    "ドミノ・ピザ", "ピザハット", "ピザーラ", "マクドナルド",
    "ローソン", "ファミリーマート", "セブン-イレブン", "ミニストップ", "デイリーヤマザキ",
    "マルエツ", "成城石井", "ピーコック", "まいばすけっと", "ライフ", "サミット", "ビオラル",
    "ウエルシア", "マツモトキヨシ", "ココカラファイン", "スギ薬局", "コクミン",
]

try:
    import pandas as pd
    from geopy.geocoders import Nominatim 
    import geopy.adapters
except ImportError:
    print("❌️ エラー: ライブラリ不足")
    sys.exit(1)

from playwright.async_api import async_playwright

ctx = ssl.create_default_context(cafile=certifi.where())
geopy.geocoders.options.default_ssl_context = ctx
geolocator = Nominatim(user_agent=f"my_app_{random.randint(10000,99999)}", timeout=20)

DAY_MAP = {
    "Monday": "月曜日", "Tuesday": "火曜日", "Wednesday": "水曜日", 
    "Thursday": "木曜日", "Friday": "金曜日", "Saturday": "土曜日", "Sunday": "日曜日"
}

def clean_address_variants(raw):
    if not raw: return []
    base = unicodedata.normalize('NFKC', raw)
    base = re.sub(r'〒\d{3}-\d{4}|日本、?', '', base).strip()
    variants = []
    match = re.search(r'^(.*?[\d]+[\-][\d]+(?:[\-][\d]+)?)', base)
    v1 = match.group(1).strip() if match else base.split(' ')[0]
    variants.append(v1)
    if re.search(r'\d+-\d+', v1): variants.append(re.sub(r'(\d+)-', r'\1丁目', v1, count=1))
    return list(dict.fromkeys(variants))

def get_lat_lon_from_address(addr):
    if not addr or addr == "取得不可": return None, None, "住所なし"
    for pat in clean_address_variants(addr):
        for _ in range(2):
            try:
                loc = geolocator.geocode(pat)
                if loc: return loc.latitude, loc.longitude, "OK"
            except Exception: time.sleep(5)
        time.sleep(1)
    return None, None, "見つかりません"

def extract_ward(addr):
    m = re.search(r'([^\s]+区)', str(addr))
    return m.group(1) if m else "その他"

async def get_data_hybrid(page):
    j_addr, j_hours, j_lat, j_lon = None, None, None, None
    try:
        data = await page.evaluate("""() => {
            const scripts = document.querySelectorAll('script[type="application/ld+json"]');
            for (const s of scripts) {
                try {
                    const json = JSON.parse(s.innerText);
                    if (json['@type'] === 'Restaurant' || json['@type'] === 'FastFoodRestaurant' || json['address']) return json;
                } catch (e) {}
            }
            return null;
        }""")
        
        if data:
            if 'address' in data:
                a = data['address']
                if isinstance(a, dict):
                    j_addr = f"{a.get('addressRegion','')} {a.get('addressLocality','')} {a.get('streetAddress','')}".strip()
            
            if 'openingHoursSpecification' in data:
                specs = data['openingHoursSpecification']
                if isinstance(specs, dict): specs = [specs]
                
                h = []
                for s in specs:
                    day_raw = s.get('dayOfWeek', [])
                    if not isinstance(day_raw, list): day_raw = [day_raw]
                    opens = s.get('opens', '')
                    closes = s.get('closes', '')
                    if opens and closes:
                        for d_item in day_raw:
                            d_jp = DAY_MAP.get(str(d_item).split('/')[-1], str(d_item).split('/')[-1])
                            if d_jp: h.append(f"{d_jp} {opens} - {closes}")
                if h: j_hours = "<br>".join(h)
            
            if 'geo' in data:
                geo = data['geo']
                if 'latitude' in geo and 'longitude' in geo:
                    j_lat, j_lon = float(geo['latitude']), float(geo['longitude'])
    except Exception: pass

    s_addr, s_hours = None, None
    if not j_addr:
        try:
            el = page.locator('button').filter(has_text=re.compile(r"[都道府県].+?[市区町村]")).first
            if await el.is_visible(): s_addr = (await el.inner_text()).replace('\n',' ').strip()
        except: pass
    if not j_hours:
        try:
            txt = await page.locator("body").inner_text()
            reg = re.compile(r'(\d{1,2}:\d{2})\s*[-–〜]\s*(\d{1,2}:\d{2})')
            h = [l.strip() for l in txt.split('\n') if reg.search(l) and len(l)<50]
            if h: s_hours = "<br>".join(h)
        except: pass
    
    return j_addr or s_addr or "取得不可", j_hours or s_hours or "取得不可", j_lat, j_lon

# 🚀 引数に update_chunk を追加
async def scrape_uber_eats(mode="scrape", target_areas=[], update_chunk=pd.DataFrame()):
    df = pd.DataFrame()
    if os.path.exists(DATABASE_FILE):
        df = pd.read_csv(DATABASE_FILE)
        df = df[df['Name'].apply(lambda x: not any(k in str(x) for k in EXCLUDE_KEYWORDS))]

    stores = []
    seen = set(df['URL'].tolist()) if not df.empty else set()

    # ▼ 新規検索モード
    if mode == "scrape" and target_areas:
        async with async_playwright() as p:
            for i, area in enumerate(target_areas):
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context(
                    locale='ja-JP', 
                    geolocation={"latitude":35.69,"longitude":139.70}, 
                    permissions=["geolocation"],
                    viewport={'width': 1280, 'height': 800},
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
                )
                page = await context.new_page()
                
                print(f"\n🔍 [{i+1}/{len(target_areas)}] エリア検索: {area}")
                try:
                    await page.goto(TARGET_URL, timeout=60000)
                    await asyncio.sleep(2)
                    
                    try:
                        print(f"   🤖 人間らしいタイピングで自動入力中...")
                        search_box = page.locator('input[placeholder*="住所"], input[placeholder*="配達先"]').first
                        await search_box.wait_for(state="visible", timeout=5000)
                        await search_box.click()
                        await search_box.fill("")
                        await search_box.press_sequentially(area, delay=100)
                        
                        print(f"   ⏳ サジェスト待機中 (3秒)...")
                        await asyncio.sleep(3) 
                        await page.keyboard.press("ArrowDown")
                        await asyncio.sleep(0.5)
                        await page.keyboard.press("Enter")
                        
                        print(f"   ✅ 候補を選択しました！お店の読み込みを待ちます...")
                        await asyncio.sleep(6)
                    except Exception as e:
                        print(f"   ⚠️ 自動操作ストップ (検索窓が見つからない等)")
                        continue

                    for _ in range(30):
                        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        await asyncio.sleep(1)
                        try: await page.locator("button:has-text('さらに表示')").click(timeout=1000)
                        except: pass
                        
                    store_nodes = await page.evaluate(r"""() => {
                        let items = [];
                        let links = Array.from(document.querySelectorAll('a[href*="/store/"]'));
                        let uniqueUrls = [...new Set(links.map(a => a.href))];
                        
                        for (let url of uniqueUrls) {
                            let storeLinks = links.filter(a => a.href === url);
                            let a = storeLinks.find(a => a.innerText.trim().length > 0) || storeLinks[0];
                            let name = a.innerText.trim().split(/\r?\n/)[0];
                            if (!name) continue;
                            
                            let node = a;
                            let cardText = "";
                            
                            for (let i = 0; i < 12; i++) {
                                if (!node || node === document.body) break;
                                let linksInNode = Array.from(node.querySelectorAll('a[href*="/store/"]'));
                                let hasOtherStores = linksInNode.some(link => link.href !== url);
                                if (hasOtherStores) break;
                                cardText = node.innerText || "";
                                node = node.parentElement;
                            }
                            items.push({ name: name, url: url, text: cardText });
                        }
                        return items;
                    }""")
                    
                    added = 0
                    print(f"   👀 画面から {len(store_nodes)} 店舗のデータを抽出中...")
                    for item in store_nodes:
                        try:
                            name = item["name"]
                            if any(k in name for k in EXCLUDE_KEYWORDS): continue
                            
                            url = item["url"].split('?')[0]
                            if url in seen: continue
                            
                            txt = item["text"]
                            if not txt: continue
                            
                            sc_match = re.search(r'(\d\.\d)', txt)
                            cnt_match = re.search(r'\((\d[\d,]*)\+?\)', txt)
                            if not cnt_match: cnt_match = re.search(r'(\d[\d,]*)\+?\s*件', txt)
                            
                            sc = float(sc_match.group(1)) if sc_match else 0.0
                            cnt = int(cnt_match.group(1).replace(',', '')) if cnt_match else 0
                            
                            if cnt >= 800 or (sc >= 4.7 and cnt >= 100):
                                stores.append({"SearchOrigin":area, "Name":name, "RatingCount":cnt, "RatingScore":sc, "URL":url, "LastSeen":datetime.now().strftime('%Y-%m-%d')})
                                seen.add(url)
                                added += 1
                                print(f"      ✨ 追加: {name[:15]}... [星:{sc} レビュー:{cnt}]")
                        except Exception: continue
                    print(f"   -> {added}件 追加")
                except Exception as e: print(f"エラー: {e}")
                await browser.close()

    # ▼ 更新確認モード（分割されたCSVデータを直接叩く）
    if mode == "update" and not update_chunk.empty:
        stores.extend(update_chunk.to_dict('records'))
        
    if not stores: 
        print("❌ 対象なし")
        return

    print(f"\n=== 詳細データ取得開始 (超爆速APIモード) ===")
    final_list = []
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(locale='ja-JP')
        page = await context.new_page()
        
        for i, s in enumerate(stores):
            try:
                await context.clear_cookies()
                try: await page.goto(s['URL'], timeout=30000)
                except: pass
                await asyncio.sleep(1.5)
                
                if "ページが見つかりません" in await page.title():
                    print(f"[{i+1}/{len(stores)}] 🗑️ 閉店検知: {s['Name'][:15]}...")
                    continue 
                
                addr, hours, lat, lon = await get_data_hybrid(page)
                if addr == "取得不可" and "Address" in s: addr = s["Address"]
                
                loc_src, geo_msg = "Unknown", ""
                if lat is not None and lon is not None: loc_src, geo_msg = "UberData", "🎯GPS直抜"
                elif addr != "取得不可":
                    if "LocationSource" in s and s["LocationSource"] == "UberData":
                         lat, lon, loc_src, geo_msg = s["Latitude"], s["Longitude"], "UberData", "🎯維持"
                    else:
                        lat, lon, msg = get_lat_lon_from_address(addr)
                        loc_src, geo_msg = "Geocoding", f"📍住所変換({msg})"
                        time.sleep(1)
                
                s.update({"Address": addr, "WeeklyHours": hours, "Latitude": lat, "Longitude": lon, "LastSeen": datetime.now().strftime('%Y-%m-%d'), "LocationSource": loc_src})
                final_list.append(s)
                print(f"[{i+1}/{len(stores)}] 更新: {s['Name'][:15]}... [🏠{addr[:3] if addr else 'NG'} {geo_msg} {'⏰時間OK' if hours and len(hours)>30 else '⏰時間短'}]")
            except Exception as e: 
                print(f"[{i+1}/{len(stores)}] エラー: {e}")
                final_list.append(s) 
        await browser.close()

    if final_list:
        new_df = pd.DataFrame(final_list)
        new_df['Ward'] = new_df['Address'].apply(extract_ward)
        if mode == "scrape" and not df.empty:
            new_df = pd.concat([df, new_df], ignore_index=True).drop_duplicates(subset=['URL'], keep='last')
        new_df.to_csv(DATABASE_FILE, index=False, encoding="utf-8-sig")
        print(f"\n✨ 保存完了: {DATABASE_FILE} (合計 {len(new_df)}件)")

# ==========================================
# 🎮 コマンドライン引数と起動制御
# ==========================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Uber Eats Scraper")
    parser.add_argument("--mode", type=str, choices=["scrape", "update"], default="scrape")
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--end", type=int, default=None)
    args = parser.parse_args()

    if args.mode == "scrape":
        target_areas = []
        if os.path.exists(AREAS_FILE):
            with open(AREAS_FILE, "r", encoding="utf-8") as f:
                target_areas = [line.strip() for line in f if line.strip() and not line.startswith("#")]
        
        slice_end = args.end if args.end is not None else len(target_areas)
        chunked_areas = target_areas[args.start:slice_end]
        print(f"▶️ 新規開拓モード: 全{len(target_areas)}エリア中、 {args.start}行目 〜 {slice_end}行目 を実行します")
        asyncio.run(scrape_uber_eats(mode="scrape", target_areas=chunked_areas))

    elif args.mode == "update":
        df = pd.DataFrame()
        if os.path.exists(DATABASE_FILE):
            df = pd.read_csv(DATABASE_FILE)
            
        if df.empty:
            print("❌ 更新対象のデータがありません。")
        else:
            slice_end = args.end if args.end is not None else len(df)
            chunked_df = df.iloc[args.start:slice_end]
            print(f"▶️ 更新・生存確認モード: 全{len(df)}店舗中、 {args.start}件目 〜 {slice_end}件目 を実行します")
            asyncio.run(scrape_uber_eats(mode="update", update_chunk=chunked_df))
