import datetime as dt
from dataclasses import dataclass
from typing import List, Tuple

import altair as alt
import pandas as pd
import requests
import streamlit as st
from streamlit_js_eval import get_geolocation


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


APP_VERSION = "v0.3 web-gps"

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
