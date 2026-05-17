import datetime as dt
import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple

import altair as alt
import pandas as pd
import requests
import streamlit as st
from streamlit_js_eval import get_geolocation, streamlit_js_eval


# =========================
# 換気判断アシスタント v0.2 simple
# =========================
# 対応項目：
# - PM2.5
# - PM10
# - Dust（黄砂・砂じん系の目安）
# - 花粉系合計（Open-Meteoの花粉データ合算）
# - 雨
# - 風
# - 湿度
#
# 重要：
# - Open-Meteoの花粉は、日本のスギ・ヒノキを直接表すものではありません。
# - ここでは「花粉系の目安」として、取得できる花粉データを合算して使います。


APP_VERSION = "v0.4 web-gps-analytics"
LOG_FILE = Path("usage_log.csv")
VISITOR_SALT = "ventilation-forecast-app-v1"

# 西尾市などの固定プリセットは廃止。
# スマホ・PCブラウザの位置情報、または手入力した緯度経度を使って予報します。


@dataclass
class Thresholds:
    pm25_ok: float = 15.0
    pm25_caution: float = 25.0
    pm10_ok: float = 45.0
    pm10_caution: float = 70.0
    dust_ok: float = 20.0
    dust_caution: float = 50.0

    # 花粉系合計の暫定しきい値
    pollen_ok: float = 10.0
    pollen_caution: float = 50.0

    rain_caution: float = 0.5
    wind_caution: float = 8.0
    wind_ng: float = 12.0
    humidity_caution: float = 80.0


AIR_HOURLY = [
    "pm10",
    "pm2_5",
    "dust",
    "alder_pollen",
    "birch_pollen",
    "grass_pollen",
    "mugwort_pollen",
    "olive_pollen",
    "ragweed_pollen",
    "european_aqi",
    "us_aqi",
]

WEATHER_HOURLY = [
    "precipitation",
    "wind_speed_10m",
    "relative_humidity_2m",
]

POLLEN_COLS = [
    "alder_pollen",
    "birch_pollen",
    "grass_pollen",
    "mugwort_pollen",
    "olive_pollen",
    "ragweed_pollen",
]


def _extract_coords(location) -> Tuple[float | None, float | None, str | None]:
    """streamlit-js-evalのget_geolocation()戻り値から緯度経度を安全に取り出す。"""
    if not location:
        return None, None, None

    if isinstance(location, dict) and "error" in location:
        err = location.get("error") or {}
        return None, None, str(err.get("message") or "位置情報の取得が許可されませんでした。")

    if isinstance(location, dict):
        coords = location.get("coords") if isinstance(location.get("coords"), dict) else location

        lat = coords.get("latitude") or coords.get("lat")
        lon = coords.get("longitude") or coords.get("lon") or coords.get("lng")

        if lat is not None and lon is not None:
            return float(lat), float(lon), None

    return None, None, "位置情報の形式を読み取れませんでした。"


def _parse_manual_coords(lat_text: str, lon_text: str) -> Tuple[float | None, float | None, str | None]:
    """手入力の緯度経度を検証して返す。"""
    if not lat_text.strip() and not lon_text.strip():
        return None, None, None

    try:
        lat = float(lat_text)
        lon = float(lon_text)
    except ValueError:
        return None, None, "緯度・経度は数値で入力してください。"

    if not (-90 <= lat <= 90):
        return None, None, "緯度は -90〜90 の範囲で入力してください。"
    if not (-180 <= lon <= 180):
        return None, None, "経度は -180〜180 の範囲で入力してください。"

    return lat, lon, None


def _now_jst() -> dt.datetime:
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=9)))


def _hash_visitor_id(visitor_id: str | None) -> str:
    raw = visitor_id or "unknown"
    return hashlib.sha256(f"{VISITOR_SALT}:{raw}".encode("utf-8")).hexdigest()[:16]


def _round_coord(value: float | None, digits: int = 2) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def _get_admin_pin() -> str:
    """Streamlit Secretsまたは環境変数から管理画面PINを取得。未設定時は初期値1234。"""
    try:
        pin = st.secrets.get("ADMIN_PIN", None)
        if pin is not None:
            return str(pin)
    except Exception:
        pass
    return os.environ.get("ADMIN_PIN", "1234")


