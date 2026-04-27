import requests
import pandas as pd
import streamlit as st
import altair as alt
from pathlib import Path
import streamlit.components.v1 as components
from concurrent.futures import ThreadPoolExecutor, as_completed

st.set_page_config(page_title="温度データ ダッシュボード", layout="wide")
st.title("楽温　ダッシュボード")
st.markdown(
    """
    <style>
    @keyframes blink-red {
      50% { opacity: 0.15; }
    }
    .std-alert {
      color: #d60000;
      font-weight: 700;
      animation: blink-red 1s step-start infinite;
    }
    .stats-table {
      width: 100%;
      border-collapse: collapse;
      margin-top: 6px;
    }
    .stats-table th, .stats-table td {
      border: 1px solid #ddd;
      padding: 6px 8px;
      text-align: right;
    }
    .stats-table th:first-child, .stats-table td:first-child {
      text-align: left;
    }
    </style>
    """,
    unsafe_allow_html=True,
)
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
SERIAL_TO_CHILD = {serial: f"子機{idx + 1}" for idx, serial in enumerate(REMOTE_SERIALS)}
VALID_SERIALS = set(REMOTE_SERIALS)
HISTORY_CSV = Path(".streamlit/ondotori_history.csv")
MAX_CHART_POINTS_PER_SERIAL = 1200
DROP_THRESHOLD_C = 0.8
FEEDING_WINDOWS = [(8, 30, 9, 30), (15, 30, 16, 30)]
RECOVERY_LIMIT_MIN = 10
STALE_HOURS = 2
NORMAL_LOOKBACK_HOURS = 1
STALE_LOOKBACK_HOURS = 24
WEEK_ANOMALY_ZSCORE = 2.0
WEEK_ANOMALY_DELTA_C = 1.5
TRANSIENT_DROP_WINDOW_MIN = 10


def load_data(start_ts: pd.Timestamp, end_ts: pd.Timestamp, from_by_serial: dict[str, pd.Timestamp] | None = None) -> pd.DataFrame:
    valid_serials = [s for s in REMOTE_SERIALS if not s.startswith("REMOTE_SERIAL_")]
    if not valid_serials:
        return pd.DataFrame(columns=["time", "temp", "remote_serial"])

    def fetch_remote_rows(remote_serial: str) -> list[dict]:
        fetch_from = start_ts
        if from_by_serial and remote_serial in from_by_serial:
            fetch_from = from_by_serial[remote_serial]
        lookback_hours = (end_ts - fetch_from).total_seconds() / 3600
        number = 65535 if lookback_hours > 6 else 5000
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

    history = pd.read_csv(HISTORY_CSV, dtype={"remote_serial": str})
    if history.empty:
        return pd.DataFrame(columns=["time", "temp", "remote_serial"])

    history["time"] = pd.to_datetime(history["time"], errors="coerce")
    history["temp"] = pd.to_numeric(history["temp"], errors="coerce")
    history["remote_serial"] = history["remote_serial"].astype(str)
    history = history.dropna(subset=["time", "temp", "remote_serial"])
    return history[["time", "temp", "remote_serial"]]


def save_history_csv(df: pd.DataFrame) -> None:
    HISTORY_CSV.parent.mkdir(parents=True, exist_ok=True)
    out = df.copy()
    out["remote_serial"] = out["remote_serial"].astype(str).str.strip()
    out.to_csv(HISTORY_CSV, index=False, encoding="utf-8-sig")


