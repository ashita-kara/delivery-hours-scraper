import pandas as pd
import glob
import os

files = glob.glob("chunk_*.csv")
df_list = []
for f in files:
    try: df_list.append(pd.read_csv(f))
    except: pass

# ファイル名を汎用的なものに変更
if os.path.exists("store_master_data.csv"):
    try: df_list.append(pd.read_csv("store_master_data.csv"))
    except: pass

if df_list:
    final_df = pd.concat(df_list, ignore_index=True)
    final_df.drop_duplicates(subset=['URL'], keep='last', inplace=True)
    final_df.to_csv("store_master_data.csv", index=False, encoding="utf-8-sig")
    print(f"✨ {len(files)}個の分割ファイルを合体完了！ 総データ数: {len(final_df)}件")
else:
    print("⚠️ 合体するデータがありませんでした。")