def get_browser_visitor_id() -> str | None:
    """ブラウザのlocalStorageに匿名IDを作成・保存して、ユニーク数の推定に使う。"""
    js = """
    (() => {
      const key = 'vfa_visitor_id_v1';
      let id = localStorage.getItem(key);
      if (!id) {
        if (window.crypto && crypto.randomUUID) {
          id = crypto.randomUUID();
        } else {
          id = String(Date.now()) + '-' + String(Math.random()).slice(2);
        }
        localStorage.setItem(key, id);
      }
      return id;
    })()
    """
    try:
        return streamlit_js_eval(js_expressions=js, key="visitor_id_v1")
    except Exception:
        return None


def get_browser_info() -> Tuple[str, int | None]:
    """端末種別の推定に使う情報をブラウザから取得。"""
    try:
        width = streamlit_js_eval(js_expressions="window.innerWidth", key="screen_width_v1")
    except Exception:
        width = None

    try:
        ua = streamlit_js_eval(js_expressions="navigator.userAgent", key="user_agent_v1")
    except Exception:
        ua = ""

    width_int = None
    try:
        if width is not None:
            width_int = int(width)
    except Exception:
        width_int = None

    ua_text = str(ua or "").lower()
    if width_int is not None and width_int <= 768:
        device_type = "smartphone"
    elif any(x in ua_text for x in ["iphone", "android", "mobile"]):
        device_type = "smartphone"
    elif any(x in ua_text for x in ["ipad", "tablet"]):
        device_type = "tablet"
    elif width_int is not None:
        device_type = "desktop"
    else:
        device_type = "unknown"

    return device_type, width_int


def log_usage_event(
    event_name: str,
    visitor_id: str | None,
    session_id: str,
    device_type: str,
    screen_width: int | None,
    geo_success: bool | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    location_source: str | None = None,
    forecast_days: int | None = None,
) -> None:
    """利用ログをCSVに追記する。個人情報対策としてIDはハッシュ化し、緯度経度は小数第2位に丸める。"""
    now = _now_jst()
    row = {
        "timestamp_jst": now.strftime("%Y-%m-%d %H:%M:%S"),
        "date_jst": now.strftime("%Y-%m-%d"),
        "event_name": event_name,
        "visitor_hash": _hash_visitor_id(visitor_id),
        "session_id": session_id,
        "device_type": device_type,
        "screen_width": screen_width,
        "geo_success": geo_success,
        "location_source": location_source,
        "lat_rounded": _round_coord(latitude),
        "lon_rounded": _round_coord(longitude),
        "forecast_days": forecast_days,
        "app_version": APP_VERSION,
    }

    df_row = pd.DataFrame([row])
    try:
        file_exists = LOG_FILE.exists()
        df_row.to_csv(LOG_FILE, mode="a", header=not file_exists, index=False, encoding="utf-8-sig")
    except Exception as e:
        # ログ保存失敗でアプリ本体を止めない。
        st.session_state["log_error"] = str(e)


@st.cache_data(ttl=10)
def load_usage_log() -> pd.DataFrame:
    if not LOG_FILE.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(LOG_FILE)
    except Exception:
        return pd.DataFrame()


