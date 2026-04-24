import requests
import pandas as pd
import streamlit as st
import altair as alt
from pathlib import Path
import streamlit.components.v1 as components
from concurrent.futures import ThreadPoolExecutor, as_completed

st.set_page_config(page_title="温度データ ダッシュボード", layout="wide")
st.title("📊 おんどとり 温度ダッシュボード")
AUTO_REFRESH_SECONDS = 60
AUTO_REFRESH_DELAY_SECONDS = 10 * 60

URL = "https://api.webstorage.jp/v1/devices/data-rtr500"
REQUEST_TIMEOUT = (5, 12)
MAX_WORKERS = 4
HEADERS = {
    "Content-Type": "application/json",
    "X-HTTP-Method-Override": "GET",
}
PAYLOAD = {
    "api-key": "",
    "login-id": "",
    "login-pass": "",
    "base-serial": "",
}
# ここに取得したい子機のリモートシリアルを追加（例: 3台）
REMOTE_SERIALS = [
    "52824458",
    "5282445B",
    "528244B2",
    "528244E7",
]
HISTORY_CSV = Path(".streamlit/ondotori_history.csv")
MAX_CHART_POINTS_PER_SERIAL = 1200


def load_data(start_ts: pd.Timestamp, end_ts: pd.Timestamp, from_by_serial: dict[str, pd.Timestamp] | None = None) -> pd.DataFrame:
    valid_serials = [s for s in REMOTE_SERIALS if not s.startswith("REMOTE_SERIAL_")]
    if not valid_serials:
        return pd.DataFrame(columns=["time", "temp", "remote_serial"])

    def fetch_remote_rows(remote_serial: str) -> list[dict]:
        fetch_from = start_ts
        if from_by_serial and remote_serial in from_by_serial:
            fetch_from = from_by_serial[remote_serial]
        number = 65535 if fetch_from == start_ts else 5000
        payload = {
            **PAYLOAD,
            "remote-serial": remote_serial,
            "unixtime-from": int(fetch_from.timestamp()),
            "unixtime-to": int(end_ts.timestamp()),
            "number": number,
        }
        response = requests.post(URL, headers=HEADERS, json=payload, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        one_rows = []
        for item in data.get("data", []):
            unixtime = item.get("unixtime")
            ch1 = item.get("ch1")
            if unixtime is None or ch1 is None:
                continue

            try:
                one_rows.append(
                    {
                        # APIのunixtime(UTC)を日本時間に変換して保存
                        "time": pd.to_datetime(int(unixtime), unit="s", utc=True)
                        .tz_convert("Asia/Tokyo")
                        .tz_localize(None),
                        "temp": float(ch1),
                        "remote_serial": remote_serial,
                    }
                )
            except (ValueError, TypeError):
                continue
        return one_rows

    rows = []
    errors = []
    worker_count = min(MAX_WORKERS, len(valid_serials))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {executor.submit(fetch_remote_rows, serial): serial for serial in valid_serials}
        for future in as_completed(futures):
            serial = futures[future]
            try:
                rows.extend(future.result())
            except requests.RequestException as e:
                errors.append(f"{serial}: {e}")

    if not rows:
        if errors:
            raise requests.RequestException(" / ".join(errors))
        return pd.DataFrame(columns=["time", "temp", "remote_serial"])

    return pd.DataFrame(rows).sort_values(["time", "remote_serial"])


def load_api_config() -> dict:
    required_keys = ["api-key", "login-id", "login-pass", "base-serial"]
    cfg = PAYLOAD.copy()

    secret_block = st.secrets.get("ondotori", {})
    secret_key_map = {
        "api-key": "api_key",
        "login-id": "login_id",
        "login-pass": "login_pass",
        "base-serial": "base_serial",
    }
    for payload_key, secret_key in secret_key_map.items():
        value = secret_block.get(secret_key)
        if isinstance(value, str) and value.strip():
            cfg[payload_key] = value.strip()

    missing = [k for k in required_keys if not cfg.get(k)]
    if missing:
        raise ValueError(
            "secrets が未設定です。.streamlit/secrets.toml の [ondotori] に "
            "api_key, login_id, login_pass, base_serial を設定してください。"
        )
    return cfg


def load_history_csv() -> pd.DataFrame:
    if not HISTORY_CSV.exists():
        return pd.DataFrame(columns=["time", "temp", "remote_serial"])

    history = pd.read_csv(HISTORY_CSV)
    if history.empty:
        return pd.DataFrame(columns=["time", "temp", "remote_serial"])

    history["time"] = pd.to_datetime(history["time"], errors="coerce")
    history["temp"] = pd.to_numeric(history["temp"], errors="coerce")
    history["remote_serial"] = history["remote_serial"].astype(str)
    history = history.dropna(subset=["time", "temp", "remote_serial"])
    return history[["time", "temp", "remote_serial"]]


def save_history_csv(df: pd.DataFrame) -> None:
    HISTORY_CSV.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(HISTORY_CSV, index=False, encoding="utf-8-sig")


def prepare_chart_df(df: pd.DataFrame) -> pd.DataFrame:
    # 描画点数を抑えてブラウザ負荷を下げる
    chunks = []
    for serial, group in df.groupby("remote_serial", sort=False):
        if len(group) > MAX_CHART_POINTS_PER_SERIAL:
            step = max(1, len(group) // MAX_CHART_POINTS_PER_SERIAL)
            group = group.iloc[::step].copy()
        chunks.append(group)
    if not chunks:
        return df
    return pd.concat(chunks, ignore_index=True)


try:
    PAYLOAD = load_api_config()
    now_jst = pd.Timestamp.now(tz="Asia/Tokyo").tz_localize(None)
    start_ts = pd.Timestamp(year=now_jst.year, month=4, day=15, hour=0, minute=0, second=0)
    end_ts = now_jst
    history_df = load_history_csv()
    history_df = history_df[history_df["time"] >= start_ts].copy()

    from_by_serial: dict[str, pd.Timestamp] = {}
    if not history_df.empty:
        for serial, max_time in history_df.groupby("remote_serial")["time"].max().items():
            # 少し戻して取得し、欠損を防ぎながら差分更新する
            from_by_serial[str(serial)] = max(start_ts, max_time - pd.Timedelta(hours=1))

    latest_df = pd.DataFrame(columns=["time", "temp", "remote_serial"])
    fetch_error = None
    try:
        latest_df = load_data(start_ts=start_ts, end_ts=end_ts, from_by_serial=from_by_serial)
    except requests.RequestException as e:
        fetch_error = e

    df = pd.concat([history_df, latest_df], ignore_index=True)
    df = df.drop_duplicates(subset=["time", "remote_serial"], keep="last")
    df = df.sort_values(["time", "remote_serial"]).reset_index(drop=True)
    df = df[df["time"] >= start_ts].copy()
    if len(df) != len(history_df) or not latest_df.empty:
        save_history_csv(df)

    if df.empty:
        st.warning("データが取得できませんでした。機器設定を確認してください。")
        st.stop()

    st.sidebar.header("⚙️ 設定")
    if st.sidebar.button("🔄 グラフ更新", width="stretch"):
        st.rerun()
    st.sidebar.caption(f"保存先: {HISTORY_CSV}")
    if fetch_error:
        st.sidebar.warning("最新データ取得に失敗したため、保存済みデータで表示中です。")
    else:
        st.sidebar.success("最新データを取得して保存しました。")

    latest_time = df["time"].max()
    st.sidebar.caption(f"最終データ時刻: {latest_time.strftime('%Y-%m-%d %H:%M:%S')}")
    if latest_time < now_jst - pd.Timedelta(hours=2):
        st.sidebar.error("最終データが2時間以上更新されていません。通信または機器状態を確認してください。")

    if "opened_at" not in st.session_state:
        st.session_state["opened_at"] = now_jst
    opened_at = st.session_state["opened_at"]
    elapsed_seconds = int((now_jst - opened_at).total_seconds())
    remaining_delay_seconds = max(0, AUTO_REFRESH_DELAY_SECONDS - elapsed_seconds)

    if remaining_delay_seconds > 0:
        remain_min = (remaining_delay_seconds + 59) // 60
        st.sidebar.info(f"自動更新は開始から10分後に有効化されます（あと約{remain_min}分）")
    else:
        st.sidebar.success(f"{AUTO_REFRESH_SECONDS}秒ごとに自動更新中")
        components.html(
            f"""
            <script>
              setTimeout(function() {{
                window.parent.location.reload();
              }}, {AUTO_REFRESH_SECONDS * 1000});
            </script>
            """,
            height=0,
        )

    avg_mode = st.sidebar.selectbox(
        "平均表示",
        ["元データ", "5分平均", "10分平均", "30分平均", "1時間平均"],
    )

    now = now_jst
    st.caption(f"表示期間: {start_ts.strftime('%Y-%m-%d %H:%M')} ～ {end_ts.strftime('%Y-%m-%d %H:%M')}")

    filtered = df[(df["time"] >= start_ts) & (df["time"] <= end_ts)].copy()
    if filtered.empty:
        st.warning("指定期間にデータがありません。")
        st.stop()

    filtered = filtered.set_index("time")
    if avg_mode == "5分平均":
        filtered = filtered.groupby("remote_serial").resample("5min").mean(numeric_only=True)
    elif avg_mode == "10分平均":
        filtered = filtered.groupby("remote_serial").resample("10min").mean(numeric_only=True)
    elif avg_mode == "30分平均":
        filtered = filtered.groupby("remote_serial").resample("30min").mean(numeric_only=True)
    elif avg_mode == "1時間平均":
        filtered = filtered.groupby("remote_serial").resample("1h").mean(numeric_only=True)

    filtered = filtered.dropna().reset_index()
    if filtered.empty:
        st.warning("平均化後のデータがありません。")
        st.stop()

    serial_options = sorted(filtered["remote_serial"].astype(str).unique())

    col1, col2, col3 = st.columns(3)
    col1.metric("最高温度", f"{filtered['temp'].max():.1f} ℃")
    col2.metric("最低温度", f"{filtered['temp'].min():.1f} ℃")
    col3.metric("平均温度", f"{filtered['temp'].mean():.1f} ℃")

    chart_df = prepare_chart_df(filtered)
    st.subheader("📈 温度推移")
    chart = alt.Chart(chart_df).mark_line().encode(
        x=alt.X("time:T", title="時刻"),
        y=alt.Y("temp:Q", title="温度(℃)", scale=alt.Scale(domain=[33, 42])),
        color=alt.Color("remote_serial:N", title="リモートシリアル"),
        tooltip=[
            alt.Tooltip("time:T", title="時刻"),
            alt.Tooltip("temp:Q", title="温度(℃)", format=".1f"),
            alt.Tooltip("remote_serial:N", title="リモートシリアル"),
        ],
    ).interactive()
    st.altair_chart(chart, width="stretch")

    st.sidebar.markdown("---")
    st.sidebar.subheader("📋 データ一覧表示")
    if "show_tables_by_serial" not in st.session_state:
        st.session_state["show_tables_by_serial"] = {serial: False for serial in serial_options}
    for serial in serial_options:
        st.session_state["show_tables_by_serial"].setdefault(serial, False)
        if st.sidebar.button(f"{serial} を表示/非表示", key=f"toggle_{serial}", width="stretch"):
            st.session_state["show_tables_by_serial"][serial] = not st.session_state["show_tables_by_serial"][serial]

    visible_serials = [s for s in serial_options if st.session_state["show_tables_by_serial"].get(s, False)]
    if visible_serials:
        for serial in visible_serials:
            st.subheader(f"📋 データ一覧 ({serial})")
            serial_df = filtered[filtered["remote_serial"].astype(str) == serial].copy()
            st.dataframe(serial_df, width="stretch")
    else:
        st.caption("データ一覧は非表示です。左側のシリアルボタンで表示できます。")

    csv = filtered.to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        "⬇️ CSVダウンロード",
        data=csv,
        file_name="temperature_data.csv",
        mime="text/csv",
    )

except requests.RequestException as e:
    st.error(f"API通信エラー: {e}")
except Exception as e:
    st.error(f"エラーが発生しました: {e}")