def normalize_sensor_df(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    out["time"] = pd.to_datetime(out["time"], errors="coerce")
    if getattr(out["time"].dtype, "tz", None) is not None:
        out["time"] = out["time"].dt.tz_convert("Asia/Tokyo").dt.tz_localize(None)
    out["temp"] = pd.to_numeric(out["temp"], errors="coerce")
    out["remote_serial"] = out["remote_serial"].astype(str).str.strip()
    out = out.dropna(subset=["time", "temp", "remote_serial"])
    out = out[out["remote_serial"].isin(VALID_SERIALS)]
    return out.sort_values(["time", "remote_serial"]).reset_index(drop=True)


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


def child_label(serial: str) -> str:
    s = str(serial).strip()
    return SERIAL_TO_CHILD.get(s, f"子機({s})")


def is_feeding_time(ts: pd.Timestamp) -> bool:
    minutes = ts.hour * 60 + ts.minute
    for sh, sm, eh, em in FEEDING_WINDOWS:
        if (sh * 60 + sm) <= minutes <= (eh * 60 + em):
            return True
    return False


def detect_drop_events(df: pd.DataFrame) -> pd.DataFrame:
    events = []
    if df.empty:
        return pd.DataFrame()

    for serial, group in df.groupby("remote_serial"):
        g = group.sort_values("time").reset_index(drop=True)
        if len(g) < 2:
            continue
        g["delta"] = g["temp"].diff()

        for idx in g.index[g["delta"] <= -DROP_THRESHOLD_C]:
            if idx <= 0:
                continue
            event_time = g.at[idx, "time"]
            before_temp = g.at[idx - 1, "temp"]
            after_temp = g.at[idx, "temp"]

            after = g.iloc[idx + 1 :].copy()
            recovered = after[after["temp"] >= before_temp]
            if recovered.empty:
                recovery_min = None
                status = "異常"
                recovery_time = None
            else:
                recovery_time = recovered.iloc[0]["time"]
                recovery_min = (recovery_time - event_time).total_seconds() / 60
                status = "正常" if recovery_min <= RECOVERY_LIMIT_MIN else "異常"

            behavior = "採食" if is_feeding_time(event_time) else "飲水"
            events.append(
                {
                    "リモートシリアル": str(serial),
                    "子機番号": child_label(str(serial)),
                    "イベント時刻": event_time,
                    "判定種別": behavior,
                    "低下前温度(℃)": round(float(before_temp), 2),
                    "低下後温度(℃)": round(float(after_temp), 2),
                    "回復時間(分)": None if recovery_min is None else round(float(recovery_min), 1),
                    "判定": status,
                    "回復時刻": recovery_time,
                }
            )

    if not events:
        return pd.DataFrame()

    event_df = pd.DataFrame(events).sort_values("イベント時刻", ascending=False).reset_index(drop=True)
    cols = ["子機番号", "イベント時刻", "判定種別", "低下前温度(℃)", "低下後温度(℃)", "回復時間(分)", "判定", "回復時刻"]
    event_df = event_df[cols]
    return event_df


def remove_transient_drops(df: pd.DataFrame, window_min: int = TRANSIENT_DROP_WINDOW_MIN) -> pd.DataFrame:
    """短時間(既定10分)で回復する一時的な低下点を除外する。"""
    if df.empty:
        return df

    cleaned_chunks = []
    for serial, group in df.groupby("remote_serial", sort=False):
        g = group.sort_values("time").copy().reset_index(drop=True)
        keep = [True] * len(g)
        for i in range(1, len(g)):
            prev_temp = g.at[i - 1, "temp"]
            cur_temp = g.at[i, "temp"]
            if cur_temp >= prev_temp:
                continue

            t0 = g.at[i, "time"]
            t1 = t0 + pd.Timedelta(minutes=window_min)
            future = g[(g["time"] > t0) & (g["time"] <= t1)]
            if not future.empty and (future["temp"] >= prev_temp).any():
                keep[i] = False

        cleaned_chunks.append(g[keep])

    if not cleaned_chunks:
        return pd.DataFrame(columns=df.columns)
    return pd.concat(cleaned_chunks, ignore_index=True).sort_values(["time", "remote_serial"]).reset_index(drop=True)


def render_serial_stats_html(serial_stats: pd.DataFrame) -> str:
    header = (
        "<table class='stats-table'><thead><tr>"
        "<th>子機番号</th><th>平均温度(℃)</th><th>最高温度(℃)</th>"
        "<th>最低温度(℃)</th><th>温度ばらつき(標準偏差)</th></tr></thead><tbody>"
    )
    rows = []
    for _, row in serial_stats.iterrows():
        std_val = float(row["温度ばらつき(標準偏差)"])
        std_text = f"{std_val:.3f}"
        if std_val > 1.0:
            std_text = f"<span class='std-alert'>{std_text}</span>"
        rows.append(
            "<tr>"
            f"<td>{row['子機番号']}</td>"
            f"<td>{float(row['平均温度(℃)']):.2f}</td>"
            f"<td>{float(row['最高温度(℃)']):.2f}</td>"
            f"<td>{float(row['最低温度(℃)']):.2f}</td>"
            f"<td>{std_text}</td>"
            "</tr>"
        )
    return header + "".join(rows) + "</tbody></table>"


def analyze_weekly_deviation(df: pd.DataFrame, end_ts: pd.Timestamp) -> pd.DataFrame:
    df = remove_transient_drops(df)
    week_start = end_ts - pd.Timedelta(days=7)
    week_df = df[df["time"] >= week_start].copy()
    if week_df.empty:
        return pd.DataFrame()

    latest_rows = (
        week_df.sort_values("time")
        .groupby("remote_serial", as_index=False)
        .tail(1)
        .reset_index(drop=True)
    )
    baseline = (
        week_df.groupby("remote_serial")["temp"]
        .agg(["mean", "std"])
        .reset_index()
        .rename(columns={"mean": "週平均(℃)", "std": "週標準偏差"})
    )
    out = latest_rows.merge(baseline, on="remote_serial", how="left")
    out["最新値(℃)"] = out["temp"]
    out["乖離(℃)"] = out["最新値(℃)"] - out["週平均(℃)"]
    out["zスコア"] = out.apply(
        lambda r: 0.0 if pd.isna(r["週標準偏差"]) or r["週標準偏差"] == 0 else (r["乖離(℃)"] / r["週標準偏差"]),
        axis=1,
    )
    out["判定"] = out.apply(
        lambda r: "異常"
        if (abs(r["zスコア"]) >= WEEK_ANOMALY_ZSCORE or abs(r["乖離(℃)"]) >= WEEK_ANOMALY_DELTA_C)
        else "正常",
        axis=1,
    )
    out["子機番号"] = out["remote_serial"].apply(child_label)
    out["時刻"] = out["time"]
    out = out[
        ["子機番号", "時刻", "最新値(℃)", "週平均(℃)", "乖離(℃)", "zスコア", "判定"]
    ].copy()
    out["最新値(℃)"] = out["最新値(℃)"].round(2)
    out["週平均(℃)"] = out["週平均(℃)"].round(2)
    out["乖離(℃)"] = out["乖離(℃)"].round(2)
    out["zスコア"] = out["zスコア"].round(2)
    return out.sort_values(["判定", "子機番号"], ascending=[False, True]).reset_index(drop=True)


try:
    PAYLOAD = load_api_config()
    now_jst = pd.Timestamp.now(tz="Asia/Tokyo").tz_localize(None)
    start_ts = pd.Timestamp(year=now_jst.year, month=4, day=15, hour=0, minute=0, second=0)
    end_ts = now_jst
    history_df = load_history_csv()
    history_df = normalize_sensor_df(history_df)
    history_df = history_df[history_df["time"] >= start_ts].copy()

    from_by_serial: dict[str, pd.Timestamp] = {}
    if not history_df.empty:
        for serial, max_time in history_df.groupby("remote_serial")["time"].max().items():
            # 更新停止時は取得範囲を広げて自動復旧しやすくする
            lookback = STALE_LOOKBACK_HOURS if max_time < (end_ts - pd.Timedelta(hours=STALE_HOURS)) else NORMAL_LOOKBACK_HOURS
            from_by_serial[str(serial)] = max(start_ts, max_time - pd.Timedelta(hours=lookback))

    latest_df = pd.DataFrame(columns=["time", "temp", "remote_serial"])
    fetch_error = None
    try:
        latest_df = load_data(start_ts=start_ts, end_ts=end_ts, from_by_serial=from_by_serial)
    except requests.RequestException as e:
        fetch_error = e

    df = pd.concat([history_df, latest_df], ignore_index=True)
    df = normalize_sensor_df(df)
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

    raw_period_df = df[(df["time"] >= start_ts) & (df["time"] <= end_ts)].copy()
    if raw_period_df.empty:
        st.warning("指定期間にデータがありません。")
        st.stop()

    raw_period_df = normalize_sensor_df(raw_period_df)

    filtered = raw_period_df.set_index("time")
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

    filtered["子機番号"] = filtered["remote_serial"].apply(child_label)
    serial_options = sorted(filtered["remote_serial"].astype(str).unique())

    st.subheader("🔎 分析・解析")
    analysis_col1, analysis_col2 = st.columns(2)
    with analysis_col1:
        stats_24h_df = raw_period_df[raw_period_df["time"] >= (end_ts - pd.Timedelta(hours=24))].copy()
        stats_24h_df = remove_transient_drops(stats_24h_df)
        stats_24h_df["子機番号"] = stats_24h_df["remote_serial"].apply(child_label)
        serial_stats = (
            stats_24h_df.groupby("子機番号")["temp"]
            .agg(["mean", "max", "min", "std"])
            .reset_index()
            .rename(
                columns={
                    "子機番号": "子機番号",
                    "mean": "平均温度(℃)",
                    "max": "最高温度(℃)",
                    "min": "最低温度(℃)",
                    "std": "温度ばらつき(標準偏差)",
                }
            )
        )
        serial_stats["平均温度(℃)"] = serial_stats["平均温度(℃)"].round(2)
        serial_stats["最高温度(℃)"] = serial_stats["最高温度(℃)"].round(2)
        serial_stats["最低温度(℃)"] = serial_stats["最低温度(℃)"].round(2)
        serial_stats["温度ばらつき(標準偏差)"] = serial_stats["温度ばらつき(標準偏差)"].fillna(0).round(3)
        st.caption("機器ごとの基本統計（過去24時間、10分以内の瞬間低下を除外）")
        st.markdown(render_serial_stats_html(serial_stats), unsafe_allow_html=True)
        if (serial_stats["温度ばらつき(標準偏差)"] > 1.0).any():
            st.error("標準偏差が1を超える子機は疾病疑いとして表示しています。")

    with analysis_col2:
        trend_df = (
            raw_period_df.sort_values(["remote_serial", "time"])
            .set_index("time")
            .groupby("remote_serial")
            .resample("30min")
            .mean(numeric_only=True)
            .dropna()
            .reset_index()
        )
        trend_df["子機番号"] = trend_df["remote_serial"].apply(child_label)
        st.caption("温度推移（30分平均・2日表示）")
        if trend_df.empty:
            st.caption("表示できるデータがありません。")
        else:
            window_days = 2
            min_time = trend_df["time"].min()
            max_time = trend_df["time"].max()
            span_days = max(0, (max_time.date() - min_time.date()).days)
            max_offset_days = max(0, span_days - (window_days - 1))
            offset_days = st.slider(
                "表示オフセット（日）",
                min_value=0,
                max_value=max_offset_days,
                value=0,
                key="trend_offset_days",
            )
            window_end = max_time - pd.Timedelta(days=offset_days)
            window_start = window_end - pd.Timedelta(days=window_days)
            window_df = trend_df[(trend_df["time"] >= window_start) & (trend_df["time"] <= window_end)].copy()
            trend_chart = (
                alt.Chart(window_df)
                .mark_line()
                .encode(
                    x=alt.X("time:T", title="時刻"),
                    y=alt.Y("temp:Q", title="温度(℃)", scale=alt.Scale(domain=[35, 43])),
                    color=alt.Color("子機番号:N", title="子機番号"),
                    tooltip=[
                        alt.Tooltip("time:T", title="時刻"),
                        alt.Tooltip("temp:Q", title="温度(℃)", format=".1f"),
                        alt.Tooltip("子機番号:N", title="子機番号"),
                    ],
                )
                .interactive()
            )
            st.altair_chart(trend_chart, width="stretch")

    hottest = filtered.loc[filtered["temp"].idxmax()]
    coldest = filtered.loc[filtered["temp"].idxmin()]
    st.info(
        "解析結果: 最高温度は "
        f"{hottest['time'].strftime('%Y-%m-%d %H:%M')} ({child_label(hottest['remote_serial'])}) に {hottest['temp']:.1f}℃、"
        f"最低温度は {coldest['time'].strftime('%Y-%m-%d %H:%M')} ({child_label(coldest['remote_serial'])}) に {coldest['temp']:.1f}℃ です。"
    )

    weekly_dev_df = analyze_weekly_deviation(raw_period_df, end_ts=end_ts)
    st.caption("過去1週間との乖離分析（最新値 vs 週平均）")
    if weekly_dev_df.empty:
        st.caption("過去1週間の分析対象データがありません。")
    else:
        abnormal_weekly = weekly_dev_df[weekly_dev_df["判定"] == "異常"]
        if abnormal_weekly.empty:
            st.success("過去1週間との比較で異常は検出されませんでした。")
        else:
            names = "、".join(abnormal_weekly["子機番号"].tolist())
            st.warning(f"過去1週間との乖離で異常を検出: {names}")
        st.dataframe(weekly_dev_df, width="stretch", hide_index=True)

    event_df = detect_drop_events(raw_period_df)
    st.caption(
        "急低下判定ルール: 9時頃/16時頃は採食、それ以外は飲水。"
        f"回復{RECOVERY_LIMIT_MIN}分以内に同温度へ戻れば正常。"
    )
    if event_df.empty:
        st.caption("急低下イベントは検出されませんでした。")
    else:
        abnormal_count = int((event_df["判定"] == "異常").sum())
        normal_count = int((event_df["判定"] == "正常").sum())
        st.write(f"イベント検出: 正常 {normal_count}件 / 異常 {abnormal_count}件")
        st.dataframe(event_df, width="stretch", hide_index=True)

    chart_series = (
        raw_period_df.sort_values(["remote_serial", "time"])
        .set_index("time")
        .groupby("remote_serial")
        .resample("30min")
        .mean(numeric_only=True)
        .dropna()
        .reset_index()
    )
    chart_series["子機番号"] = chart_series["remote_serial"].apply(child_label)
    chart_df = prepare_chart_df(chart_series)
    st.subheader("📈 温度推移（30分平均）")
    chart = alt.Chart(chart_df).mark_line().encode(
        x=alt.X("time:T", title="時刻"),
        y=alt.Y("temp:Q", title="温度(℃)", scale=alt.Scale(domain=[35, 43])),
        color=alt.Color("子機番号:N", title="子機番号"),
        tooltip=[
            alt.Tooltip("time:T", title="時刻"),
            alt.Tooltip("temp:Q", title="温度(℃)", format=".1f"),
            alt.Tooltip("子機番号:N", title="子機番号"),
        ],
    ).interactive()
    st.altair_chart(chart, width="stretch")

    st.sidebar.markdown("---")
    st.sidebar.subheader("📋 データ一覧表示")
    if "show_tables_by_serial" not in st.session_state:
        st.session_state["show_tables_by_serial"] = {serial: False for serial in serial_options}
    for serial in serial_options:
        st.session_state["show_tables_by_serial"].setdefault(serial, False)
        if st.sidebar.button(f"{child_label(serial)} を表示/非表示", key=f"toggle_{serial}", width="stretch"):
            st.session_state["show_tables_by_serial"][serial] = not st.session_state["show_tables_by_serial"][serial]

    visible_serials = [s for s in serial_options if st.session_state["show_tables_by_serial"].get(s, False)]
    if visible_serials:
        for serial in visible_serials:
            st.subheader(f"📋 データ一覧 ({child_label(serial)})")
            serial_df = filtered[filtered["remote_serial"].astype(str) == serial].copy()
            serial_df = serial_df.drop(columns=["remote_serial"])
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