def show_admin_analytics() -> None:
    """アプリ内の簡易アクセス解析画面。"""
    st.subheader("管理者用：利用状況")
    st.caption("この集計はアプリ内CSVログをもとにしています。Streamlit Cloudの再起動・再デプロイで消える可能性があります。")

    df = load_usage_log()
    if df.empty:
        st.info("まだ利用ログがありません。")
        return

    page_df = df[df["event_name"] == "page_view"].copy()
    forecast_df = df[df["event_name"] == "forecast_view"].copy()

    total_access = len(page_df)
    total_unique = page_df["visitor_hash"].nunique() if not page_df.empty else 0
    forecast_views = len(forecast_df)
    forecast_unique = forecast_df["visitor_hash"].nunique() if not forecast_df.empty else 0

    today = _now_jst().strftime("%Y-%m-%d")
    today_page = page_df[page_df["date_jst"] == today]
    today_unique = today_page["visitor_hash"].nunique() if not today_page.empty else 0

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("累計アクセス", f"{total_access}")
    c2.metric("累計ユニーク", f"{total_unique}")
    c3.metric("今日のアクセス", f"{len(today_page)}")
    c4.metric("今日のユニーク", f"{today_unique}")

    c5, c6, c7 = st.columns(3)
    c5.metric("予報表示回数", f"{forecast_views}")
    c6.metric("予報表示ユニーク", f"{forecast_unique}")
    if total_unique:
        c7.metric("1人あたり平均アクセス", f"{total_access / total_unique:.1f}")
    else:
        c7.metric("1人あたり平均アクセス", "-")

    if not page_df.empty:
        daily = (
            page_df.groupby("date_jst")
            .agg(アクセス数=("session_id", "count"), ユニーク数=("visitor_hash", "nunique"))
            .reset_index()
        )
        daily_long = daily.melt("date_jst", var_name="指標", value_name="件数")
        st.subheader("日別アクセス数・ユニーク数")
        chart = (
            alt.Chart(daily_long)
            .mark_line(point=True)
            .encode(
                x=alt.X("date_jst:N", title="日付"),
                y=alt.Y("件数:Q", title="件数"),
                color=alt.Color("指標:N", title="指標"),
                tooltip=["date_jst:N", "指標:N", "件数:Q"],
            )
            .properties(height=300)
        )
        st.altair_chart(chart, use_container_width=True)

    st.subheader("端末別")
    if "device_type" in page_df.columns and not page_df.empty:
        device_summary = page_df["device_type"].fillna("unknown").value_counts().reset_index()
        device_summary.columns = ["端末", "アクセス数"]
        st.dataframe(device_summary, use_container_width=True, hide_index=True)

    st.subheader("直近ログ")
    show_cols = [
        "timestamp_jst", "event_name", "visitor_hash", "device_type",
        "geo_success", "location_source", "lat_rounded", "lon_rounded", "forecast_days"
    ]
    existing_cols = [c for c in show_cols if c in df.columns]
    st.dataframe(df[existing_cols].tail(100).iloc[::-1], use_container_width=True, hide_index=True)

    csv_bytes = df.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
    st.download_button(
        "利用ログCSVをダウンロード",
        data=csv_bytes,
        file_name="usage_log.csv",
        mime="text/csv",
        use_container_width=True,
    )


@st.cache_data(ttl=60 * 30)
def fetch_air_quality(latitude: float, longitude: float, forecast_days: int) -> pd.DataFrame:
    url = "https://air-quality-api.open-meteo.com/v1/air-quality"
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "hourly": ",".join(AIR_HOURLY),
        "timezone": "Asia/Tokyo",
        "forecast_days": forecast_days,
    }
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()["hourly"]
    df = pd.DataFrame(data)
    df["time"] = pd.to_datetime(df["time"])
    return df


@st.cache_data(ttl=60 * 30)
def fetch_weather(latitude: float, longitude: float, forecast_days: int) -> pd.DataFrame:
    url = "https://api.open-meteo.com/v1/jma"
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "hourly": ",".join(WEATHER_HOURLY),
        "timezone": "Asia/Tokyo",
        "forecast_days": forecast_days,
    }
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()["hourly"]
    df = pd.DataFrame(data)
    df["time"] = pd.to_datetime(df["time"])
    return df


def safe_num(value) -> float:
    if value is None or pd.isna(value):
        return 0.0
    return float(value)


def pollen_total(row: pd.Series) -> float:
    return sum(safe_num(row.get(c)) for c in POLLEN_COLS)


def pollen_label(total: float, th: Thresholds) -> str:
    if total > th.pollen_caution:
        return "多い"
    if total > th.pollen_ok:
        return "やや多い"
    return "少ない"


def grade_row(row: pd.Series, th: Thresholds) -> Tuple[str, List[str], int]:
    """戻り値: 判定ラベル, 理由, スコア。スコアが低いほど良い。"""
    reasons: List[str] = []
    score = 0

    pm25 = safe_num(row.get("pm2_5"))
    pm10 = safe_num(row.get("pm10"))
    dust = safe_num(row.get("dust"))
    pollen = pollen_total(row)
    rain = safe_num(row.get("precipitation"))
    wind = safe_num(row.get("wind_speed_10m"))
    humidity = safe_num(row.get("relative_humidity_2m"))

    if pm25 > th.pm25_caution:
        score += 3
        reasons.append(f"PM2.5高め({pm25:.1f})")
    elif pm25 > th.pm25_ok:
        score += 1
        reasons.append(f"PM2.5やや高め({pm25:.1f})")

    if pm10 > th.pm10_caution:
        score += 2
        reasons.append(f"PM10高め({pm10:.1f})")
    elif pm10 > th.pm10_ok:
        score += 1
        reasons.append(f"PM10やや高め({pm10:.1f})")

    if dust > th.dust_caution:
        score += 3
        reasons.append(f"Dust高め({dust:.1f})")
    elif dust > th.dust_ok:
        score += 1
        reasons.append(f"Dustやや高め({dust:.1f})")

    if pollen > th.pollen_caution:
        score += 2
        reasons.append(f"花粉系多め({pollen:.1f})")
    elif pollen > th.pollen_ok:
        score += 1
        reasons.append(f"花粉系やや多め({pollen:.1f})")

    if rain >= th.rain_caution:
        score += 2
        reasons.append(f"雨({rain:.1f}mm)")

    if wind >= th.wind_ng:
        score += 3
        reasons.append(f"強風({wind:.1f}km/h)")
    elif wind >= th.wind_caution:
        score += 1
        reasons.append(f"風やや強め({wind:.1f}km/h)")

    if humidity >= th.humidity_caution:
        score += 1
        reasons.append(f"湿度高め({humidity:.0f}%)")

    if score >= 4:
        return "窓開け注意", reasons or ["総合的に注意"], score
    if score >= 2:
        return "短時間ならOK", reasons or ["やや注意"], score
    return "換気OK", reasons or ["空気条件良好"], score


def make_summary(df: pd.DataFrame) -> pd.DataFrame:
    th = Thresholds()
    rows = []
    for _, row in df.iterrows():
        label, reasons, score = grade_row(row, th)
        pollen = pollen_total(row)
        rows.append({
            "time": row["time"],
            "hour": row["time"].strftime("%H:%M"),
            "判定": label,
            "理由": "、".join(reasons),
            "score": score,
            "PM2.5": round(safe_num(row.get("pm2_5")), 1),
            "PM10": round(safe_num(row.get("pm10")), 1),
            "Dust": round(safe_num(row.get("dust")), 1),
            "花粉系判定": pollen_label(pollen, th),
            "花粉系合計": round(pollen, 1),
            "雨mm": round(safe_num(row.get("precipitation")), 1),
            "風km/h": round(safe_num(row.get("wind_speed_10m")), 1),
            "湿度%": round(safe_num(row.get("relative_humidity_2m")), 0),
        })
    return pd.DataFrame(rows)


def compress_time_ranges(day_df: pd.DataFrame, target_label: str) -> List[str]:
    ranges: List[str] = []
    current_start = None
    prev_time = None

    for _, row in day_df.iterrows():
        is_target = row["判定"] == target_label
        t = row["time"]
        if is_target and current_start is None:
            current_start = t
        if not is_target and current_start is not None:
            ranges.append(f"{current_start.strftime('%H:%M')}〜{prev_time.strftime('%H:%M')}")
            current_start = None
        prev_time = t + pd.Timedelta(hours=1)

    if current_start is not None and prev_time is not None:
        ranges.append(f"{current_start.strftime('%H:%M')}〜{prev_time.strftime('%H:%M')}")

    return ranges


def create_voice_text(day_df: pd.DataFrame) -> str:
    ok_ranges = compress_time_ranges(day_df, "換気OK")
    caution_ranges = compress_time_ranges(day_df, "短時間ならOK")
    ng_ranges = compress_time_ranges(day_df, "窓開け注意")

    best = day_df.sort_values(["score", "PM2.5", "Dust", "花粉系合計"]).head(3)
    best_hours = "、".join(best["hour"].tolist())

    max_pm25 = day_df["PM2.5"].max()
    max_dust = day_df["Dust"].max()
    max_pollen = day_df["花粉系合計"].max()

    text = []
    if ok_ranges:
        text.append(f"今日は、{ '、'.join(ok_ranges[:3]) } が換気に向いています。")
    else:
        text.append("今日は、長時間の窓開けに向いた時間帯は少なめです。")

    if caution_ranges:
        text.append(f"{ '、'.join(caution_ranges[:3]) } は短時間換気ならよさそうです。")
    if ng_ranges:
        text.append(f"{ '、'.join(ng_ranges[:3]) } は窓開け注意です。")

    text.append(f"特に条件が良い時間は、{best_hours} 頃です。")
    text.append(f"今日の最大値は、PM2.5が{max_pm25:.1f}、Dustが{max_dust:.1f}、花粉系が{max_pollen:.1f}です。")
    return "".join(text)


def judgment_badge(label: str) -> str:
    if label == "換気OK":
        return "🟢"
    if label == "短時間ならOK":
        return "🟡"
    return "🔴"


def jp_weekday(ts: pd.Timestamp) -> str:
    weekdays = ["月", "火", "水", "木", "金", "土", "日"]
    return weekdays[ts.weekday()]


def make_axis_label(ts: pd.Timestamp) -> str:
    """0時だけ日付＋曜日、それ以外は24時間表記。"""
    if ts.hour == 0:
        return f"{ts.month}/{ts.day}({jp_weekday(ts)})\n00:00"
    return ts.strftime("%H:%M")


def make_line_chart(df: pd.DataFrame, value_cols: List[str], title: str):
    """横軸を24時間表記にし、0時に日付＋曜日を出す折れ線グラフ。"""
    chart_df = df[["time"] + value_cols].copy()
    chart_df["時刻表示"] = chart_df["time"].apply(make_axis_label)
    chart_df["表示順"] = range(len(chart_df))

    long_df = chart_df.melt(
        id_vars=["time", "時刻表示", "表示順"],
        value_vars=value_cols,
        var_name="項目",
        value_name="値",
    )

    return (
        alt.Chart(long_df, title=title)
        .mark_line(point=True)
        .encode(
            x=alt.X(
                "時刻表示:N",
                sort=alt.SortField(field="表示順", order="ascending"),
                title="日時",
                axis=alt.Axis(labelAngle=0),
            ),
            y=alt.Y("値:Q", title="値"),
            color=alt.Color("項目:N", title="項目"),
            tooltip=[
                alt.Tooltip("time:T", title="日時", format="%Y/%m/%d %H:%M"),
                alt.Tooltip("項目:N", title="項目"),
                alt.Tooltip("値:Q", title="値", format=".1f"),
            ],
        )
        .properties(height=320)
        .interactive()
    )


def main() -> None:
    st.set_page_config(page_title="換気判断アシスタント", layout="centered")
    st.title(f"換気判断アシスタント {APP_VERSION}")
    st.caption("スマホ・PCの位置情報を使い、PM2.5 / PM10 / Dust / 花粉系 / 雨 / 風から、窓を開けてよい時間帯を判定します。")

    if "latitude" not in st.session_state:
        st.session_state.latitude = None
    if "longitude" not in st.session_state:
        st.session_state.longitude = None
    if "location_source" not in st.session_state:
        st.session_state.location_source = "未設定"
    if "session_id" not in st.session_state:
        st.session_state.session_id = str(uuid.uuid4())
    if "logged_page_view" not in st.session_state:
        st.session_state.logged_page_view = False
    if "logged_forecast_view" not in st.session_state:
        st.session_state.logged_forecast_view = False

    visitor_id = get_browser_visitor_id()
    device_type, screen_width = get_browser_info()

    if visitor_id and not st.session_state.logged_page_view:
        log_usage_event(
            event_name="page_view",
            visitor_id=visitor_id,
            session_id=st.session_state.session_id,
            device_type=device_type,
            screen_width=screen_width,
        )
        st.session_state.logged_page_view = True

    with st.sidebar:
        st.header("場所")
        st.caption("スマホの場合は、ブラウザの位置情報許可が必要です。")

        location = get_geolocation()
        gps_lat, gps_lon, gps_error = _extract_coords(location)

        if gps_lat is not None and gps_lon is not None:
            st.session_state.latitude = gps_lat
            st.session_state.longitude = gps_lon
            st.session_state.location_source = "現在地"
            st.success("現在地を取得しました。")
        elif gps_error:
            st.warning(gps_error)

        st.divider()
        st.caption("位置情報が使えない場合は、緯度・経度を手入力できます。")
        manual_lat = st.text_input("緯度", placeholder="例: 34.862600")
        manual_lon = st.text_input("経度", placeholder="例: 137.061000")

        if st.button("手入力の地点を使う", use_container_width=True):
            lat, lon, err = _parse_manual_coords(manual_lat, manual_lon)
            if err:
                st.error(err)
            elif lat is not None and lon is not None:
                st.session_state.latitude = lat
                st.session_state.longitude = lon
                st.session_state.location_source = "手入力"
                st.success("手入力の地点を設定しました。")

        forecast_days = st.slider("予報日数", 1, 5, 2)

        st.header("判定の考え方")
        st.caption("花粉はカテゴリー分けせず、花粉系合計として換気判断に反映します。")
        st.warning("スギ・ヒノキ専用値ではありません。")

        st.divider()
        st.header("管理者")
        show_admin = st.checkbox("利用状況を表示")
        admin_ok = False
        if show_admin:
            pin_input = st.text_input("管理PIN", type="password")
            admin_ok = pin_input == _get_admin_pin()
            if pin_input and not admin_ok:
                st.error("管理PINが違います。")

    if show_admin and admin_ok:
        show_admin_analytics()
        st.stop()

    latitude = st.session_state.latitude
    longitude = st.session_state.longitude

    if latitude is None or longitude is None:
        st.info("まず、サイドバーから現在地の取得を許可してください。位置情報が使えない場合は、緯度・経度を手入力してください。")
        st.stop()

    st.caption(f"使用地点: {st.session_state.location_source} / 緯度 {latitude:.6f}, 経度 {longitude:.6f}")
    st.map(pd.DataFrame([{"lat": latitude, "lon": longitude}]), latitude="lat", longitude="lon", zoom=11)

    try:
        air = fetch_air_quality(latitude, longitude, forecast_days)
        weather = fetch_weather(latitude, longitude, forecast_days)
        merged = pd.merge(air, weather, on="time", how="inner")
        summary = make_summary(merged)

        now = pd.Timestamp(dt.datetime.now()).floor("h")
        forecast_end = now + pd.Timedelta(days=forecast_days)

        forecast_df = summary[
            (summary["time"] >= now) &
            (summary["time"] < forecast_end)
        ].copy()

        forecast_df["表示"] = forecast_df["判定"].map(judgment_badge) + " " + forecast_df["判定"]
        forecast_df["日付"] = forecast_df["time"].apply(
            lambda x: f"{x.month}/{x.day}({jp_weekday(x)})"
        )

        if visitor_id and not st.session_state.logged_forecast_view:
            log_usage_event(
                event_name="forecast_view",
                visitor_id=visitor_id,
                session_id=st.session_state.session_id,
                device_type=device_type,
                screen_width=screen_width,
                geo_success=st.session_state.location_source == "現在地",
                latitude=latitude,
                longitude=longitude,
                location_source=st.session_state.location_source,
                forecast_days=forecast_days,
            )
            st.session_state.logged_forecast_view = True

        st.subheader("今後の換気予報")
        st.success(create_voice_text(forecast_df))

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("換気OK", f"{(forecast_df['判定'] == '換気OK').sum()} 時間")
        c2.metric("短時間ならOK", f"{(forecast_df['判定'] == '短時間ならOK').sum()} 時間")
        c3.metric("窓開け注意", f"{(forecast_df['判定'] == '窓開け注意').sum()} 時間")
        c4.metric("花粉系 最大", f"{forecast_df['花粉系合計'].max():.1f}")

        st.subheader("時間帯別の換気予報")
        show_cols = [
            "日付",
            "hour",
            "表示",
            "理由",
            "PM2.5",
            "PM10",
            "Dust",
            "花粉系判定",
            "花粉系合計",
            "雨mm",
            "風km/h",
            "湿度%",
        ]
        st.dataframe(forecast_df[show_cols], use_container_width=True, hide_index=True)

        st.subheader("音声読み上げ用テキスト")
        st.text_area("Alexa / iPhoneショートカット / LINE通知に流す文面", create_voice_text(forecast_df), height=150)

        st.subheader("空気質の推移")
        st.altair_chart(
            make_line_chart(forecast_df, ["PM2.5", "PM10", "Dust"], "空気質の推移"),
            use_container_width=True,
        )

        st.subheader("花粉系の推移")
        st.altair_chart(
            make_line_chart(forecast_df, ["花粉系合計"], "花粉系の推移"),
            use_container_width=True,
        )

        with st.expander("花粉データについて"):
            st.write("Open-Meteoの花粉データは、alder / birch / grass / mugwort / olive / ragweed などです。")
            st.write("本アプリでは、それらをカテゴリー分けせず、花粉系合計として扱います。")
            st.write("日本で主要なスギ・ヒノキ花粉の専用データではありません。")
            st.write("スギ・ヒノキを正確に扱う場合は、Weathernews WxTechなどの有償API連携を検討してください。")

    except Exception as e:
        st.error("データ取得または判定中にエラーが発生しました。")
        st.exception(e)


if __name__ == "__main__":
    main()
